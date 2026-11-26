"""Loader for the per-repository ``.sisyphus.toml`` project config.

Standard library only. Validation happens at load time and every failure
raises :class:`ConfigError` with a message naming the offending section and key.
Path values are returned verbatim; the loader never touches the filesystem for
them, never executes ``token_cmd`` and never reads the environment. The one
filesystem read is the listing of packaged pattern-set names, used only to
validate ``[refactor] pattern_set``.
"""

# ruff: noqa: TRY003
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotharness.ai_backend import BACKEND_NAMES
from dotharness.fileio import TOMLDecodeError, load_toml
from dotharness.paths import fallback_repo_slug, repo_slug_from_name
from dotharness.sisyphus import stack

DEFAULT_CONFIG_FILE = ".sisyphus.toml"

TRACKER_KINDS = ("linear", "github", "host", "file")
HOST_KINDS = ("host", "github")
# Kind names rejected as `[source.<kind>]` tables; `[source.host]` is the host options table, so `host` is absent.
_NON_TABLE_KINDS = ("linear", "github", "file")
AGENT_BACKENDS: tuple[str, ...] = BACKEND_NAMES
DEFAULT_PICK_LABEL = "sisyphus"
_HOST_SECTION = "source.host"


class ConfigError(Exception):
    """Raised when ``.sisyphus.toml`` is missing, malformed or invalid."""


@dataclass(frozen=True)
class ProjectConfig:
    repo_dir: str | None = None
    workspace_dir: str | None = None
    brief_path_template: str | None = None
    base_branch: str | None = None
    repo_name: str | None = None

    @property
    def repo_slug(self) -> str:
        """Directory-safe repo slug: ``repo_name`` (``/`` -> ``-``, validated) or the fallback over ``repo_dir``.

        With neither set the fallback runs over the current directory (as the CLI does for ``repo_dir``).
        """
        if self.repo_name is None:
            return fallback_repo_slug(Path(self.repo_dir or "."))
        return _resolve_repo_name(self.repo_name)


def _resolve_repo_name(repo_name: str) -> str:
    try:
        return repo_slug_from_name(repo_name)
    except ValueError as exc:
        raise ConfigError(f"[project] repo_name '{repo_name}' is not a valid repo slug: {exc}") from exc


@dataclass(frozen=True)
class WorkItemSource:
    kind: str | None = None
    instruction: str | None = None
    assignee: str | None = None
    pick_state: str | None = None
    pick_label: str | None = None
    dir: str | None = None
    remove_label_when_settled: bool | None = None
    # The spelling the user wrote (`host` or `github`); `kind` holds the canonical value. Not part of equality.
    written_kind: str | None = field(default=None, compare=False)

    @property
    def spelling(self) -> str | None:
        """The kind as the user wrote it, falling back to ``kind`` for a directly built source."""
        return self.written_kind or self.kind


def uses_host_items(work_item: WorkItemSource | None) -> bool:
    """True when the host issue tracker serves work items: no ``[source.work_item]``, or ``kind`` is ``host``/``github``."""
    return work_item is None or work_item.kind in HOST_KINDS


@dataclass(frozen=True)
class HostSource:
    token_cmd: str | None = None
    name: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceConfig:
    work_item: WorkItemSource | None = None
    host: HostSource | None = None


@dataclass(frozen=True)
class AgentConfig:
    backend: str | None = None


@dataclass(frozen=True)
class LimitsConfig:
    max_run_minutes: int = 180
    max_tickets_per_run: int = 1
    max_leaves_per_run: int = 32
    presentation_timeout_minutes: int = 5


@dataclass(frozen=True)
class PresentationConfig:
    present_stops: bool = True


DEFAULT_DIGEST_PATH_TEMPLATE = "<workspace_dir>/digest-<date>.md"


@dataclass(frozen=True)
class DigestConfig:
    path_template: str = DEFAULT_DIGEST_PATH_TEMPLATE


@dataclass(frozen=True)
class KnowledgeConfig:
    sizing_file: str | None = None
    conventions_path: str | None = None


@dataclass(frozen=True)
class ArchiveConfig:
    dir: str = "archive"


@dataclass(frozen=True)
class RefactorConfig:
    enabled: bool = True
    pattern_set: str | None = None


@dataclass(frozen=True)
class SisyphusConfig:
    project: ProjectConfig
    source: SourceConfig = field(default_factory=SourceConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    presentation: PresentationConfig = field(default_factory=PresentationConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    refactor: RefactorConfig = field(default_factory=RefactorConfig)


def _table(raw: Any, name: str, allowed: tuple[str, ...], *, passthrough: bool = False) -> dict[str, Any]:
    """Check ``raw`` is a table; unknown keys are rejected unless ``passthrough`` (harness-owned keys)."""
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] must be a table, got {type(raw).__name__}")
    for key in raw:
        if not passthrough and key not in allowed:
            raise ConfigError(f"unknown key '{key}' in [{name}] (allowed: {', '.join(allowed)})")
    return raw


def _str(table: dict[str, Any], section: str, key: str) -> str | None:
    value = table.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"[{section}] {key} must be a string, got {type(value).__name__}")
    return value


def _bool(table: dict[str, Any], section: str, key: str) -> bool | None:
    value = table.get(key)
    if value is not None and not isinstance(value, bool):
        raise ConfigError(f"[{section}] {key} must be a boolean, got {type(value).__name__}")
    return value


def _posint(table: dict[str, Any], section: str, key: str, default: int | None) -> int | None:
    value = table.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"[{section}] {key} must be an integer, got {type(value).__name__}")
    if value <= 0:
        raise ConfigError(f"[{section}] {key} must be a positive integer, got {value}")
    return value


def _limit(table: dict[str, Any], key: str, default: int) -> int:
    value = _posint(table, "limits", key, default)
    assert value is not None  # noqa: S101
    return value


def _choice(value: str | None, section: str, key: str, allowed: tuple[str, ...]) -> str | None:
    if value is not None and value not in allowed:
        raise ConfigError(f"[{section}] {key} must be one of {', '.join(allowed)}, got '{value}'")
    return value


def _parse_work_item(raw: Any) -> WorkItemSource:
    section = "source.work_item"
    t = _table(
        raw,
        section,
        ("kind", "instruction", "assignee", "pick_state", "pick_label", "dir", "remove_label_when_settled"),
    )
    written = _choice(_str(t, section, "kind"), section, "kind", TRACKER_KINDS)
    kind = "host" if written in HOST_KINDS else written
    pick_state = _str(t, section, "pick_state")
    pick_label = _str(t, section, "pick_label")
    if kind == "host" and pick_state is not None:
        raise ConfigError(
            f'[{section}] pick_state is a linear key and cannot be used with kind = "{written}" (mixed tracker)'
        )
    if kind == "linear" and pick_label is not None:
        raise ConfigError(
            f'[{section}] pick_label is a github key and cannot be used with kind = "linear" (mixed tracker)'
        )
    remove_label = _bool(t, section, "remove_label_when_settled")
    if kind == "linear" and remove_label is not None:
        raise ConfigError(
            f"[{section}] remove_label_when_settled is a github key"
            ' and cannot be used with kind = "linear" (mixed tracker)'
        )
    dir_ = _str(t, section, "dir")
    if kind == "file" and dir_ is None:
        raise ConfigError(f'[{section}] dir is required when kind = "file"')
    if kind != "file" and dir_ is not None:
        raise ConfigError(f'[{section}] dir is a file-tracker key and requires kind = "file"')
    if kind == "host" and pick_label is None:
        pick_label = DEFAULT_PICK_LABEL
    return WorkItemSource(
        kind=kind,
        instruction=_str(t, section, "instruction"),
        assignee=_str(t, section, "assignee"),
        pick_state=pick_state,
        pick_label=pick_label,
        dir=dir_,
        remove_label_when_settled=remove_label,
        written_kind=written,
    )


def _parse_source(raw: Any) -> SourceConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"[source] must be a table, got {type(raw).__name__}")
    for key in raw:
        if key in _NON_TABLE_KINDS:
            if "work_item" in raw:
                raise ConfigError(
                    f"two trackers: [source.{key}] alongside [source.work_item]; exactly one tracker is allowed"
                )
            raise ConfigError(
                f'[source.{key}] is not a valid table; declare the tracker as [source.work_item] with kind = "{key}"'
            )
        if key not in ("work_item", "ci", "host"):
            raise ConfigError(f"unknown key '{key}' in [source] (allowed: work_item, ci, host)")
    work_item = _parse_work_item(raw["work_item"]) if "work_item" in raw else None
    if "ci" in raw:
        # [source.ci] is accepted and ignored; its keys belong to harness.
        _table(raw["ci"], "source.ci", (), passthrough=True)
    host = None
    if "host" in raw:
        t = _table(raw["host"], _HOST_SECTION, ("token_cmd", "name", "options"))
        name = _str(t, _HOST_SECTION, "name")
        if name is not None and not name.strip():
            raise ConfigError("[source.host] name must be a non-empty string")
        options = t.get("options", {})
        if not isinstance(options, dict):
            raise ConfigError(f"[source.host] options must be a table, got {type(options).__name__}")
        host = HostSource(token_cmd=_str(t, _HOST_SECTION, "token_cmd"), name=name, options=dict(options))
    return SourceConfig(work_item=work_item, host=host)


def _parse_refactor(data: dict[str, Any]) -> RefactorConfig:
    if "refactor" not in data:
        return RefactorConfig()
    t = _table(data["refactor"], "refactor", ("enabled", "pattern_set"))
    enabled = _bool(t, "refactor", "enabled")
    pattern_set = _str(t, "refactor", "pattern_set")
    if pattern_set is not None:
        try:
            valid = stack.pattern_set_names()
        except stack.StackError as exc:
            raise ConfigError(f"cannot validate [refactor] pattern_set: {exc}") from exc
        pattern_set = _choice(pattern_set, "refactor", "pattern_set", valid)
    return RefactorConfig(enabled=True if enabled is None else enabled, pattern_set=pattern_set)


def _parse_limits(raw: Any) -> LimitsConfig:
    keys = (
        "max_run_minutes",
        "max_tickets_per_run",
        "max_leaves_per_run",
        "presentation_timeout_minutes",
    )
    t = _table(raw, "limits", keys)
    d = LimitsConfig()
    return LimitsConfig(
        max_run_minutes=_limit(t, "max_run_minutes", d.max_run_minutes),
        max_tickets_per_run=_limit(t, "max_tickets_per_run", d.max_tickets_per_run),
        max_leaves_per_run=_limit(t, "max_leaves_per_run", d.max_leaves_per_run),
        presentation_timeout_minutes=_limit(t, "presentation_timeout_minutes", d.presentation_timeout_minutes),
    )


def _parse_presentation(raw: Any) -> PresentationConfig:
    t = _table(raw, "presentation", ("present_stops",))
    present_stops = _bool(t, "presentation", "present_stops")
    return PresentationConfig() if present_stops is None else PresentationConfig(present_stops=present_stops)


def _parse_digest(raw: Any) -> DigestConfig:
    t = _table(raw, "digest", ("path_template",))
    template = _str(t, "digest", "path_template")
    if template == "":
        raise ConfigError("[digest] path_template must not be empty")
    return DigestConfig() if template is None else DigestConfig(path_template=template)


def _parse(data: dict[str, Any]) -> SisyphusConfig:
    top = (
        "project",
        "source",
        "agent",
        "limits",
        "merge",
        "presentation",
        "digest",
        "knowledge",
        "archive",
        "refactor",
    )
    for key in data:
        if key not in top:
            raise ConfigError(f"unknown top-level key '{key}' (allowed: {', '.join(top)})")
    if "project" not in data:
        raise ConfigError("missing required [project] table")

    t = _table(
        data["project"],
        "project",
        ("repo_dir", "workspace_dir", "brief_path_template", "base_branch", "repo_name"),
    )
    project = ProjectConfig(
        repo_dir=_str(t, "project", "repo_dir"),
        workspace_dir=_str(t, "project", "workspace_dir"),
        brief_path_template=_str(t, "project", "brief_path_template"),
        base_branch=_str(t, "project", "base_branch"),
        repo_name=_str(t, "project", "repo_name"),
    )
    if project.repo_name is not None:
        _resolve_repo_name(project.repo_name)  # fail at load time on an invalid slug
    source = _parse_source(data["source"]) if "source" in data else SourceConfig()

    agent = AgentConfig()
    if "agent" in data:
        t = _table(data["agent"], "agent", ("backend",))
        agent = AgentConfig(backend=_choice(_str(t, "agent", "backend"), "agent", "backend", AGENT_BACKENDS))

    limits = _parse_limits(data["limits"]) if "limits" in data else LimitsConfig()
    presentation = _parse_presentation(data["presentation"]) if "presentation" in data else PresentationConfig()

    digest = _parse_digest(data["digest"]) if "digest" in data else DigestConfig()

    knowledge = KnowledgeConfig()
    if "knowledge" in data:
        t = _table(data["knowledge"], "knowledge", ("sizing_file", "conventions_path"))
        knowledge = KnowledgeConfig(
            sizing_file=_str(t, "knowledge", "sizing_file"),
            conventions_path=_str(t, "knowledge", "conventions_path"),
        )

    archive = ArchiveConfig()
    if "archive" in data:
        t = _table(data["archive"], "archive", ("dir",))
        archive_dir = _str(t, "archive", "dir")
        if archive_dir is not None:
            archive = ArchiveConfig(dir=archive_dir)
    refactor = _parse_refactor(data)

    return SisyphusConfig(
        project=project,
        source=source,
        agent=agent,
        limits=limits,
        presentation=presentation,
        digest=digest,
        knowledge=knowledge,
        archive=archive,
        refactor=refactor,
    )


def load_config(path: str | Path) -> SisyphusConfig:
    """Load and validate a ``.sisyphus.toml`` file.

    Raises :class:`ConfigError` for unreadable files, TOML syntax errors and any
    schema violation.
    """
    p = Path(path)
    try:
        data = load_toml(p)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read config file {p}: {exc}") from exc
    except TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {p}: {exc}") from exc
    return _parse(data)
