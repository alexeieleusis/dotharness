from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILENAME = ".spec-prism-flow.toml"


class ConfigError(ValueError):
    pass


@dataclass
class AgentConfig:
    backend: str = "claude"


@dataclass
class PlanConfig:
    workspace_dir: Path
    phase_dir: Path
    conventions_path: Path | None = None


@dataclass
class ReviewConfig:
    enabled: bool = False
    command: str = "harness"
    tool_dir: Path = field(default_factory=lambda: Path("~/.harness/tools/pr-review").expanduser())
    harness_config: Path | None = None


@dataclass
class VibeHealConfig:
    enabled: bool = False
    command: str = "vibe-heal"
    tool_dir: Path = field(default_factory=lambda: Path("~/.harness/vendor/vibe-heal").expanduser())


@dataclass
class BuildConfig:
    commands: list[str] = field(default_factory=list)
    workers: int = 1
    max_retry_cycles: int = 3
    state_dir: Path = field(
        default_factory=lambda: Path("~/.local/share/dotharness/spec-prism-flow/state").expanduser()
    )


@dataclass
class HarnessSection:
    knowledge_dir: Path = field(default_factory=lambda: Path("~/.harness/knowledge").expanduser())


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
    backend = a.get("backend", "claude")
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
        workspace_dir=Path(p["workspace_dir"]),
        phase_dir=Path(p["phase_dir"]),
        conventions_path=Path(raw_conventions_path) if raw_conventions_path else None,
    )

    r = data.get("review", {})
    review_enabled = r.get("enabled", False)
    raw_harness_config = r.get("harness_config")
    if review_enabled and not raw_harness_config:
        raise ConfigError("review.harness_config is required when review.enabled is true")  # noqa: TRY003
    review = ReviewConfig(
        enabled=review_enabled,
        command=r.get("command", "harness"),
        tool_dir=Path(r.get("tool_dir", "~/.harness/tools/pr-review")).expanduser(),
        harness_config=Path(raw_harness_config).expanduser() if raw_harness_config else None,
    )

    vh = data.get("vibe_heal", {})
    vibe_heal = VibeHealConfig(
        enabled=vh.get("enabled", False),
        command=vh.get("command", "vibe-heal"),
        tool_dir=Path(vh.get("tool_dir", "~/.harness/vendor/vibe-heal")).expanduser(),
    )

    b = data.get("build", {})
    build = BuildConfig(
        commands=list(b.get("commands", [])),
        workers=b.get("workers", 1),
        max_retry_cycles=b.get("max_retry_cycles", 3),
        state_dir=Path(b.get("state_dir", "~/.local/share/dotharness/spec-prism-flow/state")).expanduser(),
    )

    h = data.get("harness", {})
    harness = HarnessSection(
        knowledge_dir=Path(h.get("knowledge_dir", "~/.harness/knowledge")).expanduser(),
    )

    return SpecPrismFlowConfig(
        agent=agent,
        plan=plan,
        review=review,
        vibe_heal=vibe_heal,
        build=build,
        harness=harness,
    )
