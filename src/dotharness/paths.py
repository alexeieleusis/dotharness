"""Shared filesystem locations (usable without importing ``dotharness.pr_review``)."""

import hashlib
import re
from importlib import resources
from pathlib import Path

DATA_DIR_TILDE = "~/.local/share/dotharness"  # the one literal (F-DUP-4); unexpanded for callers that expand lazily


def data_dir() -> Path:
    """`~/.local/share/dotharness`, expanded at call time."""
    return Path.home() / DATA_DIR_TILDE.removeprefix("~/")  # Path.home(), not expanduser: tests patch it


def default_knowledge_dir() -> Path:
    """`~/.harness/knowledge` when it exists, else the knowledge shipped inside the dotharness package (D-7)."""
    user_dir = Path("~/.harness/knowledge").expanduser()
    if user_dir.is_dir():
        return user_dir
    # Plain path from importlib.resources: the wheel is installed unpacked, so the traversable is a real directory.
    return Path(str(resources.files("dotharness") / "knowledge"))


def validate_repo_slug(slug: str) -> str:
    """Return `slug` if it is a safe single path segment, else raise `ValueError`."""
    if slug in {".", ".."} or not re.fullmatch(r"[a-zA-Z0-9._-]+", slug):
        raise ValueError(f"Invalid repo slug: {slug!r}")  # noqa: TRY003
    return slug


def build_gate_record_path(repo_slug: str, pr_number: int) -> Path:
    """`<data_dir>/state/<slug>/build_gate_<pr>.json`; pure, creates nothing, `data_dir()` read at call time."""
    validate_repo_slug(repo_slug)
    return data_dir() / "state" / repo_slug / f"build_gate_{pr_number}.json"


def fallback_repo_slug(working_dir: Path) -> str:
    """Directory-safe slug for a config without `repo.name`.

    Sanitized basename (anything outside [a-zA-Z0-9._-] becomes "-") plus "-" and the
    first 8 hex chars of the SHA-256 of the resolved path. Always matches
    ``^[a-zA-Z0-9._-]+$``.
    """
    resolved = working_dir.expanduser().resolve()
    base = re.sub(r"[^a-zA-Z0-9._-]", "-", resolved.name).strip(".-") or "repo"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"
