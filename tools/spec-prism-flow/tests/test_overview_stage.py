import pytest
from click.testing import CliRunner

from spec_prism_flow import handoff, overview_stage
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
from spec_prism_flow.overview_stage import OPEN_QUESTIONS_FILENAME, OVERVIEW_FILENAME, OverviewError, run_draft_overview
from spec_prism_flow.workspace import init_workspace


def _make_cfg(tmp_path, conventions_path=None) -> SpecPrismFlowConfig:
    return SpecPrismFlowConfig(
        agent=AgentConfig(),
        plan=PlanConfig(
            workspace_dir=tmp_path / "workspace",
            phase_dir=tmp_path / "phases",
            conventions_path=conventions_path,
        ),
        review=ReviewConfig(),
        vibe_heal=VibeHealConfig(),
        build=BuildConfig(),
        harness=HarnessSection(knowledge_dir=tmp_path / "knowledge"),
    )


def test_build_overview_prompt_references_all_manifest_inputs(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("brief")
    manifest = {
        "brief": str(brief),
        "code": "/some/code",
        "conventions": "/some/CONVENTIONS.md",
        "links": ["https://example.com/a", "https://example.com/b"],
    }

    prompt = overview_stage.build_overview_prompt(manifest)

    assert str(brief) in prompt
    assert "/some/code" in prompt
    assert "/some/CONVENTIONS.md" in prompt
    assert "https://example.com/a" in prompt
    assert "https://example.com/b" in prompt
    assert OPEN_QUESTIONS_FILENAME in prompt


def test_build_overview_prompt_handles_no_optional_inputs(tmp_path):
    manifest = {"brief": str(tmp_path / "brief.md"), "code": None, "conventions": None, "links": []}

    prompt = overview_stage.build_overview_prompt(manifest)

    assert "(none provided)" in prompt


def test_run_draft_overview_raises_when_manifest_missing(tmp_path):
    cfg = _make_cfg(tmp_path)
    cfg.plan.workspace_dir.mkdir(parents=True)

    with pytest.raises(OverviewError, match="Manifest not found"):
        run_draft_overview(cfg)


def test_run_draft_overview_writes_output_and_reports_open_questions(tmp_path, monkeypatch):
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    cfg = _make_cfg(tmp_path)
    init_workspace(cfg.plan.workspace_dir, brief, None, None, [])

    def _fake_run_handoff(prompt_text, workspace_dir, stage_name, output_filename=None):
        assert stage_name == "draft_overview"
        assert output_filename == OVERVIEW_FILENAME
        (workspace_dir / OVERVIEW_FILENAME).write_text("overview")
        (workspace_dir / OPEN_QUESTIONS_FILENAME).write_text("- unresolved?")

    monkeypatch.setattr(overview_stage.handoff, "run_handoff", _fake_run_handoff)

    overview_path, open_questions_path = run_draft_overview(cfg)

    assert overview_path == cfg.plan.workspace_dir / OVERVIEW_FILENAME
    assert open_questions_path == cfg.plan.workspace_dir / OPEN_QUESTIONS_FILENAME


def test_run_draft_overview_open_questions_absent_is_not_an_error(tmp_path, monkeypatch):
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    cfg = _make_cfg(tmp_path)
    init_workspace(cfg.plan.workspace_dir, brief, None, None, [])

    def _fake_run_handoff(prompt_text, workspace_dir, stage_name, output_filename=None):
        (workspace_dir / OVERVIEW_FILENAME).write_text("overview")

    monkeypatch.setattr(overview_stage.handoff, "run_handoff", _fake_run_handoff)

    overview_path, open_questions_path = run_draft_overview(cfg)

    assert overview_path.exists()
    assert open_questions_path is None


def test_run_draft_overview_raises_when_output_not_written(tmp_path, monkeypatch):
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    cfg = _make_cfg(tmp_path)
    init_workspace(cfg.plan.workspace_dir, brief, None, None, [])

    monkeypatch.setattr(overview_stage.handoff, "run_handoff", lambda *a, **k: None)

    with pytest.raises(OverviewError, match=OVERVIEW_FILENAME):
        run_draft_overview(cfg)


def test_run_draft_overview_raises_when_output_is_stale(tmp_path, monkeypatch):
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    cfg = _make_cfg(tmp_path)
    init_workspace(cfg.plan.workspace_dir, brief, None, None, [])
    (cfg.plan.workspace_dir / OVERVIEW_FILENAME).write_text("stale overview")

    monkeypatch.setattr(overview_stage.handoff, "run_handoff", lambda *a, **k: None)

    with pytest.raises(OverviewError, match=OVERVIEW_FILENAME):
        run_draft_overview(cfg)

    assert (cfg.plan.workspace_dir / OVERVIEW_FILENAME).read_text() == "stale overview"


def test_run_draft_overview_translates_real_handoff_error_cleanly(tmp_path, monkeypatch):
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    cfg = _make_cfg(tmp_path)
    init_workspace(cfg.plan.workspace_dir, brief, None, None, [])

    def _raise_handoff_error(*a, **k):
        raise handoff.HandoffError("Expected output file not found: /nowhere")  # noqa: TRY003

    monkeypatch.setattr(overview_stage.handoff, "run_handoff", _raise_handoff_error)

    with pytest.raises(OverviewError):
        run_draft_overview(cfg)


MINIMAL_TOML = """
[plan]
workspace_dir = "workspace"
phase_dir = "phases"
"""


def test_cli_draft_overview_writes_overview_and_prints_next_step(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    init_workspace(tmp_path / "workspace", brief, None, None, [])

    def _fake_run_handoff(prompt_text, workspace_dir, stage_name, output_filename=None):
        (workspace_dir / OVERVIEW_FILENAME).write_text("overview")

    monkeypatch.setattr(overview_stage.handoff, "run_handoff", _fake_run_handoff)

    result = CliRunner().invoke(cli, ["plan", "draft-overview"])

    assert result.exit_code == 0, result.output
    assert "plan draft-requirements" in result.output
    assert "No open questions raised." in result.output


def test_cli_draft_overview_fails_clearly_when_output_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    init_workspace(tmp_path / "workspace", brief, None, None, [])

    monkeypatch.setattr(overview_stage.handoff, "run_handoff", lambda *a, **k: None)

    result = CliRunner().invoke(cli, ["plan", "draft-overview"])

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert OVERVIEW_FILENAME in result.output


def test_cli_draft_overview_fails_clearly_when_real_handoff_error_raised(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    init_workspace(tmp_path / "workspace", brief, None, None, [])

    def _raise_handoff_error(*a, **k):
        raise handoff.HandoffError("Expected output file not found: /nowhere")  # noqa: TRY003

    monkeypatch.setattr(overview_stage.handoff, "run_handoff", _raise_handoff_error)

    result = CliRunner().invoke(cli, ["plan", "draft-overview"])

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert "Error:" in result.output


def test_cli_draft_overview_rejects_missing_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)

    result = CliRunner().invoke(cli, ["plan", "draft-overview"])

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert "Manifest not found" in result.output


def test_cli_draft_overview_prompts_before_overwriting_existing_overview(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    init_workspace(tmp_path / "workspace", brief, None, None, [])
    (tmp_path / "workspace" / OVERVIEW_FILENAME).write_text("existing overview")

    def _fake_run_handoff(prompt_text, workspace_dir, stage_name, output_filename=None):
        (workspace_dir / OVERVIEW_FILENAME).write_text("new overview")

    monkeypatch.setattr(overview_stage.handoff, "run_handoff", _fake_run_handoff)

    result = CliRunner().invoke(cli, ["plan", "draft-overview"], input="n\n")

    assert result.exit_code != 0
    assert (tmp_path / "workspace" / OVERVIEW_FILENAME).read_text() == "existing overview"


def test_cli_draft_overview_yes_skips_confirmation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    init_workspace(tmp_path / "workspace", brief, None, None, [])
    (tmp_path / "workspace" / OVERVIEW_FILENAME).write_text("existing overview")

    def _fake_run_handoff(prompt_text, workspace_dir, stage_name, output_filename=None):
        (workspace_dir / OVERVIEW_FILENAME).write_text("new overview")

    monkeypatch.setattr(overview_stage.handoff, "run_handoff", _fake_run_handoff)

    result = CliRunner().invoke(cli, ["plan", "draft-overview", "--yes"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "workspace" / OVERVIEW_FILENAME).read_text() == "new overview"
