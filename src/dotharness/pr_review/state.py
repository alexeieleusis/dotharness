import copy
import json
import logging
import re
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from dotharness.fileio import atomic_write_text, file_lock
from dotharness.paths import data_dir

logger = logging.getLogger(__name__)

XDG_DATA = data_dir()

VIBE_HEAL_FILE = "vibe_heal.json"
SELF_REVIEW_FILE = "self_review.json"
ADDRESS_COMMENTS_FILE = "address_comments.json"

_CORRUPTED_STATE_MSG = "Corrupted state file %s: %s — returning defaults"

_COMMAND_FILES = {
    "review-prs": VIBE_HEAL_FILE,
    "self-review": SELF_REVIEW_FILE,
}


def _validate_repo_slug(slug: str) -> str:
    # HarnessConfig.repo_slug hyphenates "owner/repo" into a single path segment
    # (e.g. "Oscilar-backend") before it reaches here, so no "/" is expected.
    if slug in {".", ".."} or not re.fullmatch(r"[a-zA-Z0-9._-]+", slug):
        raise ValueError(f"Invalid repo slug: {slug!r}")  # noqa: TRY003
    return slug


def _state_path(repo_slug: str, filename: str) -> Path:
    _validate_repo_slug(repo_slug)
    p = XDG_DATA / "state" / repo_slug / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def _state_lock(path: Path) -> Generator[None]:
    """Hold an exclusive blocking flock on a dedicated lock file next to the state file.

    Locking a separate file (rather than the state file itself) avoids the side
    effect of `open(path, "a")` creating the state file on disk before its first
    real write, which would make callers see it as an existing-but-empty file.
    """
    with file_lock(path.with_suffix(path.suffix + ".lock"), blocking=True):
        yield


def _load_json_dict(p: Path, defaults: dict) -> dict:
    """Load a state file, falling back to `defaults` when it is missing, unreadable, corrupt,
    or not a JSON object. Each key in `defaults` is coerced to the default's type, so a
    wrong-shaped value (e.g. `"reviewed_shas": null`) can't crash every later run."""
    if not p.exists():
        return copy.deepcopy(defaults)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        logger.warning(_CORRUPTED_STATE_MSG, p, exc)
        return copy.deepcopy(defaults)
    if not isinstance(data, dict):
        logger.warning(_CORRUPTED_STATE_MSG, p, f"expected a JSON object, got {type(data).__name__}")
        return copy.deepcopy(defaults)
    for key, default in defaults.items():
        if not isinstance(data.get(key), type(default)):
            data[key] = copy.deepcopy(default)
    return data


def read_vibe_heal_state(repo_slug: str) -> dict:
    defaults = {"version": 1, "reviewed_shas": {}, "last_main_sha": ""}
    data = _load_json_dict(_state_path(repo_slug, VIBE_HEAL_FILE), defaults)
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
            atomic_write_text(p, json.dumps(current))
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
    defaults = {
        "version": 1,
        "reviewed_prs": [],
        "partial_reviews": {},
        "design_reviewed_prs": [],
        "traceability_reviewed_prs": [],
        "regret_reviewed_prs": [],
    }
    return _load_json_dict(_state_path(repo_slug, SELF_REVIEW_FILE), defaults)


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
    """Drop reviewed_prs / partial_reviews / design_reviewed_prs / traceability_reviewed_prs /
    regret_reviewed_prs entries for PRs that are no longer open, so self_review.json doesn't
    grow unboundedly as the user's own PRs get merged/closed over time. Returns the
    resulting state dict."""
    keep = {str(n) for n in open_pr_numbers}

    def mutate(current: dict) -> bool:
        reviewed_after = [n for n in current["reviewed_prs"] if str(n) in keep]
        partial_after = {k: v for k, v in current["partial_reviews"].items() if k in keep}
        design_reviewed_after = [n for n in current["design_reviewed_prs"] if str(n) in keep]
        traceability_reviewed_after = [n for n in current["traceability_reviewed_prs"] if str(n) in keep]
        regret_reviewed_after = [n for n in current["regret_reviewed_prs"] if str(n) in keep]
        changed = (
            reviewed_after != current["reviewed_prs"]
            or partial_after != current["partial_reviews"]
            or design_reviewed_after != current["design_reviewed_prs"]
            or traceability_reviewed_after != current["traceability_reviewed_prs"]
            or regret_reviewed_after != current["regret_reviewed_prs"]
        )
        current["reviewed_prs"] = reviewed_after
        current["partial_reviews"] = partial_after
        current["design_reviewed_prs"] = design_reviewed_after
        current["traceability_reviewed_prs"] = traceability_reviewed_after
        current["regret_reviewed_prs"] = regret_reviewed_after
        return changed

    return _update_state(repo_slug, SELF_REVIEW_FILE, read_self_review_state, mutate)


def get_design_reviewed_prs(repo_slug: str) -> set[int]:
    """Independent of reviewed_prs/partial_reviews by design: a design-review pass's
    completion is tracked separately so its own retry never forces (or is forced by)
    the per-file review's retry-from-scratch behavior. See also
    get_traceability_reviewed_prs, tracked the same way for the independent
    requirement-traceability pass (dotharness#4)."""
    return set(read_self_review_state(repo_slug).get("design_reviewed_prs", []))


def add_design_reviewed_pr(repo_slug: str, pr_number: int) -> None:
    def mutate(current: dict) -> bool:
        design_reviewed = set(current.get("design_reviewed_prs", []))
        if pr_number in design_reviewed:
            return False
        design_reviewed.add(pr_number)
        current["design_reviewed_prs"] = sorted(design_reviewed)
        return True

    _update_state(repo_slug, SELF_REVIEW_FILE, read_self_review_state, mutate)


def get_traceability_reviewed_prs(repo_slug: str) -> set[int]:
    """Independent of reviewed_prs/partial_reviews/design_reviewed_prs by design (§7.4 of
    requirement-traceability-requirements.md): a traceability-pass failure never forces
    re-running the other three passes, and vice versa."""
    return set(read_self_review_state(repo_slug).get("traceability_reviewed_prs", []))


def add_traceability_reviewed_pr(repo_slug: str, pr_number: int) -> None:
    def mutate(current: dict) -> bool:
        traceability_reviewed = set(current.get("traceability_reviewed_prs", []))
        if pr_number in traceability_reviewed:
            return False
        traceability_reviewed.add(pr_number)
        current["traceability_reviewed_prs"] = sorted(traceability_reviewed)
        return True

    _update_state(repo_slug, SELF_REVIEW_FILE, read_self_review_state, mutate)


def get_regret_reviewed_prs(repo_slug: str) -> set[int]:
    """Independent of reviewed_prs/partial_reviews/design_reviewed_prs/
    traceability_reviewed_prs by design (regret-review-requirements.md §7.3): a
    regret-pass retry never forces, or is forced by, any other pass's retry."""
    return set(read_self_review_state(repo_slug).get("regret_reviewed_prs", []))


def add_regret_reviewed_pr(repo_slug: str, pr_number: int) -> None:
    def mutate(current: dict) -> bool:
        regret_reviewed = set(current.get("regret_reviewed_prs", []))
        if pr_number in regret_reviewed:
            return False
        regret_reviewed.add(pr_number)
        current["regret_reviewed_prs"] = sorted(regret_reviewed)
        return True

    _update_state(repo_slug, SELF_REVIEW_FILE, read_self_review_state, mutate)


def read_address_comments_state(repo_slug: str) -> dict:
    defaults = {"version": 1, "replied": {}}
    return _load_json_dict(_state_path(repo_slug, ADDRESS_COMMENTS_FILE), defaults)


def get_replied_comment_keys(repo_slug: str, pr_number: int) -> set[str]:
    """Keys ("issue:123", "review:456") of non-inline comments already addressed on this PR.
    Needed because issue/review comments have no reply linkage in the GitHub API, so there
    is no way to tell from GitHub alone that we already answered one."""
    return set(read_address_comments_state(repo_slug)["replied"].get(str(pr_number), []))


def record_replied_comment(repo_slug: str, pr_number: int, key: str) -> None:
    def mutate(current: dict) -> bool:
        keys = set(current["replied"].get(str(pr_number), []))
        if key in keys:
            return False
        keys.add(key)
        current["replied"][str(pr_number)] = sorted(keys)
        return True

    _update_state(repo_slug, ADDRESS_COMMENTS_FILE, read_address_comments_state, mutate)


def prune_address_comments_state(repo_slug: str, open_pr_numbers: set[int]) -> None:
    """Drop entries for PRs that are no longer open, so the map doesn't grow unboundedly."""
    keep = {str(n) for n in open_pr_numbers}

    def mutate(current: dict) -> bool:
        to_drop = current["replied"].keys() - keep
        for pr in to_drop:
            del current["replied"][pr]
        return bool(to_drop)

    _update_state(repo_slug, ADDRESS_COMMENTS_FILE, read_address_comments_state, mutate)


def delete_state(repo_slug: str, command: str) -> None:
    filename = _COMMAND_FILES.get(command)
    if filename is None:
        raise ValueError(f"No state file for command: {command}")  # noqa: TRY003
    p = _state_path(repo_slug, filename)
    if p.exists():
        p.unlink()
