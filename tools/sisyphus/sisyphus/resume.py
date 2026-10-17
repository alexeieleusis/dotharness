"""Resume support: list stopped items, detect that a stop is cleared, locate the answer file.

Cheap and deterministic: no agent call, no network. A stop clears when (see ``stoprecord.CLEARS_BY``)
the brief no longer has blocking open decisions (C1), the user wrote a non-empty answer file (C2..C4),
or a bounded retry delay has passed (C8). Everything else (C5..C7, errors, anything unknown) needs an
explicit ``sisyphus resume``. Imports only Package A modules.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sisyphus.config import SisyphusConfig
from sisyphus.intake import IntakeError, parse_open_decisions, resolve_brief_path
from sisyphus.runrecord import RunRecord, record_path
from sisyphus.stages import (
    AUTO_RETRY_KEY,
    BUILD_RETRY_KEY,
    BUILD_STAGE,
    CI_FIX_ATTEMPTS_KEY,
    CI_FIX_STAGE,
    INTAKE_STAGE,
    MERGE_STAGE,
    OPEN_PR_STAGE,
    PLAN_STAGE,
    PUSH_STAGE,
)
from sisyphus.stoprecord import (
    AUTO_RETRY_DELAY_MINUTES,
    AUTO_RETRY_MAX,
    CLEARS_BY,
    StopRecord,
    is_abandoned,
    read_stop_record,
)

SIDECAR_SUFFIXES = (".stop.json", ".intake.json", ".stopped.json")
ANSWER_SUFFIX = ".answer.md"
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
    BUILD_STAGE: (BUILD_RETRY_KEY, AUTO_RETRY_KEY),
    PUSH_STAGE: (AUTO_RETRY_KEY,),
    OPEN_PR_STAGE: (AUTO_RETRY_KEY,),
    CI_FIX_STAGE: (CI_FIX_ATTEMPTS_KEY, AUTO_RETRY_KEY),
    MERGE_STAGE: (AUTO_RETRY_KEY,),
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


def read_answer(rec_path: Path) -> str:
    try:
        return answer_path(rec_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def consume_answer(rec_path: Path) -> None:
    """Rename the answer file to ``*.answer.md.used`` so a stale answer does not clear a later stop."""
    path = answer_path(rec_path)
    if path.is_file():
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
