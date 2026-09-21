from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_AGENT_BACKEND = "claude"
DEFAULT_REVIEW_COMMAND = "harness"
DEFAULT_REVIEW_TOOL_DIR = "~/.harness/tools/pr-review"
DEFAULT_VIBE_HEAL_COMMAND = "vibe-heal"
DEFAULT_VIBE_HEAL_TOOL_DIR = "~/.harness/vendor/vibe-heal"
DEFAULT_BUILD_WORKERS = 1
DEFAULT_BUILD_MAX_RETRY_CYCLES = 3
DEFAULT_BUILD_STATE_DIR = "~/.local/share/dotharness/spec-prism-flow/state"
DEFAULT_HARNESS_KNOWLEDGE_DIR = "~/.harness/knowledge"


class ConfigError(ValueError):
    pass


@dataclass
class AgentConfig:
    backend: str = DEFAULT_AGENT_BACKEND


@dataclass
class PlanConfig:
    workspace_dir: Path
    phase_dir: Path
    conventions_path: Path | None = None


@dataclass
class ReviewConfig:
    enabled: bool = False
    command: str = DEFAULT_REVIEW_COMMAND
    tool_dir: Path = field(default_factory=lambda: Path(DEFAULT_REVIEW_TOOL_DIR).expanduser())
    harness_config: Path | None = None


@dataclass
class VibeHealConfig:
    enabled: bool = False
    command: str = DEFAULT_VIBE_HEAL_COMMAND
    tool_dir: Path = field(default_factory=lambda: Path(DEFAULT_VIBE_HEAL_TOOL_DIR).expanduser())


@dataclass
class BuildConfig:
    commands: list[str] = field(default_factory=list)
    workers: int = DEFAULT_BUILD_WORKERS
    max_retry_cycles: int = DEFAULT_BUILD_MAX_RETRY_CYCLES
    state_dir: Path = field(default_factory=lambda: Path(DEFAULT_BUILD_STATE_DIR).expanduser())


@dataclass
class HarnessSection:
    knowledge_dir: Path = field(default_factory=lambda: Path(DEFAULT_HARNESS_KNOWLEDGE_DIR).expanduser())


@dataclass
class SpecPrismFlowConfig:
    agent: AgentConfig
    plan: PlanConfig
    review: ReviewConfig
    vibe_heal: VibeHealConfig
    build: BuildConfig
    harness: HarnessSection


def load_config(path: Path) -> SpecPrismFlowConfig:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")  # noqa: TRY003

    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Invalid TOML in config file: {e}") from None  # noqa: TRY003

    a = data.get("agent", {})
    backend = a.get("backend", DEFAULT_AGENT_BACKEND)
    if backend not in ("claude", "opencode"):
        raise ConfigError(f"Invalid agent.backend '{backend}': must be 'claude' or 'opencode'")  # noqa: TRY003
    agent = AgentConfig(backend=backend)

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

    r = data.get("review", {})
    review_enabled = r.get("enabled", False)
    raw_harness_config = r.get("harness_config")
    if review_enabled and not raw_harness_config:
        raise ConfigError("review.harness_config is required when review.enabled is true")  # noqa: TRY003
    review = ReviewConfig(
        enabled=review_enabled,
        command=r.get("command", DEFAULT_REVIEW_COMMAND),
        tool_dir=Path(r.get("tool_dir", DEFAULT_REVIEW_TOOL_DIR)).expanduser(),
        harness_config=Path(raw_harness_config).expanduser() if raw_harness_config else None,
    )

    vh = data.get("vibe_heal", {})
    vibe_heal = VibeHealConfig(
        enabled=vh.get("enabled", False),
        command=vh.get("command", DEFAULT_VIBE_HEAL_COMMAND),
        tool_dir=Path(vh.get("tool_dir", DEFAULT_VIBE_HEAL_TOOL_DIR)).expanduser(),
    )

    b = data.get("build", {})
    raw_commands = b.get("commands", [])
    if not isinstance(raw_commands, list) or not all(isinstance(c, str) for c in raw_commands):
        raise ConfigError("build.commands must be a list of strings")  # noqa: TRY003
    build = BuildConfig(
        commands=raw_commands,
        workers=b.get("workers", DEFAULT_BUILD_WORKERS),
        max_retry_cycles=b.get("max_retry_cycles", DEFAULT_BUILD_MAX_RETRY_CYCLES),
        state_dir=Path(b.get("state_dir", DEFAULT_BUILD_STATE_DIR)).expanduser(),
    )

    h = data.get("harness", {})
    harness = HarnessSection(
        knowledge_dir=Path(h.get("knowledge_dir", DEFAULT_HARNESS_KNOWLEDGE_DIR)).expanduser(),
    )

    return SpecPrismFlowConfig(
        agent=agent,
        plan=plan,
        review=review,
        vibe_heal=vibe_heal,
        build=build,
        harness=harness,
    )
