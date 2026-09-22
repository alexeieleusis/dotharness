from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from spec_prism_flow.config import SpecPrismFlowConfig


@dataclass(frozen=True)
class ResumeState:
    repo: str
    branch: str
    pr_number: int
    pr_url: str
    cycle_index: int


def resume_state_path(config: SpecPrismFlowConfig, repo: str, branch: str) -> Path:
    """Keyed by repo slug + branch so two different phases' resume records never
    collide. Escapes literal "-" before collapsing "/" to "-" so hyphenated repo
    names (e.g. "acme/my-repo" vs "acme-my/repo") can't slugify to the same path."""
    slug = repo.replace("-", "--").replace("/", "-")
    return config.build.state_dir / slug / branch / "resume_state.json"


def load_resume_state(path: Path) -> ResumeState | None:
    """Returns None, not an error, when `path` doesn't exist -- the normal case for
    a phase's first run, before any PR has been created for it."""
    if not path.exists():
        return None
    return ResumeState(**json.loads(path.read_text()))


def save_resume_state(path: Path, state: ResumeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state)))
    tmp.replace(path)


def clear_resume_state(path: Path) -> None:
    """No-op (not an error) when `path` is already absent."""
    path.unlink(missing_ok=True)
