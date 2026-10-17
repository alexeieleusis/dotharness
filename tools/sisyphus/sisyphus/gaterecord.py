"""Per-PR gate record: the file the Package B merge runner reads before it merges.

Standard library and the stdlib-only ``sisyphus.runrecord`` only (never imports ``harness``). The location and the ``head_sha`` key match
``harness/runners/merge.py`` (``gate_record_path`` / ``read_gate_record``):
``<XDG_DATA>/state/<repo_slug>/build_gate_<PR>.json`` with ``XDG_DATA = ~/.local/share/dotharness``.
The merge runner reads only ``head_sha`` and ignores other keys, so ``commands`` and ``passed_at``
are informational. The record vouches for exactly one head SHA; any later push makes it stale.
"""

# ruff: noqa: TRY003
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sisyphus.runrecord import atomic_write_json

_SLUG = re.compile(r"^[a-zA-Z0-9._-]+$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def xdg_data() -> Path:
    """``~/.local/share/dotharness``, evaluated at call time (the harness evaluates it at import)."""
    return Path.home() / ".local/share/dotharness"


def repo_slug_from_name(name: str) -> str:
    """Hyphenated ``owner-repo`` slug: the harness rule ``name.replace("/", "-")`` for ``repo.name``."""
    return name.replace("/", "-")


def fallback_repo_slug(working_dir: str | os.PathLike[str]) -> str:
    """Slug for a config without ``repo.name``: sanitised basename, ``-``, 8 hex of SHA-256 of the resolved path.

    Mirrors ``harness.config._fallback_repo_slug`` without importing it.
    """
    resolved = Path(working_dir).expanduser().resolve()
    base = re.sub(r"[^a-zA-Z0-9._-]", "-", resolved.name).strip(".-") or "repo"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def validate_repo_slug(slug: str) -> str:
    """Return ``slug`` or raise ``ValueError`` unless it is one path segment of ``[a-zA-Z0-9._-]``."""
    if not _SLUG.match(slug):
        raise ValueError(f"Invalid repo slug: {slug!r}")
    return slug


def gate_record_path(repo_slug: str, pr_number: int, state_dir: str | os.PathLike[str] | None = None) -> Path:
    """Location of the gate record; creates nothing.

    ``state_dir`` replaces ``<XDG_DATA>/state`` (the directory that holds one folder per repo slug).
    """
    base = Path(state_dir) if state_dir is not None else xdg_data() / "state"
    return base / validate_repo_slug(repo_slug) / f"build_gate_{pr_number}.json"


def read_gate_record(repo_slug: str, pr_number: int, state_dir: str | os.PathLike[str] | None = None) -> str | None:
    """Head SHA the record vouches for, or ``None`` when it is missing or unreadable (merge.py rule)."""
    try:
        data = json.loads(gate_record_path(repo_slug, pr_number, state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sha = data.get("head_sha") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) and sha else None


def write_gate_record(
    repo_slug: str,
    pr_number: int,
    head_sha: str,
    state_dir: str | os.PathLike[str] | None = None,
    commands: Sequence[str] | None = None,
) -> Path:
    """Atomically write the record for ``head_sha`` (full 40-character SHA) and return its path.

    Writes a temp file in the same directory, fsyncs it, then ``os.replace``. Call it only after
    every gate command passed at exactly ``head_sha``.
    """
    if not _SHA.match(head_sha):
        raise ValueError(f"head_sha must be a full 40-character hex SHA, got {head_sha!r}")
    path = gate_record_path(repo_slug, pr_number, state_dir)
    data: dict[str, object] = {"head_sha": head_sha.lower()}
    if commands is not None:
        data["commands"] = list(commands)
    data["passed_at"] = datetime.now(UTC).isoformat()
    atomic_write_json(path, data, indent=None)
    return path
