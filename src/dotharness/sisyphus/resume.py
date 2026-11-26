"""Resume support: list stopped items, detect that a stop is cleared, locate the answer file.

Cheap and deterministic: no agent call, no network. A stop clears when (see ``stoprecord.CLEARS_BY``)
the brief no longer has blocking open decisions (C1), the user wrote a non-empty answer file (C2..C4),
or a bounded retry delay has passed (C8). Everything else (C5..C7, errors, anything unknown) needs an
explicit ``sisyphus resume``. Imports only Package A modules.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dotharness.sisyphus.config import SisyphusConfig
from dotharness.sisyphus.intake import IntakeError, parse_open_decisions, resolve_brief_path
from dotharness.sisyphus.runrecord import STAGE_COMPLETE, RunRecord, record_path
from dotharness.sisyphus.stages import (
    AUTO_RETRY_KEY,
    BUILD_RETRY_KEY,
    BUILD_STAGE,
    INTAKE_STAGE,
    OPEN_PR_STAGE,
    PLAN_STAGE,
    PREWORK_RETRY_KEY,
    PREWORK_STAGE,
    PUSH_STAGE,
    REFACTOR_RETRY_KEY,
    REFACTOR_STAGE,
)
from dotharness.sisyphus.stoprecord import (
    AUTO_RETRY_DELAY_MINUTES,
    AUTO_RETRY_MAX,
    CLEARS_BY,
    StopRecord,
    is_abandoned,
    read_stop_record,
)

SIDECAR_SUFFIXES = (".stop.json", ".intake.json", ".stopped.json")
ANSWER_SUFFIX = ".answer.md"
ANSWERS_LOG_SUFFIX = ".answers.jsonl"
USED_SUFFIX = ".used"

# `sisyphus resume --reset-retries` zeroes exactly these run-record counters for the RECORDED stage of the
# stopped item, and nothing else. Exact key names, no prefix matching. `branch-created` is a marker that
# Sisyphus made the branch (the push guard relies on it), not a retry counter: it is never listed here.
# `plan:leaf-limit` is also left alone: it is a "leaves already planned" marker that stops a C6 resume from
# re-planning (and overwriting the leaves), not a budget. `auto-retry` is the C8 auto-resume budget; it belongs
# to whichever stage the item stopped at. Stages without their own counter reset only `auto-retry`.
RESET_KEYS: dict[str, tuple[str, ...]] = {
    INTAKE_STAGE: (AUTO_RETRY_KEY,),
    PLAN_STAGE: (AUTO_RETRY_KEY,),
    PREWORK_STAGE: (PREWORK_RETRY_KEY, AUTO_RETRY_KEY),
    BUILD_STAGE: (BUILD_RETRY_KEY, AUTO_RETRY_KEY),
    REFACTOR_STAGE: (REFACTOR_RETRY_KEY, AUTO_RETRY_KEY),
    PUSH_STAGE: (AUTO_RETRY_KEY,),
    OPEN_PR_STAGE: (AUTO_RETRY_KEY,),
}


def reset_keys_for(stage: str | None) -> tuple[str, ...]:
    """The counters ``--reset-retries`` zeroes for ``stage`` (empty for no or an unknown stage)."""
    return RESET_KEYS.get(stage or "", ())


def reset_stage_retries(rec: RunRecord, now: datetime | None = None) -> dict[str, int]:
    """Explicit reset for the recorded stage of ``rec``; returns ``{key: previous}`` for what changed.

    Records the event on the run record (also when there was nothing to reset). Only ever called from
    ``sisyphus resume --reset-retries``: never by auto-resume, ``run`` or loading a record.
    """
    return rec.reset_retries(reset_keys_for(rec.stage), stage=rec.stage, now=now)


def answer_path(rec_path: Path) -> Path:
    """``<item-encoded>.answer.md`` next to the run record; the user writes the answer here."""
    return rec_path.with_suffix(ANSWER_SUFFIX)


def answers_log_path(rec_path: Path) -> Path:
    """``<item-encoded>.answers.jsonl`` next to the run record: the append-only log of consumed answers."""
    return rec_path.with_suffix(ANSWERS_LOG_SUFFIX)


def build_answer_log_record(rec_path: Path, item: str, answer: str) -> dict[str, Any]:
    """One answers-log entry: the answer plus what the stop record says it answered (never raises)."""
    stop = read_stop_record(rec_path)
    questions: Any = () if stop is None else stop.questions
    if isinstance(questions, str):
        # DESIGN DECISION (OD-1): a bare string (legacy or hand-written sidecar) is NOT split; it becomes
        # one question, and an empty string becomes none.
        questions = [questions] if questions else []
    return {
        "item": item,
        "checkpoint": stop.checkpoint if stop else None,
        "stage": stop.stage if stop else None,
        "stage_status": stop.stage_status if stop else None,
        "leaf": getattr(stop, "leaf", None),
        "questions": list(questions or ()),
        "detail": stop.detail if stop else None,
        "stopped_at": stop.stopped_at if stop else None,
        "answer": answer.strip(),
        "answered_at": datetime.now(UTC).isoformat(),
    }


def _append_line(path: Path, line: str) -> None:
    """Durably append exactly one newline-terminated line to the JSONL file ``path``.

    ``line`` is passed without a trailing newline; one is added (a single supplied
    trailing newline is not doubled). The parent directory is created. If the
    existing file is non-empty and does not end in a newline (a partial last
    line left by a crash or a failed write), the file is first truncated back to
    the last newline (or to empty), so the partial bytes never become a mid-file
    line. The new record is then written in one ``write`` call, followed by
    ``flush`` and ``os.fsync``. Any ``OSError`` propagates unchanged.

    Reader tolerance (FR-12): readers of the answers log need only skip a final
    line that does not parse; the append path never leaves an unparseable line
    anywhere else.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "r+b") as fh:
            data = fh.read()
            if data and not data.endswith(b"\n"):
                fh.truncate(data.rfind(b"\n") + 1)
                fh.flush()
                os.fsync(fh.fileno())
    except FileNotFoundError:
        pass
    body = line[:-1] if line.endswith("\n") else line
    with open(path, "ab") as fh:
        fh.write((body + "\n").encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())


def append_answer_log(rec_path: Path, item: str, answer: str) -> None:
    """Log one answer as a single JSON line (does not rename the answer file).

    The log is append-only and nothing deletes it. Reader tolerance (FR-12): a
    reader need only skip a final line that does not parse (a crash may leave a
    truncated last line); the next append truncates it away first.
    """
    record = build_answer_log_record(rec_path, item, answer)
    _append_line(answers_log_path(rec_path), json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def read_answer(rec_path: Path) -> str:
    try:
        return answer_path(rec_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def consume_answer(rec_path: Path, item: str, answer: str) -> None:
    """Log the answer, then rename the answer file to ``*.answer.md.used`` so a stale answer does not clear a later stop.

    The answers-log line is appended first (FR-13); an OSError from it propagates and the
    answer file stays un-renamed (FR-18). A crash between the two steps can only duplicate a line.

    ``answer`` is what the agent saw; the file may have been edited while the agent ran. The file is
    re-read here, and if its content differs, that content is logged too (after ``answer``), so the text
    that is renamed to ``.used`` is always in the log.
    """
    path = answer_path(rec_path)
    if path.is_file():
        append_answer_log(rec_path, item, answer)
        current = read_answer(rec_path)
        if current and current != answer.strip():
            append_answer_log(rec_path, item, current)
        path.replace(path.with_name(path.name + USED_SUFFIX))


def iter_records(workspace: Path) -> Iterator[RunRecord]:
    """Valid run records in ``workspace`` (sorted by file name); sidecars and foreign files are ignored."""
    if not workspace.is_dir():
        return
    for path in sorted(workspace.glob("*.json")):
        if path.name.endswith(SIDECAR_SUFFIXES):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        item = data.get("item") if isinstance(data, dict) else None
        if not isinstance(item, str) or record_path(workspace, item) != path:
            continue
        yield RunRecord.open(workspace, item)


def is_stopped(rec: RunRecord, stop: StopRecord | None) -> bool:
    """Stopped at a checkpoint or on an error. An abandoned item is not stopped: nothing resumes it."""
    if is_abandoned(stop):
        return False
    return rec.checkpoint is not None or stop is not None


def is_parked(rec: RunRecord, stop: StopRecord | None) -> bool:
    """A clean intake left this record: intake complete, no checkpoint, no stop record, not abandoned.

    ``resume`` continues it from the plan stage. A hard-killed record (stage in progress) is not parked.
    """
    return (
        not is_abandoned(stop)
        and rec.stage == INTAKE_STAGE
        and rec.stage_status == STAGE_COMPLETE
        and rec.checkpoint is None
        and stop is None
    )


def _stopped_at(rec: RunRecord, stop: StopRecord | None) -> datetime:
    for text in (stop.stopped_at if stop else "", rec.updated_at):
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            continue
    return datetime.min.replace(tzinfo=UTC)


def list_waiting(workspace: Path) -> list[tuple[RunRecord, StopRecord | None]]:
    """Items stopped at a checkpoint (record checkpoint set), oldest stop first."""
    pairs = [(r, read_stop_record(r.path)) for r in iter_records(workspace) if r.checkpoint is not None]
    found = [(r, stop) for r, stop in pairs if not is_abandoned(stop)]
    return sorted(found, key=lambda pair: _stopped_at(*pair))


def cleared(rec: RunRecord, stop: StopRecord | None, config: SisyphusConfig, now: datetime) -> bool:
    """True when the blocker of the stop at ``rec.checkpoint`` is cleared and an automatic resume may start."""
    how = CLEARS_BY.get(rec.checkpoint, "explicit")
    if how == "brief":
        try:
            brief = resolve_brief_path(config, rec.item)
            return brief.is_file() and not parse_open_decisions(brief.read_text(encoding="utf-8"))
        except (IntakeError, OSError):
            return False
    if how == "answer":
        return bool(read_answer(rec.path))
    if how == "retry":
        if rec.retries.get(AUTO_RETRY_KEY, 0) >= AUTO_RETRY_MAX:
            return False
        return now - _stopped_at(rec, stop) >= timedelta(minutes=AUTO_RETRY_DELAY_MINUTES)
    return False
