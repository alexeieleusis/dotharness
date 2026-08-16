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
    defaults = {"version": 1, "reviewed_shas": {}, "last_main_sha": ""}
    p = _state_path(repo_slug, VIBE_HEAL_FILE)
    if not p.exists():
        return defaults
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("Corrupted state file %s: %s — returning defaults", p, exc)
        return defaults
    data.setdefault("last_main_sha", "")
    data.setdefault("reviewed_shas", {})
    if any(not isinstance(entry, dict) for entry in data["reviewed_shas"].values()):
        data["reviewed_shas"] = {
            pr: (entry if isinstance(entry, dict) else {"sha": entry, "reviewed_at": 0})
            for pr, entry in data["reviewed_shas"].items()
        }
    return data


def _update_state(repo_slug: str, filename: str, read_fn, mutate) -> dict:
    """Read-modify-write a state file under an exclusive lock.

    `mutate` receives the current state dict and returns True if it changed anything;
    the file is only rewritten when it did. Returns the resulting state dict.
    """
    p = _state_path(repo_slug, filename)
    with _state_lock(p):
        current = read_fn(repo_slug)
        if mutate(current):
            _atomic_write(p, current)
        return current


def write_vibe_heal_state(repo_slug: str, *, last_main_sha: str) -> None:
    def mutate(current: dict) -> bool:
        current["last_main_sha"] = last_main_sha
        return True

    _update_state(repo_slug, VIBE_HEAL_FILE, read_vibe_heal_state, mutate)


def get_reviewed_sha(repo_slug: str, pr_number: int) -> str | None:
    """Return the head SHA this PR was last successfully reviewed at, or None if never."""
    entry = read_vibe_heal_state(repo_slug)["reviewed_shas"].get(str(pr_number))
    return entry["sha"] if entry is not None else None


def record_reviewed_sha(repo_slug: str, pr_number: int, sha: str, reviewed_at: float) -> None:
    """Mark a PR as successfully reviewed at the given head SHA and time, immediately and
    independently of any other PR in the same batch — this is what lets one perpetually-failing
    PR stop blocking credit for every other PR discovered alongside it."""

    def mutate(current: dict) -> bool:
        current["reviewed_shas"][str(pr_number)] = {"sha": sha, "reviewed_at": reviewed_at}
        return True

    _update_state(repo_slug, VIBE_HEAL_FILE, read_vibe_heal_state, mutate)


def prune_reviewed_shas(repo_slug: str, open_pr_numbers: set[int]) -> None:
    """Drop reviewed_shas entries for PRs that are no longer open, so the map doesn't
    grow unboundedly as PRs get closed/merged over time."""
    keep = {str(n) for n in open_pr_numbers}

    def mutate(current: dict) -> bool:
        to_drop = current["reviewed_shas"].keys() - keep
        for pr in to_drop:
            del current["reviewed_shas"][pr]
        return bool(to_drop)

    _update_state(repo_slug, VIBE_HEAL_FILE, read_vibe_heal_state, mutate)


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
    def mutate(current: dict) -> bool:
        current["reviewed_prs"] = reviewed_prs
        if partial_reviews is not None:
            current["partial_reviews"] = partial_reviews
        return True

    _update_state(repo_slug, SELF_REVIEW_FILE, read_self_review_state, mutate)


def get_partial_reviewed_files(repo_slug: str, pr_number: int) -> list[str]:
    data = read_self_review_state(repo_slug)
    return list(data.get("partial_reviews", {}).get(str(pr_number), []))


def set_partial_reviewed_files(repo_slug: str, pr_number: int, files: list[str]) -> None:
    def mutate(current: dict) -> bool:
        current["partial_reviews"][str(pr_number)] = list(files)
        return True

    _update_state(repo_slug, SELF_REVIEW_FILE, read_self_review_state, mutate)


def prune_self_review_state(repo_slug: str, open_pr_numbers: set[int]) -> dict:
    """Drop reviewed_prs / partial_reviews entries for PRs that are no longer open, so
    self_review.json doesn't grow unboundedly as the user's own PRs get merged/closed
    over time. Returns the resulting state dict."""
    keep = {str(n) for n in open_pr_numbers}

    def mutate(current: dict) -> bool:
        reviewed_after = [n for n in current["reviewed_prs"] if str(n) in keep]
        partial_after = {k: v for k, v in current["partial_reviews"].items() if k in keep}
        changed = reviewed_after != current["reviewed_prs"] or partial_after != current["partial_reviews"]
        current["reviewed_prs"] = reviewed_after
        current["partial_reviews"] = partial_after
        return changed

    return _update_state(repo_slug, SELF_REVIEW_FILE, read_self_review_state, mutate)


def delete_state(repo_slug: str, command: str) -> None:
    if command not in _COMMAND_FILES:
        raise ValueError(f"No state file for command: {command}")  # noqa: TRY003
    p = _state_path(repo_slug, _COMMAND_FILES[command])
    if p.exists():
        p.unlink()
