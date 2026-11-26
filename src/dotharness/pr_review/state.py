import copy
import json
import logging
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotharness.fileio import atomic_write_text, file_lock
from dotharness.paths import data_dir, validate_repo_slug

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


def _state_path(repo_slug: str, filename: str) -> Path:
    validate_repo_slug(repo_slug)  # shared with the merge-gate reader (dotharness.paths)
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
    # `handled_versions` is {pr: {comment_key: {"version": str, "rounds": int, "rocket_at": str}}}: the
    # edit version last handled, and the rounds run for the rocket created at `rocket_at`. Files written
    # before it existed load with the empty default.
    defaults = {"version": 1, "replied": {}, "handled_versions": {}}
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


@dataclass(frozen=True)
class Watermark:
    """What the signal path has handled for one comment: the edit `version` last handled (REST
    `updated_at`), and the `rounds` run for the rocket created at `rocket_at`. Owns the on-disk ISO-string
    format; an unparseable timestamp reads as None (an edit then counts as unhandled, no rocket matches)."""

    version: datetime | None
    rounds: int
    rocket_at: datetime | None

    @classmethod
    def from_entry(cls, entry: object) -> "Watermark | None":
        """Parse a stored entry; None when it is malformed (no string `version`)."""
        if not isinstance(entry, dict) or not isinstance(entry.get("version"), str):
            return None
        rounds = entry.get("rounds")
        return cls(
            version=_parse_timestamp(entry["version"]),
            rounds=rounds if isinstance(rounds, int) and rounds >= 0 else 0,
            rocket_at=_parse_timestamp(entry.get("rocket_at")),
        )

    def to_entry(self) -> dict:
        return {
            "version": self.version.isoformat() if self.version is not None else "",
            "rounds": self.rounds,
            "rocket_at": self.rocket_at.isoformat() if self.rocket_at is not None else "",
        }

    def is_stale(self, updated_at: datetime | None) -> bool:
        """True when the comment was edited after the handled version (or the version is unreadable)."""
        if updated_at is None:
            return False
        if self.version is None:
            return True
        try:
            return updated_at > self.version
        except TypeError:  # naive vs aware
            return True

    def rounds_for(self, rocket_at: datetime) -> int:
        """Rounds already run for the rocket created at `rocket_at`: 0 when the stored `rocket_at` differs
        (the rocket was removed and re-added)."""
        return self.rounds if self.rocket_at == rocket_at else 0

    def advanced(self, version: datetime, rocket_at: datetime) -> "Watermark":
        """The watermark after one more round: new `version`, rounds restarting when `rocket_at` changed."""
        return Watermark(version=version, rounds=self.rounds_for(rocket_at) + 1, rocket_at=rocket_at)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def get_comment_watermark(repo_slug: str, pr_number: int, key: str) -> Watermark | None:
    """The stored `Watermark` for a comment key on this PR, or None if never handled (or the entry is
    malformed)."""
    entries = read_address_comments_state(repo_slug)["handled_versions"].get(str(pr_number), {})
    return Watermark.from_entry(entries.get(key) if isinstance(entries, dict) else None)


def record_handled_comment(repo_slug: str, pr_number: int, key: str, version: datetime, rocket_at: datetime) -> None:
    """Advance the watermark for a comment key: store the snapshot edit `version` and `rocket_at`, and
    increment `rounds` (restarting from 0 when `rocket_at` changed) in the same locked write."""

    def mutate(current: dict) -> bool:
        per_pr = current["handled_versions"].setdefault(str(pr_number), {})
        previous = Watermark.from_entry(per_pr.get(key)) or Watermark(None, 0, None)
        per_pr[key] = previous.advanced(version, rocket_at).to_entry()
        return True

    _update_state(repo_slug, ADDRESS_COMMENTS_FILE, read_address_comments_state, mutate)


def prune_address_comments_state(repo_slug: str, open_pr_numbers: set[int]) -> None:
    """Drop entries for PRs that are no longer open, so the map doesn't grow unboundedly."""
    keep = {str(n) for n in open_pr_numbers}

    def mutate(current: dict) -> bool:
        to_drop = (current["replied"].keys() | current["handled_versions"].keys()) - keep
        for pr in to_drop:
            current["replied"].pop(pr, None)
            current["handled_versions"].pop(pr, None)
        return bool(to_drop)

    _update_state(repo_slug, ADDRESS_COMMENTS_FILE, read_address_comments_state, mutate)


def delete_state(repo_slug: str, command: str) -> None:
    filename = _COMMAND_FILES.get(command)
    if filename is None:
        raise ValueError(f"No state file for command: {command}")  # noqa: TRY003
    p = _state_path(repo_slug, filename)
    if p.exists():
        p.unlink()
