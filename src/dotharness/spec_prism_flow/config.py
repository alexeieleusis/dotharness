from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotharness.paths import default_knowledge_dir

DEFAULT_AGENT_BACKEND = "claude"
DEFAULT_CONFIG_FILE = ".spec-prism-flow.toml"


class ConfigError(ValueError):
    pass


def parse_backend(value: str, field_name: str) -> str:
    if value not in ("claude", "opencode"):
        raise ConfigError(f"Invalid {field_name} '{value}': must be 'claude' or 'opencode'")  # noqa: TRY003
    return value


def resolve_config_path(config_path_str: str | None) -> Path:
    return Path(config_path_str).expanduser() if config_path_str else Path(DEFAULT_CONFIG_FILE)


@dataclass
class AgentConfig:
    # Accepted and validated but unused (D6).
    backend: str = DEFAULT_AGENT_BACKEND


@dataclass
class PlanConfig:
    workspace_dir: Path
    phase_dir: Path
    conventions_path: Path | None = None


@dataclass
class ProseReviewConfig:
    enabled: bool = False
    backend: str = DEFAULT_AGENT_BACKEND


@dataclass
class HarnessSection:
    knowledge_dir: Path = field(default_factory=default_knowledge_dir)


@dataclass
class SpecPrismFlowConfig:
    agent: AgentConfig
    plan: PlanConfig
    harness: HarnessSection
    prose_review: ProseReviewConfig = field(default_factory=ProseReviewConfig)


def load_config(path: Path) -> SpecPrismFlowConfig:
    """Load the config. Unknown keys and tables (e.g. legacy ``[build]``, ``[review]``, ``[vibe_heal]``) are ignored."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")  # noqa: TRY003

    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Invalid TOML in config file: {e}") from None  # noqa: TRY003

    a = data.get("agent", {})
    agent = AgentConfig(backend=parse_backend(a.get("backend", DEFAULT_AGENT_BACKEND), "agent.backend"))

    p = data.get("plan", {})
    if not p.get("workspace_dir"):
        raise ConfigError("plan.workspace_dir is required")  # noqa: TRY003
    if not p.get("phase_dir"):
        raise ConfigError("plan.phase_dir is required")  # noqa: TRY003
    raw_conventions_path = p.get("conventions_path")
    plan = PlanConfig(
        workspace_dir=Path(p["workspace_dir"]).expanduser(),
        phase_dir=Path(p["phase_dir"]).expanduser(),
        conventions_path=Path(raw_conventions_path).expanduser() if raw_conventions_path else None,
    )

    h = data.get("harness", {})
    raw_knowledge_dir = h.get("knowledge_dir")
    harness = HarnessSection(
        knowledge_dir=Path(raw_knowledge_dir).expanduser() if raw_knowledge_dir else default_knowledge_dir(),
    )

    pr = data.get("prose_review", {})
    prose_review = ProseReviewConfig(
        enabled=pr.get("enabled", False),
        backend=parse_backend(pr.get("backend", DEFAULT_AGENT_BACKEND), "prose_review.backend"),
    )

    return SpecPrismFlowConfig(agent=agent, plan=plan, harness=harness, prose_review=prose_review)
