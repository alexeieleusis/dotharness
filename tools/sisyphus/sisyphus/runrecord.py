"""Per-item run record: small JSON state that lets an unattended run resume.

Standard library only; deliberately independent of the config loader and of every
other Sisyphus or spec-prism-flow module. Stage and checkpoint names are opaque strings owned by the
caller. One record file lives under the caller-supplied workspace directory for
each item reference. Writes are atomic (temp file in the same directory, then
rename. There is no file locking. Retry counters are never reset on load or by any automatic path; the only
way to zero them is the explicit :meth:`RunRecord.reset_retries`, which also appends an event to the optional
``resets`` list (additive field, absent from records that never had a reset).
"""

# ruff: noqa: TRY003
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_STATE_DIR = "~/.local/share/dotharness/sisyphus"

STAGE_IN_PROGRESS = "in_progress"
STAGE_COMPLETE = "complete"
STAGE_STATUSES = (STAGE_IN_PROGRESS, STAGE_COMPLETE)

RECORD_SUFFIX = ".json"
_MAX_ENCODED = 120
_LITERAL = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


class RunRecordError(Exception):
    """Raised for a corrupt, truncated or otherwise unusable run record."""


def encode_item(item: str) -> str:
    """Encode an item reference as a filesystem-safe, case-insensitive-safe stem.

    Injective: bytes in ``[a-z0-9-]`` stay literal, every other UTF-8 byte
    (including ``_``, upper case, ``/`` and ``.``) becomes ``_XX`` lower hex.
    References whose encoding would be long get a truncated prefix plus the full
    SHA-256 of the reference, which keeps names within filesystem limits.
    """
    if not isinstance(item, str) or not item:
        raise ValueError("item reference must be a non-empty string")
    raw = item.encode("utf-8")
    encoded = "".join(chr(b) if chr(b) in _LITERAL and b < 128 else f"_{b:02x}" for b in raw)
    if len(encoded) <= _MAX_ENCODED:
        return encoded
    return f"{encoded[: _MAX_ENCODED - 70]}__{hashlib.sha256(raw).hexdigest()}"


def record_path(workspace_dir: str | os.PathLike[str], item: str) -> Path:
    """Return the record file path for ``item`` under ``workspace_dir`` (verbatim, no ``~`` expansion)."""
    return Path(workspace_dir) / f"{encode_item(item)}{RECORD_SUFFIX}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RunRecord:
    """State for one item. Obtain with :meth:`open`; mutators persist immediately."""

    def __init__(
        self,
        path: Path,
        item: str,
        stage: str | None,
        stage_status: str | None,
        checkpoint: str | None,
        started_at: str,
        updated_at: str,
        retries: dict[str, int],
        resets: list[dict[str, Any]] | None = None,
    ) -> None:
        self.path = path
        self.item = item
        self.stage = stage
        self.stage_status = stage_status
        self.checkpoint = checkpoint
        self.started_at = started_at
        self.updated_at = updated_at
        self.retries = retries
        self.resets: list[dict[str, Any]] = resets if resets is not None else []

    @classmethod
    def open(cls, workspace_dir: str | os.PathLike[str], item: str) -> RunRecord:
        """Load the existing record for ``item`` or create and persist a fresh one."""
        path = record_path(workspace_dir, item)
        if path.exists():
            return cls._load(path, item)
        now = _now()
        record = cls(path, item, None, None, None, now, now, {})
        record.save()
        return record

    @classmethod
    def _load(cls, path: Path, item: str) -> RunRecord:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RunRecordError(f"run record {path} is unreadable or corrupt: {exc}") from exc
        problem = _validate(data, item)
        if problem:
            raise RunRecordError(f"run record {path} is invalid: {problem}")
        return cls(
            path,
            data["item"],
            data["stage"],
            data["stage_status"],
            data["checkpoint"],
            data["started_at"],
            data["updated_at"],
            dict(data["retries"]),
            [dict(e) for e in data.get("resets", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "item": self.item,
            "stage": self.stage,
            "stage_status": self.stage_status,
            "checkpoint": self.checkpoint,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "retries": dict(self.retries),
        }
        if self.resets:
            data["resets"] = [dict(e) for e in self.resets]
        return data

    def save(self) -> None:
        """Atomically write the record (temp file in the same directory, then rename)."""
        self.updated_at = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-", suffix=RECORD_SUFFIX)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def mark_stage(self, stage: str, status: str = STAGE_IN_PROGRESS) -> None:
        """Record ``stage`` with ``status`` (``in_progress`` or ``complete``)."""
        if not isinstance(stage, str) or not stage:
            raise ValueError("stage must be a non-empty string")
        if status not in STAGE_STATUSES:
            raise ValueError(f"status must be one of {STAGE_STATUSES}")
        self.stage = stage
        self.stage_status = status
        self.save()

    @contextmanager
    def stage_scope(self, stage: str) -> Generator[RunRecord, None, None]:
        """Mark ``stage`` in progress before the body and complete after it returns.

        If the body raises, the record keeps naming the stage as in progress.
        """
        self.mark_stage(stage, STAGE_IN_PROGRESS)
        yield self
        self.mark_stage(stage, STAGE_COMPLETE)

    def set_checkpoint(self, checkpoint: str | None) -> None:
        """Record the raised checkpoint, or ``None`` to clear it."""
        if checkpoint is not None and (not isinstance(checkpoint, str) or not checkpoint):
            raise ValueError("checkpoint must be a non-empty string or None")
        self.checkpoint = checkpoint
        self.save()

    def increment_retry(self, key: str) -> int:
        """Add one to the counter ``key`` (starting from 0), persist, and return the new value."""
        if not isinstance(key, str) or not key:
            raise ValueError("retry key must be a non-empty string")
        self.retries[key] = self.retries.get(key, 0) + 1
        self.save()
        return self.retries[key]

    def reset_retries(self, keys: Iterable[str], *, stage: str | None, now: datetime | None = None) -> dict[str, int]:
        """Explicitly zero the counters in ``keys`` that are above zero, record the event, persist.

        Only the named keys change; everything else (other counters, stage, checkpoint) is preserved. The
        event ``{"at", "stage", "keys": {key: {"from": n, "to": 0}}}`` is appended to ``resets`` even when
        nothing needed resetting (``keys`` empty), so a reset is always visible. Returns ``{key: previous}``
        for the counters that changed. Never called on load; callers decide which keys belong to a stage.
        """
        changed: dict[str, int] = {}
        for key in dict.fromkeys(keys):
            previous = self.retries.get(key, 0)
            if previous > 0:
                changed[key] = previous
                self.retries[key] = 0
        self.resets.append({
            "at": (now or datetime.now(UTC)).isoformat(),
            "stage": stage,
            "keys": {k: {"from": v, "to": 0} for k, v in changed.items()},
        })
        self.save()
        return changed


def _valid_resets(resets: Any) -> bool:
    return isinstance(resets, list) and all(
        isinstance(e, dict)
        and isinstance(e.get("at"), str)
        and (e.get("stage") is None or isinstance(e.get("stage"), str))
        and isinstance(e.get("keys"), dict)
        for e in resets
    )


def _validate(data: Any, item: str) -> str | None:
    if not isinstance(data, dict):
        return "top level is not a JSON object"
    for key in ("item", "stage", "stage_status", "checkpoint", "started_at", "updated_at", "retries"):
        if key not in data:
            return f"missing field {key!r}"
    if data["item"] != item:
        return f"item reference {data['item']!r} does not match {item!r}"
    problem = _validate_scalars(data)
    if problem:
        return problem
    retries = data["retries"]
    if not isinstance(retries, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0 for k, v in retries.items()
    ):
        return "field 'retries' must map strings to non-negative integers"
    if "resets" in data and not _valid_resets(data["resets"]):
        return "field 'resets' must be a list of reset events"
    return None


def _validate_scalars(data: dict[str, Any]) -> str | None:
    for key in ("stage", "checkpoint"):
        if data[key] is not None and not isinstance(data[key], str):
            return f"field {key!r} must be a string or null"
    if data["stage_status"] is not None and data["stage_status"] not in STAGE_STATUSES:
        return "field 'stage_status' has an unknown value"
    for key in ("started_at", "updated_at"):
        if not isinstance(data[key], str):
            return f"field {key!r} must be a string"
    return None
