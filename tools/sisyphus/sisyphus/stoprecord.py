"""Stop record: a sidecar next to the run record that says where an item stopped and what to do.

The run record (Phase 10 schema) is not touched. The sidecar lives at ``<record path>.stopped.json`` and
carries the stage, the checkpoint (or an unexpected error), the questions or blocker, the exact action
the user must take and the exact resume command. It is written when a run stops and removed when a run
completes. Retry counters live only in the run record. Standard library only.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STOP_RECORD_SUFFIX = ".stopped.json"
KIND_CHECKPOINT = "checkpoint"
KIND_ERROR = "error"
ERROR_LABEL = "error"

# How a stop clears: "brief" (re-parse the brief), "answer" (non-empty answer file), "retry" (bounded
# automatic retry after a delay) or "explicit" (only `sisyphus resume`). Ambiguous cases are explicit.
CLEARS_BY: dict[str | None, str] = {
    "C1": "brief",
    "C2": "answer",
    "C3": "answer",
    "C4": "answer",
    "C8": "retry",
}
AUTO_RETRY_DELAY_MINUTES = 30
AUTO_RETRY_MAX = 3

_RESUME = "The next scheduled run resumes it by itself; or run the resume command now."
ACTIONS: dict[str, str] = {
    "C1": "Edit the brief at {brief}: answer each open decision and mark it resolved. " + _RESUME,
    "C2": "Write your answer to the unresolved plan questions in {answer} (any non-empty text). " + _RESUME,
    "C3": "Write your answer to the leaf's open question (see the detail) in {answer}. " + _RESUME,
    "C4": "Write in {answer} whether to accept the leaf's deviation or how to redirect the later leaves. " + _RESUME,
    "C5": "Decide how to proceed (change the leaf or the config, or fix the cause), then run the resume command. "
    "Not automatic. The retry budget is kept, so an exhausted build budget stops again at C5.",
    "C6": "Raise the exceeded limit in .sisyphus.toml (a time limit needs nothing), then run the resume command. "
    "Not automatic.",
    "C7": "Rebase the branch yourself, then run the resume command. Not automatic.",
    "C8": f"Fix access (auth or network) if needed. The run retries by itself after {AUTO_RETRY_DELAY_MINUTES} "
    f"minutes, at most {AUTO_RETRY_MAX} times; after that run the resume command.",
}
ERROR_ACTION = "Fix the cause of the error shown in the detail, then run the resume command. Not automatic."


@dataclass(frozen=True)
class StopRecord:
    item: str
    kind: str
    stage: str | None
    stage_status: str | None
    checkpoint: str | None
    questions: tuple[str, ...]
    detail: str
    action: str
    resume_command: str
    stopped_at: str

    @property
    def clears_by(self) -> str:
        return CLEARS_BY.get(self.checkpoint, "explicit")

    @property
    def label(self) -> str:
        """``C1``..``C8``, or ``error``."""
        return self.checkpoint or ERROR_LABEL

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "kind": self.kind,
            "stage": self.stage,
            "stage_status": self.stage_status,
            "checkpoint": self.checkpoint,
            "questions": list(self.questions),
            "detail": self.detail,
            "action": self.action,
            "clears_by": self.clears_by,
            "resume_command": self.resume_command,
            "stopped_at": self.stopped_at,
        }


def stop_record_path(record_path: Path) -> Path:
    return record_path.with_name(record_path.name + STOP_RECORD_SUFFIX)


def action_for(checkpoint: str | None, brief_path: str = "the brief", answer_path: str = "the answer file") -> str:
    if checkpoint is None:
        return ERROR_ACTION
    text = ACTIONS.get(checkpoint, "Resolve the stop described in the detail, then run the resume command.")
    return text.format(brief=brief_path, answer=answer_path)


def build_stop_record(
    item: str,
    *,
    checkpoint: str | None,
    stage: str | None,
    stage_status: str | None,
    detail: str,
    resume_command: str,
    questions: tuple[str, ...] = (),
    brief_path: str = "the brief",
    answer_path: str = "the answer file",
    now: datetime | None = None,
) -> StopRecord:
    return StopRecord(
        item=item,
        kind=KIND_CHECKPOINT if checkpoint else KIND_ERROR,
        stage=stage,
        stage_status=stage_status,
        checkpoint=checkpoint,
        questions=questions or ((detail,) if detail else ()),
        detail=detail,
        action=action_for(checkpoint, brief_path, answer_path),
        resume_command=resume_command,
        stopped_at=(now or datetime.now(UTC)).isoformat(),
    )


def write_stop_record(record_path: Path, stop: StopRecord) -> Path:
    """Atomically write the sidecar for the run record at ``record_path``."""
    path = stop_record_path(record_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=STOP_RECORD_SUFFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(stop.to_dict(), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def read_stop_record(record_path: Path) -> StopRecord | None:
    """The stop record for the run record at ``record_path``; ``None`` when absent or unreadable."""
    try:
        data = json.loads(stop_record_path(record_path).read_text(encoding="utf-8"))
        return StopRecord(
            item=str(data["item"]),
            kind=str(data["kind"]),
            stage=data.get("stage"),
            stage_status=data.get("stage_status"),
            checkpoint=data.get("checkpoint"),
            questions=tuple(str(q) for q in data.get("questions", [])),
            detail=str(data.get("detail", "")),
            action=str(data.get("action", "")),
            resume_command=str(data.get("resume_command", "")),
            stopped_at=str(data.get("stopped_at", "")),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def clear_stop_record(record_path: Path) -> None:
    stop_record_path(record_path).unlink(missing_ok=True)
