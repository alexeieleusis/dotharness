from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from harness.lock import working_dir_lock_key


class ConfigError(ValueError):
    pass


def _fallback_repo_slug(working_dir: Path) -> str:
    """Directory-safe slug for a config without `repo.name`.

    Sanitized basename (anything outside [a-zA-Z0-9._-] becomes "-") plus "-" and the
    first 8 hex chars of the SHA-256 of the resolved path. Always matches
    ``^[a-zA-Z0-9._-]+$``.
    """
    resolved = working_dir.expanduser().resolve()
    base = re.sub(r"[^a-zA-Z0-9._-]", "-", resolved.name).strip(".-") or "repo"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


@dataclass
class PreCommand:
    cmd: str
    critical: bool = False


def _parse_pre_command(entry: str | dict) -> PreCommand:
    if isinstance(entry, str):
        return PreCommand(cmd=entry)
    if "cmd" not in entry:
        raise ConfigError("pre_commands dict entry is missing required key 'cmd'")  # noqa: TRY003
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
    min_reanalysis_interval_hours: float = 24.0
    prune_projects_enabled: bool = False
    prune_older_than_minutes: int = 60
    prune_projects_timeout: int = 120


@dataclass
class FocusedReviewConfig:
    enabled: bool = False
    vibe_types_repo: Path = field(default_factory=lambda: Path("~/.harness/vendor/vibe-types").expanduser())


@dataclass
class AddressCommentsConfig:
    enabled: bool = True
    trusted_commenters: str | list[str] = "*"


@dataclass
class RegretReviewConfig:
    enabled: bool = False
    max_diff_lines: int = 50
    authors: str | list[str] = "*"
    regret_review_timeout: int = 300


@dataclass
class LocalReviewConfig:
    base: str | None = None
    output_dir: str | None = None


@dataclass
class RepoConfig:
    name: str  # "" when `repo.name` is absent (only possible with require_repo_name=False)
    working_dir: Path
    subdirs: list[SubDir] = field(default_factory=list)
    opencode_dir: Path | None = None
    name_provided: bool = True


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
    regret_review: RegretReviewConfig = field(default_factory=RegretReviewConfig)
    local_review: LocalReviewConfig = field(default_factory=LocalReviewConfig)

    @property
    def repo_slug(self) -> str:
        if self.repo.name:
            return self.repo.name.replace("/", "-")
        return _fallback_repo_slug(self.repo.working_dir)

    @property
    def lock_key(self) -> str:
        """Scoped to repo.working_dir, not just repo.name, so a second clone of the
        same origin (e.g. one dedicated to a cheaper backend for a single runner)
        doesn't contend for the same lock as the primary clone."""
        return working_dir_lock_key(self.repo_slug, self.repo.working_dir)


def _parse_subdirs(raw_subdirs: list[dict]) -> list[SubDir]:
    subdirs = []
    for s in raw_subdirs:
        if "path" not in s:
            raise ConfigError("repo.subdir[].path is required")  # noqa: TRY003
        subdirs.append(
            SubDir(
                path=s["path"],
                pre_commands=[_parse_pre_command(pc) for pc in s.get("pre_commands", [])],
                coverage=s.get("coverage", False),
                timeout=s.get("timeout", 300),
            )
        )
    return subdirs


def _parse_opencode_dir(raw_odir: str | None, working_dir: Path) -> Path | None:
    if not raw_odir:
        return None
    opencode_dir = Path(raw_odir).expanduser()
    try:
        opencode_dir.resolve().relative_to(working_dir.resolve())
    except ValueError:
        raise ConfigError(  # noqa: TRY003
            f"repo.opencode_dir '{opencode_dir}' must be inside repo.working_dir '{working_dir}'"
        ) from None
    return opencode_dir


def load_config(path: Path, *, require_repo_name: bool = True) -> HarnessConfig:
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Invalid TOML in config file: {e}") from None  # noqa: TRY003

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
    if require_repo_name and not r.get("name"):
        raise ConfigError("repo.name is required")  # noqa: TRY003
    if r.get("name"):
        slug = str(r["name"]).replace("/", "-")
        if slug in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", slug):
            raise ConfigError(  # noqa: TRY003
                f"Invalid repo.name '{r['name']}': must match [A-Za-z0-9._/-]+ and not be '.' or '..'"
            )
    if not r.get("working_dir"):
        raise ConfigError("repo.working_dir is required")  # noqa: TRY003

    subdirs = _parse_subdirs(r.get("subdir", []))

    working_dir = Path(r["working_dir"]).expanduser()
    opencode_dir = _parse_opencode_dir(r.get("opencode_dir"), working_dir)

    repo = RepoConfig(
        name=r.get("name") or "",
        working_dir=working_dir,
        subdirs=subdirs,
        opencode_dir=opencode_dir,
        name_provided=bool(r.get("name")),
    )

    vh = data.get("vibe_heal", {})
    vibe_heal = VibehealConfig(
        enabled=vh.get("enabled", False),
        python=vh.get("python", ""),
        authors=vh.get("authors", "*"),
        vibe_heal_timeout=vh.get("vibe_heal_timeout", 600),
        vibe_heal_post_timeout=vh.get("vibe_heal_post_timeout", 120),
        min_reanalysis_interval_hours=vh.get("min_reanalysis_interval_hours", 24.0),
        prune_projects_enabled=vh.get("prune_projects_enabled", False),
        prune_older_than_minutes=vh.get("prune_older_than_minutes", 60),
        prune_projects_timeout=vh.get("prune_projects_timeout", 120),
    )

    fr = data.get("focused_review", {})
    focused_review = FocusedReviewConfig(
        enabled=fr.get("enabled", False),
        vibe_types_repo=Path(fr.get("vibe_types_repo", "~/.harness/vendor/vibe-types")).expanduser(),
    )

    ac = data.get("address_comments", {})
    raw_tc = ac.get("trusted_commenters", "*")
    if isinstance(raw_tc, str):
        trusted_commenters: str | list[str] = raw_tc
    else:
        trusted_commenters = list(raw_tc)
    address_comments = AddressCommentsConfig(
        enabled=ac.get("enabled", True),
        trusted_commenters=trusted_commenters,
    )

    rr = data.get("regret_review", {})
    raw_rr_authors = rr.get("authors", "*")
    regret_review = RegretReviewConfig(
        enabled=rr.get("enabled", False),
        max_diff_lines=rr.get("max_diff_lines", 50),
        authors=raw_rr_authors if isinstance(raw_rr_authors, str) else list(raw_rr_authors),
        regret_review_timeout=rr.get("regret_review_timeout", 300),
    )

    lr = data.get("local_review", {})
    local_review = LocalReviewConfig(
        base=lr.get("base") or None,
        output_dir=lr.get("output_dir") or None,
    )

    return HarnessConfig(
        harness=harness_section,
        repo=repo,
        vibe_heal=vibe_heal,
        focused_review=focused_review,
        address_comments=address_comments,
        regret_review=regret_review,
        local_review=local_review,
    )
