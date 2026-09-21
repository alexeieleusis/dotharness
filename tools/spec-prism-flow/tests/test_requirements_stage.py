import pytest
from click.testing import CliRunner

from spec_prism_flow import handoff, requirements_stage
from spec_prism_flow.cli import cli
from spec_prism_flow.config import (
    AgentConfig,
    BuildConfig,
    HarnessSection,
    PlanConfig,
    ReviewConfig,
    SpecPrismFlowConfig,
    VibeHealConfig,
)
from spec_prism_flow.overview_stage import OPEN_QUESTIONS_FILENAME, OVERVIEW_FILENAME
from spec_prism_flow.requirements_stage import (
    REQUIREMENTS_FILENAME,
    TEMPLATE_FILENAME,
    RequirementsError,
    build_requirements_prompt,
    run_draft_requirements,
)
from spec_prism_flow.workspace import init_workspace

TEMPLATE_TEXT = "# Requirements drafting template\n\nSome long unabridged instructions.\n"


def _make_cfg(tmp_path) -> SpecPrismFlowConfig:
    return SpecPrismFlowConfig(
        agent=AgentConfig(),
        plan=PlanConfig(workspace_dir=tmp_path / "workspace", phase_dir=tmp_path / "phases"),
        review=ReviewConfig(),
        vibe_heal=VibeHealConfig(),
        build=BuildConfig(),
        harness=HarnessSection(knowledge_dir=tmp_path / "knowledge"),
    )


def _write_template(cfg: SpecPrismFlowConfig, text: str = TEMPLATE_TEXT) -> None:
    template_dir = cfg.harness.knowledge_dir / "spec-prism-flow"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / TEMPLATE_FILENAME).write_text(text)


def test_build_requirements_prompt_contains_full_unabridged_template_then_overview():
    prompt = build_requirements_prompt(TEMPLATE_TEXT, "overview body", None, None)

    assert TEMPLATE_TEXT in prompt
    assert prompt.index(TEMPLATE_TEXT) == 0
    assert prompt.index("overview body") > prompt.index(TEMPLATE_TEXT)


def test_build_requirements_prompt_includes_open_questions_and_conventions_when_present():
    prompt = build_requirements_prompt(TEMPLATE_TEXT, "overview body", "unresolved question", "convention rules")

    assert "unresolved question" in prompt
    assert "convention rules" in prompt
    assert prompt.index("overview body") < prompt.index("unresolved question") < prompt.index("convention rules")


def test_build_requirements_prompt_omits_optional_sections_when_absent():
    prompt = build_requirements_prompt(TEMPLATE_TEXT, "overview body", None, None)

    assert "Open questions" not in prompt
    assert "Architecture / convention constraints" not in prompt


def test_run_draft_requirements_raises_when_manifest_missing(tmp_path):
    cfg = _make_cfg(tmp_path)
    cfg.plan.workspace_dir.mkdir(parents=True)

    with pytest.raises(RequirementsError, match="Manifest not found"):
        run_draft_requirements(cfg)


def test_run_draft_requirements_raises_when_overview_missing(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    cfg = _make_cfg(tmp_path)
    init_workspace(cfg.plan.workspace_dir, brief, None, None, [])

    with pytest.raises(RequirementsError, match="Overview not found"):
        run_draft_requirements(cfg)


def test_run_draft_requirements_raises_when_template_missing(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    cfg = _make_cfg(tmp_path)
    init_workspace(cfg.plan.workspace_dir, brief, None, None, [])
    (cfg.plan.workspace_dir / OVERVIEW_FILENAME).write_text("overview")

    with pytest.raises(RequirementsError, match="template not found"):
        run_draft_requirements(cfg)


def test_run_draft_requirements_raises_when_conventions_path_invalid(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    conventions = tmp_path / "CONVENTIONS.md"
    cfg = _make_cfg(tmp_path)
    init_workspace(cfg.plan.workspace_dir, brief, None, conventions, [])
    (cfg.plan.workspace_dir / OVERVIEW_FILENAME).write_text("overview")
    _write_template(cfg)

    with pytest.raises(RequirementsError, match="Conventions file not found"):
        run_draft_requirements(cfg)


def test_run_draft_requirements_writes_output_using_full_prompt(tmp_path, monkeypatch):
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    conventions = tmp_path / "CONVENTIONS.md"
    conventions.write_text("convention rules")
    cfg = _make_cfg(tmp_path)
    init_workspace(cfg.plan.workspace_dir, brief, None, conventions, [])
    (cfg.plan.workspace_dir / OVERVIEW_FILENAME).write_text("overview body")
    (cfg.plan.workspace_dir / OPEN_QUESTIONS_FILENAME).write_text("unresolved question")
    _write_template(cfg)

    captured = {}

    def _fake_run_handoff(prompt_text, workspace_dir, stage_name, output_filename=None):
        captured["prompt_text"] = prompt_text
        assert stage_name == "draft_requirements"
        assert output_filename == REQUIREMENTS_FILENAME
        (workspace_dir / REQUIREMENTS_FILENAME).write_text("requirements")

    monkeypatch.setattr(requirements_stage.handoff, "run_handoff", _fake_run_handoff)

    requirements_path = run_draft_requirements(cfg)

    assert requirements_path == cfg.plan.workspace_dir / REQUIREMENTS_FILENAME
    assert TEMPLATE_TEXT in captured["prompt_text"]
    assert "overview body" in captured["prompt_text"]
    assert "unresolved question" in captured["prompt_text"]
    assert "convention rules" in captured["prompt_text"]


def test_run_draft_requirements_raises_when_output_not_written(tmp_path, monkeypatch):
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    cfg = _make_cfg(tmp_path)
    init_workspace(cfg.plan.workspace_dir, brief, None, None, [])
    (cfg.plan.workspace_dir / OVERVIEW_FILENAME).write_text("overview")
    _write_template(cfg)

    monkeypatch.setattr(requirements_stage.handoff, "run_handoff", lambda *a, **k: None)

    with pytest.raises(RequirementsError, match=REQUIREMENTS_FILENAME):
        run_draft_requirements(cfg)


def test_run_draft_requirements_translates_real_handoff_error_cleanly(tmp_path, monkeypatch):
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    cfg = _make_cfg(tmp_path)
    init_workspace(cfg.plan.workspace_dir, brief, None, None, [])
    (cfg.plan.workspace_dir / OVERVIEW_FILENAME).write_text("overview")
    _write_template(cfg)

    def _raise_handoff_error(*a, **k):
        raise handoff.HandoffError("Expected output file not found: /nowhere")  # noqa: TRY003

    monkeypatch.setattr(requirements_stage.handoff, "run_handoff", _raise_handoff_error)

    with pytest.raises(RequirementsError):
        run_draft_requirements(cfg)


MINIMAL_TOML = """
[plan]
workspace_dir = "workspace"
phase_dir = "phases"

[harness]
knowledge_dir = "knowledge"
"""


def test_cli_draft_requirements_writes_requirements_and_prints_next_step(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    init_workspace(tmp_path / "workspace", brief, None, None, [])
    (tmp_path / "workspace" / OVERVIEW_FILENAME).write_text("overview body")
    (tmp_path / "knowledge" / "spec-prism-flow").mkdir(parents=True)
    (tmp_path / "knowledge" / "spec-prism-flow" / TEMPLATE_FILENAME).write_text(TEMPLATE_TEXT)

    def _fake_run_handoff(prompt_text, workspace_dir, stage_name, output_filename=None):
        (workspace_dir / REQUIREMENTS_FILENAME).write_text("requirements")

    monkeypatch.setattr(requirements_stage.handoff, "run_handoff", _fake_run_handoff)

    result = CliRunner().invoke(cli, ["plan", "draft-requirements"])

    assert result.exit_code == 0, result.output
    assert "plan decompose" in result.output


def test_cli_draft_requirements_fails_clearly_when_overview_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    init_workspace(tmp_path / "workspace", brief, None, None, [])

    result = CliRunner().invoke(cli, ["plan", "draft-requirements"])

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert "Overview not found" in result.output


def test_cli_draft_requirements_fails_clearly_when_real_handoff_error_raised(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    init_workspace(tmp_path / "workspace", brief, None, None, [])
    (tmp_path / "workspace" / OVERVIEW_FILENAME).write_text("overview body")
    (tmp_path / "knowledge" / "spec-prism-flow").mkdir(parents=True)
    (tmp_path / "knowledge" / "spec-prism-flow" / TEMPLATE_FILENAME).write_text(TEMPLATE_TEXT)

    def _raise_handoff_error(*a, **k):
        raise handoff.HandoffError("Expected output file not found: /nowhere")  # noqa: TRY003

    monkeypatch.setattr(requirements_stage.handoff, "run_handoff", _raise_handoff_error)

    result = CliRunner().invoke(cli, ["plan", "draft-requirements"])

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert "Error:" in result.output


def test_cli_draft_requirements_prompts_before_overwriting_existing_requirements(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    init_workspace(tmp_path / "workspace", brief, None, None, [])
    (tmp_path / "workspace" / OVERVIEW_FILENAME).write_text("overview body")
    (tmp_path / "knowledge" / "spec-prism-flow").mkdir(parents=True)
    (tmp_path / "knowledge" / "spec-prism-flow" / TEMPLATE_FILENAME).write_text(TEMPLATE_TEXT)
    (tmp_path / "workspace" / REQUIREMENTS_FILENAME).write_text("existing requirements")

    def _fake_run_handoff(prompt_text, workspace_dir, stage_name, output_filename=None):
        (workspace_dir / REQUIREMENTS_FILENAME).write_text("new requirements")

    monkeypatch.setattr(requirements_stage.handoff, "run_handoff", _fake_run_handoff)

    result = CliRunner().invoke(cli, ["plan", "draft-requirements"], input="n\n")

    assert result.exit_code != 0
    assert (tmp_path / "workspace" / REQUIREMENTS_FILENAME).read_text() == "existing requirements"


def test_cli_draft_requirements_yes_skips_confirmation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    init_workspace(tmp_path / "workspace", brief, None, None, [])
    (tmp_path / "workspace" / OVERVIEW_FILENAME).write_text("overview body")
    (tmp_path / "knowledge" / "spec-prism-flow").mkdir(parents=True)
    (tmp_path / "knowledge" / "spec-prism-flow" / TEMPLATE_FILENAME).write_text(TEMPLATE_TEXT)
    (tmp_path / "workspace" / REQUIREMENTS_FILENAME).write_text("existing requirements")

    def _fake_run_handoff(prompt_text, workspace_dir, stage_name, output_filename=None):
        (workspace_dir / REQUIREMENTS_FILENAME).write_text("new requirements")

    monkeypatch.setattr(requirements_stage.handoff, "run_handoff", _fake_run_handoff)

    result = CliRunner().invoke(cli, ["plan", "draft-requirements", "--yes"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "workspace" / REQUIREMENTS_FILENAME).read_text() == "new requirements"
