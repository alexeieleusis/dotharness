"""Digest writer: run records and caller-supplied report entries in, one Markdown file out.

Six sections, always present and always in the same order (see :data:`SECTIONS`); an empty
section says ``None``. The digest is rendered from whatever stages the records reached, so a run
that failed early still produces a complete document. Evidence strings, PRs, commits and timings
are supplied by the caller (the run record does not carry them); the digest renders them verbatim
and defines no gate-record format.

Imports only Package A modules (never ``harness`` or ``spec_prism_flow``).
"""

# ruff: noqa: TRY003
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sisyphus.config import SisyphusConfig
from sisyphus.rails import Checkpoint
from sisyphus.runrecord import STAGE_COMPLETE, RunRecord
from sisyphus.stages import CI_FIX_STAGE, MERGE_STAGE
from sisyphus.stoprecord import StopRecord, read_stop_record

SECTIONS = ("Needs you", "Done", "Declined", "Deviations", "Limits hit", "Cost and time")

CHECKPOINT_QUESTIONS: dict[str, str] = {
    Checkpoint.C1.value: "The brief has open decisions that block the requirements doc. Answer them in the brief, then resume.",
    Checkpoint.C2.value: "OPEN_QUESTIONS.md has unresolved entries after draft-overview. Answer them, then resume.",
    Checkpoint.C3.value: "A leaf reported an open question. Answer it, then resume.",
    Checkpoint.C4.value: "A leaf reported a deviation that may change a later leaf (or the sub-agent could not tell). Accept it or redirect the later leaves?",
    Checkpoint.C5.value: "A leaf exhausted its retry cycles or violated its scope. Fix the cause by hand, or change the leaf?",
    Checkpoint.C6.value: "A hard run limit was hit. Raise the limit or accept the partial result?",
    Checkpoint.C7.value: "The PR branch cannot be updated without a rebase or force-push. Rebase it by hand?",
    Checkpoint.C8.value: "A GitHub or Linear call failed in a way the agent could not recover from. Fix access (auth, network), then resume?",
}


class DigestWriteError(Exception):
    """Raised when the digest file cannot be written (the CLI maps it to a non-zero exit)."""


@dataclass(frozen=True)
class NeedsYou:
    """An extra question for the human, rendered under "Needs you"."""

    item: str
    checkpoint: str
    question: str
    action: str = ""
    resume: str = ""


@dataclass(frozen=True)
class DoneEntry:
    """A PR opened or updated, or a commit, with the evidence strings the caller supplies."""

    summary: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeclinedEntry:
    """A CI failure that was not auto-fixed, with the reason."""

    summary: str
    reason: str


@dataclass(frozen=True)
class StageTiming:
    """Wall-clock seconds for one stage, and tokens when the backend reported them."""

    item: str
    stage: str
    seconds: float
    tokens: int | None = None


@dataclass(frozen=True)
class DigestReport:
    """Caller-supplied digest content that run records do not carry."""

    needs_you: tuple[NeedsYou, ...] = ()
    done: tuple[DoneEntry, ...] = ()
    declined: tuple[DeclinedEntry, ...] = ()
    deviations: tuple[str, ...] = ()
    limits_hit: tuple[str, ...] = ()
    timings: tuple[StageTiming, ...] = ()


def _numbered(lines: Sequence[str]) -> list[str]:
    return [f"{i}. {line}" for i, line in enumerate(lines, start=1)] or ["None"]


def _bullets(lines: Sequence[str]) -> list[str]:
    return [f"- {line}" for line in lines] or ["None"]


def _stage_desc(rec: RunRecord) -> str:
    if rec.stage is None:
        return "no stage reached"
    return f"{rec.stage} ({rec.stage_status or 'unknown'})"


ERROR_QUESTION = "The run stopped on an unexpected error. Can you fix the cause shown below?"


def needs_from_stop(stop: StopRecord) -> NeedsYou:
    """Render a stop record as a Needs-you entry: question, the action to take and the resume command."""
    if stop.checkpoint is None:
        question = f"{ERROR_QUESTION} Detail: {stop.detail}"
    else:
        base = CHECKPOINT_QUESTIONS.get(stop.checkpoint, "The run stopped at this checkpoint. How should it proceed?")
        question = f"{base} Detail: {stop.detail}"
    return NeedsYou(stop.item, stop.label, question, stop.action, stop.resume_command)


def needs_from_record(rec: RunRecord) -> NeedsYou | None:
    """Needs-you entry for a run record: its stop record when present, else the built-in question."""
    stop = read_stop_record(rec.path)
    if stop is not None and stop.abandoned:
        return None
    if stop is not None and (stop.checkpoint == rec.checkpoint or stop.checkpoint is None):
        return needs_from_stop(stop)
    if rec.checkpoint is None:
        return None
    question = CHECKPOINT_QUESTIONS.get(rec.checkpoint, "The run stopped at this checkpoint. How should it proceed?")
    return NeedsYou(rec.item, rec.checkpoint, f"{question} Stage reached: {_stage_desc(rec)}.")


def _needs_line(n: NeedsYou) -> str:
    line = f"[{n.checkpoint}] {n.item}: {n.question}"
    if n.action:
        line += f" Action: {n.action}"
    if n.resume:
        line += f" Resume: `{n.resume}`"
    return line


def _needs_you(records: Sequence[RunRecord], report: DigestReport) -> list[str]:
    lines = [_needs_line(n) for n in report.needs_you]
    covered = {(n.item, n.checkpoint) for n in report.needs_you}
    for rec in records:
        need = needs_from_record(rec)
        if need is None or (need.item, need.checkpoint) in covered:
            continue
        lines.append(_needs_line(need))
    return lines


def _abandoned(records: Sequence[RunRecord]) -> list[str]:
    """Short notes for abandoned items (not "Needs you": nothing is waiting on the user)."""
    lines: list[str] = []
    for rec in records:
        stop = read_stop_record(rec.path)
        if stop is not None and stop.abandoned:
            lines.append(
                f"Abandoned: {rec.item} on {stop.stopped_at} (stage {_stage_desc(rec)}). Not picked or resumed again; "
                f"branch and any PR left as they are. Re-enable: `{stop.resume_command}`"
            )
    return lines


def describe_reset(item: str, event: dict[str, object]) -> str:
    """One line for a reset event of the run record: item, stage, time and each ``key previous->0``."""
    keys = event.get("keys")
    changes = (
        ", ".join(f"{k} {v.get('from')}->{v.get('to')}" for k, v in keys.items() if isinstance(v, dict))
        if isinstance(keys, dict)
        else ""
    )
    return f"Retry reset: {item} at stage {event.get('stage') or 'none'} on {event.get('at')}: " + (
        changes or "no counters to reset"
    )


def _resets(records: Sequence[RunRecord]) -> list[str]:
    return [describe_reset(rec.item, e) for rec in records for e in rec.resets]


def _retry_note(rec: RunRecord, stage: str) -> str:
    counts = {k: v for k, v in sorted(rec.retries.items()) if k.startswith(stage)}
    return " ".join(f"{k}={v}" for k, v in counts.items())


def _outcome_line(rec: RunRecord, stage: str) -> str:
    note = _retry_note(rec, stage)
    return f"{rec.item}: {stage} {rec.stage_status}" + (f" (retries: {note})" if note else "")


def _done(records: Sequence[RunRecord], report: DigestReport) -> list[str]:
    lines: list[str] = []
    for entry in report.done:
        evidence = "; ".join(entry.evidence) if entry.evidence else "no evidence supplied"
        lines.append(f"{entry.summary} (evidence: {evidence})")
    lines.extend(
        _outcome_line(rec, rec.stage)
        for rec in records
        if rec.stage in (CI_FIX_STAGE, MERGE_STAGE) and rec.stage_status == STAGE_COMPLETE
    )
    return lines


def _declined(records: Sequence[RunRecord], report: DigestReport) -> list[str]:
    lines = [f"{d.summary} (why: {d.reason})" for d in report.declined]
    lines.extend(
        _outcome_line(rec, rec.stage) + " and did not finish"
        for rec in records
        if rec.stage in (CI_FIX_STAGE, MERGE_STAGE) and rec.stage_status != STAGE_COMPLETE
    )
    return lines


def _limits(records: Sequence[RunRecord], report: DigestReport) -> list[str]:
    lines = list(report.limits_hit)
    lines.extend(
        f"{rec.item}: checkpoint {Checkpoint.C6.value} raised at stage {_stage_desc(rec)}"
        for rec in records
        if rec.checkpoint == Checkpoint.C6.value
    )
    return lines


def _elapsed(rec: RunRecord) -> str:
    try:
        seconds = (datetime.fromisoformat(rec.updated_at) - datetime.fromisoformat(rec.started_at)).total_seconds()
    except ValueError:
        return "unknown"
    return f"{max(seconds, 0):.0f}s"


def _cost(records: Sequence[RunRecord], report: DigestReport) -> list[str]:
    lines = [f"{rec.item}: stage reached {_stage_desc(rec)}; wall-clock {_elapsed(rec)} since start" for rec in records]
    for t in report.timings:
        tokens = f", {t.tokens} tokens" if t.tokens is not None else ", tokens not reported"
        lines.append(f"{t.item} / {t.stage}: {t.seconds:.0f}s{tokens}")
    return lines


def render_digest(records: Sequence[RunRecord], report: DigestReport | None = None) -> str:
    """Render the six-section Markdown digest. Empty sections say ``None``."""
    report = report or DigestReport()
    bodies = [
        _numbered(_needs_you(records, report)),
        _bullets(_done(records, report)),
        _bullets(_declined(records, report)),
        _bullets([*report.deviations, *_abandoned(records), *_resets(records)]),
        _bullets(_limits(records, report)),
        _bullets(_cost(records, report)),
    ]
    parts = ["# Sisyphus digest", ""]
    for title, body in zip(SECTIONS, bodies, strict=True):
        parts += [f"## {title}", "", *body, ""]
    return "\n".join(parts)


def resolve_digest_path(config: SisyphusConfig, today: datetime | None = None) -> Path:
    """Expand ``[digest].path_template`` (placeholders ``<date>`` and ``<workspace_dir>``)."""
    template = config.digest.path_template
    if not template:
        raise DigestWriteError("[digest].path_template is not set")
    date = (today or datetime.now(UTC)).strftime("%Y-%m-%d")
    path = template.replace("<date>", date)
    if "<workspace_dir>" in path:
        if not config.project.workspace_dir:
            raise DigestWriteError("path_template uses <workspace_dir> but [project].workspace_dir is not set")
        path = path.replace("<workspace_dir>", config.project.workspace_dir)
    return Path(path).expanduser()


def write_digest(
    records: Sequence[RunRecord],
    config: SisyphusConfig,
    report: DigestReport | None = None,
    today: datetime | None = None,
) -> Path:
    """Write exactly one digest file and return its path; raise :class:`DigestWriteError` on failure."""
    path = resolve_digest_path(config, today)
    text = render_digest(records, report)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise DigestWriteError(f"cannot write digest to {path}: {exc}") from exc
    return path
