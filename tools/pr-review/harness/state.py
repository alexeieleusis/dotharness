import fcntl
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

XDG_DATA = Path.home() / ".local/share/dotharness"

VIBE_HEAL_FILE = "vibe_heal.json"
SELF_REVIEW_FILE = "self_review.json"

_COMMAND_FILES = {
    "review-prs": VIBE_HEAL_FILE,
    "self-review": SELF_REVIEW_FILE,
}


def _validate_repo_slug(slug: str) -> str:
    # HarnessConfig.repo_slug hyphenates "owner/repo" into a single path segment
    # (e.g. "Oscilar-backend") before it reaches here, so no "/" is expected.
    if not re.match(r"^[a-zA-Z0-9._-]+$", slug):
        raise ValueError(f"Invalid repo slug: {slug!r}")  # noqa: TRY003
    return slug


def _state_path(repo_slug: str, filename: str) -> Path:
    _validate_repo_slug(repo_slug)
    p = XDG_DATA / "state" / repo_slug / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.rename(path)


class _state_lock:
    """Acquire an exclusive flock on a dedicated lock file next to the state file.

    Locking a separate file (rather than the state file itself) avoids the side
    effect of `open(path, "a")` creating the state file on disk before its first
    real write, which would make callers see it as an existing-but-empty file.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.with_suffix(path.suffix + ".lock")

    def __enter__(self):
        self._fd = open(self.path, "a")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args: object) -> None:
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()


def read_vibe_heal_state(repo_slug: str) -> dict:
    defaults = {"version": 1, "last_pr": 0, "last_main_sha": ""}
    p = _state_path(repo_slug, VIBE_HEAL_FILE)
    if not p.exists():
        return defaults
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("Corrupted state file %s: %s — returning defaults", p, exc)
        return defaults
    data.setdefault("last_main_sha", "")
    return data


def write_vibe_heal_state(repo_slug: str, last_pr: int | None = None, *, last_main_sha: str | None = None) -> None:
    if last_pr is None and last_main_sha is None:
        raise ValueError("write_vibe_heal_state called with no fields to update")  # noqa: TRY003
    p = _state_path(repo_slug, VIBE_HEAL_FILE)
    with _state_lock(p):
        current = read_vibe_heal_state(repo_slug)
        if last_pr is not None:
            current["last_pr"] = last_pr
        if last_main_sha is not None:
            current["last_main_sha"] = last_main_sha
        _atomic_write(p, current)


def read_self_review_state(repo_slug: str) -> dict:
    defaults = {"version": 1, "reviewed_prs": [], "partial_reviews": {}}
    p = _state_path(repo_slug, SELF_REVIEW_FILE)
    if not p.exists():
        return defaults
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("Corrupted state file %s: %s — returning defaults", p, exc)
        return defaults
    data.setdefault("reviewed_prs", [])
    data.setdefault("partial_reviews", {})
    return data


def write_self_review_state(repo_slug: str, reviewed_prs: list[int], partial_reviews: dict | None = None) -> None:
    p = _state_path(repo_slug, SELF_REVIEW_FILE)
    with _state_lock(p):
        current = read_self_review_state(repo_slug)
        if partial_reviews is not None:
            current["partial_reviews"] = partial_reviews
        _atomic_write(
            p,
            {
                "version": current["version"],
                "reviewed_prs": reviewed_prs,
                "partial_reviews": current["partial_reviews"],
            },
        )


def get_partial_reviewed_files(repo_slug: str, pr_number: int) -> list[str]:
    data = read_self_review_state(repo_slug)
    return list(data.get("partial_reviews", {}).get(str(pr_number), []))


def set_partial_reviewed_files(repo_slug: str, pr_number: int, files: list[str]) -> None:
    p = _state_path(repo_slug, SELF_REVIEW_FILE)
    with _state_lock(p):
        current = read_self_review_state(repo_slug)
        current["partial_reviews"][str(pr_number)] = list(files)
        _atomic_write(p, current)


def delete_state(repo_slug: str, command: str) -> None:
    if command not in _COMMAND_FILES:
        raise ValueError(f"No state file for command: {command}")  # noqa: TRY003
    p = _state_path(repo_slug, _COMMAND_FILES[command])
    if p.exists():
        p.unlink()
