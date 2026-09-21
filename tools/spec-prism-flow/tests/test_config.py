from pathlib import Path

import pytest

from spec_prism_flow.config import ConfigError, load_config

MINIMAL_TOML = """
[plan]
workspace_dir = "docs"
phase_dir = "docs/phases"
"""


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / ".spec-prism-flow.toml"
    p.write_text(content)
    return p


def test_load_minimal(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL_TOML))

    assert cfg.plan.workspace_dir == Path("docs")
    assert cfg.plan.phase_dir == Path("docs/phases")
    assert cfg.plan.conventions_path is None
    assert cfg.agent.backend == "claude"


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / ".spec-prism-flow.toml")


def test_malformed_toml_raises_config_error(tmp_path):
    p = _write(
        tmp_path,
        """
[plan
workspace_dir = "docs"
""",
    )

    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_config(p)


def test_missing_workspace_dir_raises(tmp_path):
    p = _write(tmp_path, '[plan]\nphase_dir = "docs/phases"\n')

    with pytest.raises(ConfigError, match=r"plan\.workspace_dir"):
        load_config(p)


def test_missing_phase_dir_raises(tmp_path):
    p = _write(tmp_path, '[plan]\nworkspace_dir = "docs"\n')

    with pytest.raises(ConfigError, match=r"plan\.phase_dir"):
        load_config(p)


def test_conventions_path_parsed(tmp_path):
    p = _write(
        tmp_path,
        MINIMAL_TOML
        + """
conventions_path = "docs/CONVENTIONS.md"
""",
    )

    cfg = load_config(p)

    assert cfg.plan.conventions_path == Path("docs/CONVENTIONS.md")


def test_agent_backend_defaults_to_claude_when_section_absent(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL_TOML))

    assert cfg.agent.backend == "claude"


def test_agent_backend_parsed(tmp_path):
    p = _write(tmp_path, MINIMAL_TOML + '\n[agent]\nbackend = "opencode"\n')

    cfg = load_config(p)

    assert cfg.agent.backend == "opencode"


def test_invalid_agent_backend_raises(tmp_path):
    p = _write(tmp_path, MINIMAL_TOML + '\n[agent]\nbackend = "bogus"\n')

    with pytest.raises(ConfigError, match=r"agent\.backend"):
        load_config(p)


def test_build_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL_TOML))

    assert cfg.build.commands == []
    assert cfg.build.workers == 1
    assert cfg.build.max_retry_cycles == 3
    assert str(cfg.build.state_dir).endswith("dotharness/spec-prism-flow/state")
    assert not str(cfg.build.state_dir).startswith("~")


def test_build_parsed(tmp_path):
    p = _write(
        tmp_path,
        MINIMAL_TOML
        + """
[build]
commands = ["pnpm build", "pnpm lint", "pnpm test"]
workers = 4
max_retry_cycles = 5
state_dir = "~/custom/state"
""",
    )

    cfg = load_config(p)

    assert cfg.build.commands == ["pnpm build", "pnpm lint", "pnpm test"]
    assert cfg.build.workers == 4
    assert cfg.build.max_retry_cycles == 5
    assert not str(cfg.build.state_dir).startswith("~")
    assert str(cfg.build.state_dir).endswith("custom/state")


def test_review_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL_TOML))

    assert cfg.review.enabled is False
    assert cfg.review.command == "harness"
    assert not str(cfg.review.tool_dir).startswith("~")
    assert cfg.review.harness_config is None


def test_review_enabled_without_harness_config_raises(tmp_path):
    p = _write(tmp_path, MINIMAL_TOML + "\n[review]\nenabled = true\n")

    with pytest.raises(ConfigError, match=r"review\.harness_config"):
        load_config(p)


def test_review_enabled_with_harness_config_parses(tmp_path):
    p = _write(
        tmp_path,
        MINIMAL_TOML
        + """
[review]
enabled = true
harness_config = "~/.harness-spec_prism_flow.toml"
""",
    )

    cfg = load_config(p)

    assert cfg.review.enabled is True
    assert not str(cfg.review.harness_config).startswith("~")
    assert str(cfg.review.harness_config).endswith(".harness-spec_prism_flow.toml")


def test_vibe_heal_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL_TOML))

    assert cfg.vibe_heal.enabled is False
    assert cfg.vibe_heal.command == "vibe-heal"
    assert not str(cfg.vibe_heal.tool_dir).startswith("~")


def test_harness_knowledge_dir_default(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL_TOML))

    assert not str(cfg.harness.knowledge_dir).startswith("~")
    assert str(cfg.harness.knowledge_dir).endswith(".harness/knowledge")


def test_harness_knowledge_dir_parsed(tmp_path):
    p = _write(
        tmp_path,
        MINIMAL_TOML
        + """
[harness]
knowledge_dir = "~/custom/knowledge"
""",
    )

    cfg = load_config(p)

    assert not str(cfg.harness.knowledge_dir).startswith("~")
    assert str(cfg.harness.knowledge_dir).endswith("custom/knowledge")
