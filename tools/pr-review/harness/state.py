import json
from pathlib import Path

XDG_DATA = Path.home() / ".local/share/dotharness"

VIBE_HEAL_FILE = "vibe_heal.json"
SELF_REVIEW_FILE = "self_review.json"

_COMMAND_FILES = {
    "review-prs": VIBE_HEAL_FILE,
    "self-review": SELF_REVIEW_FILE,
}


def _state_path(repo_slug: str, filename: str) -> Path:
    p = XDG_DATA / "state" / repo_slug / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.rename(path)


def read_vibe_heal_state(repo_slug: str) -> dict:
    p = _state_path(repo_slug, VIBE_HEAL_FILE)
    if not p.exists():
        return {"version": 1, "last_pr": 0, "last_main_sha": ""}
    data = json.loads(p.read_text(encoding="utf-8"))
    data.setdefault("last_main_sha", "")
    return data


def write_vibe_heal_state(repo_slug: str, last_pr: int | None = None, *, last_main_sha: str | None = None) -> None:
    if last_pr is None and last_main_sha is None:
        raise ValueError("write_vibe_heal_state called with no fields to update")  # noqa: TRY003
    current = read_vibe_heal_state(repo_slug)
    if last_pr is not None:
        current["last_pr"] = last_pr
    if last_main_sha is not None:
        current["last_main_sha"] = last_main_sha
    p = _state_path(repo_slug, VIBE_HEAL_FILE)
    _atomic_write(p, current)


def read_self_review_state(repo_slug: str) -> dict:
    p = _state_path(repo_slug, SELF_REVIEW_FILE)
    if not p.exists():
        return {"version": 1, "reviewed_prs": []}
    return json.loads(p.read_text(encoding="utf-8"))


def write_self_review_state(repo_slug: str, reviewed_prs: list[int]) -> None:
    p = _state_path(repo_slug, SELF_REVIEW_FILE)
    _atomic_write(p, {"version": 1, "reviewed_prs": reviewed_prs})


def delete_state(repo_slug: str, command: str) -> None:
    if command not in _COMMAND_FILES:
        raise ValueError(f"No state file for command: {command}")  # noqa: TRY003
    p = _state_path(repo_slug, _COMMAND_FILES[command])
    if p.exists():
        p.unlink()
