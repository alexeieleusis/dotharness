"""Loader for the per-repository ``.sisyphus.toml`` project config.

Standard library only. Validation happens at load time and every failure
raises :class:`ConfigError` with a message naming the offending section and key.
Path values are returned verbatim; the loader never touches the filesystem for
them, never executes ``token_cmd`` and never reads the environment.
"""

# ruff: noqa: TRY003
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_FILE = ".sisyphus.toml"

TRACKER_KINDS = ("linear", "github")
CI_KINDS = ("github",)
AGENT_BACKENDS = ("claude", "opencode")
DEFAULT_PICK_LABEL = "sisyphus"
_CI_SECTION = "source.ci"


class ConfigError(Exception):
    """Raised when ``.sisyphus.toml`` is missing, malformed or invalid."""


@dataclass(frozen=True)
class ProjectConfig:
    repo_dir: str | None = None
    workspace_dir: str | None = None
    brief_path_template: str | None = None
    base_branch: str | None = None


@dataclass(frozen=True)
class WorkItemSource:
    kind: str | None = None
    instruction: str | None = None
    assignee: str | None = None
    pick_state: str | None = None
    pick_label: str | None = None


@dataclass(frozen=True)
class CiSource:
    kind: str | None = None
    max_fix_attempts: int | None = None


@dataclass(frozen=True)
class HostSource:
    token_cmd: str | None = None


@dataclass(frozen=True)
class SourceConfig:
    work_item: WorkItemSource | None = None
    ci: CiSource | None = None
    host: HostSource | None = None


@dataclass(frozen=True)
class AgentConfig:
    backend: str | None = None


@dataclass(frozen=True)
class LimitsConfig:
    max_run_minutes: int = 180
    max_tickets_per_run: int = 1
    max_fix_attempts_per_pr: int = 3
    max_leaves_per_run: int = 4


@dataclass(frozen=True)
class MergeConfig:
    enabled: bool = False
    require_all_files_viewed: bool | None = None
    max_changed_lines: int | None = None


@dataclass(frozen=True)
class DigestConfig:
    path_template: str | None = None


@dataclass(frozen=True)
class KnowledgeConfig:
    sizing_file: str | None = None
    conventions_path: str | None = None


@dataclass(frozen=True)
class SisyphusConfig:
    project: ProjectConfig
    source: SourceConfig = field(default_factory=SourceConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)


def _table(raw: Any, name: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] must be a table, got {type(raw).__name__}")
    for key in raw:
        if key not in allowed:
            raise ConfigError(f"unknown key '{key}' in [{name}] (allowed: {', '.join(allowed)})")
    return raw


def _str(table: dict[str, Any], section: str, key: str) -> str | None:
    value = table.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"[{section}] {key} must be a string, got {type(value).__name__}")
    return value


def _bool(table: dict[str, Any], section: str, key: str, default: bool | None) -> bool | None:
    value = table.get(key, default)
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
    t = _table(raw, section, ("kind", "instruction", "assignee", "pick_state", "pick_label"))
    kind = _choice(_str(t, section, "kind"), section, "kind", TRACKER_KINDS)
    pick_state = _str(t, section, "pick_state")
    pick_label = _str(t, section, "pick_label")
    if kind == "github" and pick_state is not None:
        raise ConfigError(
            f'[{section}] pick_state is a linear key and cannot be used with kind = "github" (mixed tracker)'
        )
    if kind == "linear" and pick_label is not None:
        raise ConfigError(
            f'[{section}] pick_label is a github key and cannot be used with kind = "linear" (mixed tracker)'
        )
    if kind == "github" and pick_label is None:
        pick_label = DEFAULT_PICK_LABEL
    return WorkItemSource(
        kind=kind,
        instruction=_str(t, section, "instruction"),
        assignee=_str(t, section, "assignee"),
        pick_state=pick_state,
        pick_label=pick_label,
    )


def _parse_source(raw: Any) -> SourceConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"[source] must be a table, got {type(raw).__name__}")
    for key in raw:
        if key in TRACKER_KINDS:
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
    ci = None
    if "ci" in raw:
        t = _table(raw["ci"], _CI_SECTION, ("kind", "max_fix_attempts"))
        ci = CiSource(
            kind=_choice(_str(t, _CI_SECTION, "kind"), _CI_SECTION, "kind", CI_KINDS),
            max_fix_attempts=_posint(t, _CI_SECTION, "max_fix_attempts", None),
        )
    host = None
    if "host" in raw:
        t = _table(raw["host"], "source.host", ("token_cmd",))
        host = HostSource(token_cmd=_str(t, "source.host", "token_cmd"))
    return SourceConfig(work_item=work_item, ci=ci, host=host)


def _parse(data: dict[str, Any]) -> SisyphusConfig:
    top = ("project", "source", "agent", "limits", "merge", "digest", "knowledge")
    for key in data:
        if key not in top:
            raise ConfigError(f"unknown top-level key '{key}' (allowed: {', '.join(top)})")
    if "project" not in data:
        raise ConfigError("missing required [project] table")

    t = _table(data["project"], "project", ("repo_dir", "workspace_dir", "brief_path_template", "base_branch"))
    project = ProjectConfig(
        repo_dir=_str(t, "project", "repo_dir"),
        workspace_dir=_str(t, "project", "workspace_dir"),
        brief_path_template=_str(t, "project", "brief_path_template"),
        base_branch=_str(t, "project", "base_branch"),
    )
    source = _parse_source(data["source"]) if "source" in data else SourceConfig()

    agent = AgentConfig()
    if "agent" in data:
        t = _table(data["agent"], "agent", ("backend",))
        agent = AgentConfig(backend=_choice(_str(t, "agent", "backend"), "agent", "backend", AGENT_BACKENDS))

    limits = LimitsConfig()
    if "limits" in data:
        keys = ("max_run_minutes", "max_tickets_per_run", "max_fix_attempts_per_pr", "max_leaves_per_run")
        t = _table(data["limits"], "limits", keys)
        d = LimitsConfig()
        limits = LimitsConfig(
            max_run_minutes=_limit(t, "max_run_minutes", d.max_run_minutes),
            max_tickets_per_run=_limit(t, "max_tickets_per_run", d.max_tickets_per_run),
            max_fix_attempts_per_pr=_limit(t, "max_fix_attempts_per_pr", d.max_fix_attempts_per_pr),
            max_leaves_per_run=_limit(t, "max_leaves_per_run", d.max_leaves_per_run),
        )

    merge = MergeConfig()
    if "merge" in data:
        t = _table(data["merge"], "merge", ("enabled", "require_all_files_viewed", "max_changed_lines"))
        merge = MergeConfig(
            enabled=bool(_bool(t, "merge", "enabled", False)),
            require_all_files_viewed=_bool(t, "merge", "require_all_files_viewed", None),
            max_changed_lines=_posint(t, "merge", "max_changed_lines", None),
        )

    digest = DigestConfig()
    if "digest" in data:
        t = _table(data["digest"], "digest", ("path_template",))
        digest = DigestConfig(path_template=_str(t, "digest", "path_template"))

    knowledge = KnowledgeConfig()
    if "knowledge" in data:
        t = _table(data["knowledge"], "knowledge", ("sizing_file", "conventions_path"))
        knowledge = KnowledgeConfig(
            sizing_file=_str(t, "knowledge", "sizing_file"),
            conventions_path=_str(t, "knowledge", "conventions_path"),
        )

    return SisyphusConfig(
        project=project, source=source, agent=agent, limits=limits, merge=merge, digest=digest, knowledge=knowledge
    )


def load_config(path: str | Path) -> SisyphusConfig:
    """Load and validate a ``.sisyphus.toml`` file.

    Raises :class:`ConfigError` for unreadable files, TOML syntax errors and any
    schema violation.
    """
    p = Path(path)
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read config file {p}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {p}: {exc}") from exc
    return _parse(data)
