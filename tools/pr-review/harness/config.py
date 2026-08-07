from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass
class PreCommand:
    cmd: str
    critical: bool = False


def _parse_pre_command(entry: str | dict) -> PreCommand:
    if isinstance(entry, str):
        return PreCommand(cmd=entry)
    return PreCommand(cmd=entry["cmd"], critical=entry.get("critical", False))


@dataclass
class SubDir:
    path: str
    pre_commands: list[PreCommand] = field(default_factory=list)
    coverage: bool = False
    timeout: int = 300


@dataclass
class VibehealConfig:
    enabled: bool = False
    python: str = ""
    authors: str | list[str] = "*"
    vibe_heal_timeout: int = 600
    vibe_heal_post_timeout: int = 120


@dataclass
class FocusedReviewConfig:
    enabled: bool = False
    vibe_types_repo: Path = field(default_factory=lambda: Path("~/.harness/vendor/vibe-types").expanduser())


@dataclass
class AddressCommentsConfig:
    require_reaction_for_focused_review: bool = False


@dataclass
class RepoConfig:
    name: str
    working_dir: Path
    subdirs: list[SubDir] = field(default_factory=list)
    opencode_dir: Path | None = None


@dataclass
class HarnessSection:
    backend: str = "opencode"
    gh_token_cmd: str = "gh auth token"  # noqa: S105
    backend_timeout_seconds: int = 900
    knowledge_dir: Path = field(default_factory=lambda: Path("~/.harness/knowledge").expanduser())
    path_prepend: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    review_knowledge_file: Path | None = None


@dataclass
class HarnessConfig:
    harness: HarnessSection
    repo: RepoConfig
    vibe_heal: VibehealConfig = field(default_factory=VibehealConfig)
    focused_review: FocusedReviewConfig = field(default_factory=FocusedReviewConfig)
    address_comments: AddressCommentsConfig = field(default_factory=AddressCommentsConfig)

    @property
    def repo_slug(self) -> str:
        return self.repo.name.replace("/", "-")


def load_config(path: Path) -> HarnessConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    h = data.get("harness", {})
    backend = h.get("backend", "opencode")
    if backend not in ("opencode", "claude"):
        raise ConfigError(f"Invalid backend '{backend}': must be 'opencode' or 'claude'")  # noqa: TRY003

    path_prepend = list(h.get("path_prepend", {}).values())
    rkf = h.get("review_knowledge_file")
    harness_section = HarnessSection(
        backend=backend,
        gh_token_cmd=h.get("gh_token_cmd", "gh auth token"),
        backend_timeout_seconds=h.get("backend_timeout_seconds", 900),
        knowledge_dir=Path(h.get("knowledge_dir", "~/.harness/knowledge")).expanduser(),
        path_prepend=path_prepend,
        env=h.get("env", {}),
        review_knowledge_file=Path(rkf).expanduser() if rkf else None,
    )

    r = data.get("repo", {})
    if not r.get("name"):
        raise ConfigError("repo.name is required")  # noqa: TRY003
    if not r.get("working_dir"):
        raise ConfigError("repo.working_dir is required")  # noqa: TRY003

    subdirs = [
        SubDir(
            path=s["path"],
            pre_commands=[_parse_pre_command(pc) for pc in s.get("pre_commands", [])],
            coverage=s.get("coverage", False),
            timeout=s.get("timeout", 300),
        )
        for s in r.get("subdir", [])
    ]

    working_dir = Path(r["working_dir"]).expanduser()
    raw_odir = r.get("opencode_dir")
    opencode_dir: Path | None = None
    if raw_odir:
        opencode_dir = Path(raw_odir).expanduser()
        try:
            opencode_dir.relative_to(working_dir)
        except ValueError:
            raise ConfigError(  # noqa: TRY003
                f"repo.opencode_dir '{opencode_dir}' must be inside repo.working_dir '{working_dir}'"
            ) from None

    repo = RepoConfig(
        name=r["name"],
        working_dir=working_dir,
        subdirs=subdirs,
        opencode_dir=opencode_dir,
    )

    vh = data.get("vibe_heal", {})
    vibe_heal = VibehealConfig(
        enabled=vh.get("enabled", False),
        python=vh.get("python", ""),
        authors=vh.get("authors", "*"),
        vibe_heal_timeout=vh.get("vibe_heal_timeout", 600),
        vibe_heal_post_timeout=vh.get("vibe_heal_post_timeout", 120),
    )

    fr = data.get("focused_review", {})
    focused_review = FocusedReviewConfig(
        enabled=fr.get("enabled", False),
        vibe_types_repo=Path(fr.get("vibe_types_repo", "~/.harness/vendor/vibe-types")).expanduser(),
    )

    ac = data.get("address_comments", {})
    address_comments = AddressCommentsConfig(
        require_reaction_for_focused_review=ac.get("require_reaction_for_focused_review", False),
    )

    return HarnessConfig(
        harness=harness_section,
        repo=repo,
        vibe_heal=vibe_heal,
        focused_review=focused_review,
        address_comments=address_comments,
    )
