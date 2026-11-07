import json
from pathlib import Path

from typer.testing import CliRunner

from dotharness.spec_prism_flow import decompose, draft_phases, overview_stage, requirements_stage, review
from dotharness.spec_prism_flow.cli import cli
from dotharness.spec_prism_flow.decompose_journal import JOURNAL_FILENAME

MINIMAL_TOML = """
[plan]
workspace_dir = "workspace"
phase_dir = "phases"
"""


def _write_config(tmp_path) -> None:
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)


def test_cli_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "plan" in result.output
    assert "build" not in result.output


def test_plan_group_lists_subcommands(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["plan", "--help"])

    assert result.exit_code == 0
    for name in ("init", "draft-overview", "draft-requirements", "decompose", "draft-phases", "review"):
        assert name in result.output


def test_plan_init_creates_manifest(tmp_path, monkeypatch, runner: CliRunner) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")

    result = runner.invoke(cli, ["plan", "init", "brief.md"])

    assert result.exit_code == 0, result.output
    manifest = json.loads((tmp_path / "workspace" / "init_manifest.json").read_text())
    assert manifest["code"] is None
    assert manifest["conventions"] is None
    assert manifest["links"] == []
    assert manifest["brief"].endswith("brief.md")


def test_plan_init_rejects_missing_brief(tmp_path, monkeypatch, runner: CliRunner) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)

    result = runner.invoke(cli, ["plan", "init", "missing-brief.md"])

    assert result.exit_code == 1
    assert "BRIEF_PATH does not exist:" in result.output


def test_plan_init_rejects_missing_config(tmp_path, monkeypatch, runner: CliRunner) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")

    result = runner.invoke(cli, ["plan", "init", "brief.md"])

    assert result.exit_code == 1
    assert "Config file not found" in result.output
    assert "Traceback" not in result.output


def test_plan_init_rejects_missing_code_path(tmp_path, monkeypatch, runner: CliRunner) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")

    result = runner.invoke(cli, ["plan", "init", "brief.md", "--code", "missing-code"])

    assert result.exit_code == 1
    assert "--code" in result.output


def test_plan_init_accepts_links_verbatim(tmp_path, monkeypatch, runner: CliRunner) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")

    result = runner.invoke(cli, ["plan", "init", "brief.md", "--links", "https://example.com/a, https://example.com/b"])

    assert result.exit_code == 0, result.output
    manifest = json.loads((tmp_path / "workspace" / "init_manifest.json").read_text())
    assert manifest["links"] == ["https://example.com/a", "https://example.com/b"]


def test_plan_init_prompts_before_overwriting_existing_manifest(tmp_path, monkeypatch, runner: CliRunner) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")
    runner.invoke(cli, ["plan", "init", "brief.md"])

    result = runner.invoke(cli, ["plan", "init", "brief.md"], input="n\n")

    assert result.exit_code == 1
    assert "Aborted!" in result.output
    manifest = json.loads((tmp_path / "workspace" / "init_manifest.json").read_text())
    assert manifest["brief"].endswith("brief.md")


def test_plan_init_yes_skips_confirmation(tmp_path, monkeypatch, runner: CliRunner) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")
    runner.invoke(cli, ["plan", "init", "brief.md"])

    result = runner.invoke(cli, ["plan", "init", "brief.md", "--yes"])

    assert result.exit_code == 0, result.output


def test_plan_decompose_help_documents_options(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["plan", "decompose", "--help"])

    assert result.exit_code == 0
    assert "--depth-cap" in result.output
    assert "--resume" in result.output
    assert "--yes" in result.output


def test_plan_review_fails_clearly_without_phase_corpus(tmp_path, monkeypatch, runner: CliRunner) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)

    result = runner.invoke(cli, ["plan", "review"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_plan_init_accepts_existing_code_and_conventions(tmp_path, monkeypatch, runner: CliRunner) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")
    (tmp_path / "code").mkdir()
    (tmp_path / "conv.md").write_text("conventions")

    result = runner.invoke(cli, ["plan", "init", "brief.md", "--code", "code", "--conventions", "conv.md"])

    assert result.exit_code == 0, result.output
    manifest = json.loads((tmp_path / "workspace" / "init_manifest.json").read_text())
    assert manifest["code"].endswith("code")
    assert manifest["conventions"].endswith("conv.md")


def test_plan_init_rejects_missing_conventions_path(tmp_path, monkeypatch, runner: CliRunner) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")

    result = runner.invoke(cli, ["plan", "init", "brief.md", "--conventions", "nope"])

    assert result.exit_code == 1
    assert "--conventions does not exist:" in result.output


def test_plan_init_rejects_unreadable_brief(tmp_path, monkeypatch, runner: CliRunner) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").mkdir()  # exists but open() raises OSError

    result = runner.invoke(cli, ["plan", "init", "brief.md"])

    assert result.exit_code == 1
    assert "BRIEF_PATH is not readable:" in result.output


def _raiser(exc: Exception):
    def _raise(*args, **kwargs):
        raise exc

    return _raise


def _workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


# --- draft-overview ---


def test_draft_overview_reports_open_questions(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(overview_stage, "run_draft_overview", lambda cfg: (Path("ov.md"), Path("OQ.md")))

    result = runner.invoke(cli, ["plan", "draft-overview"])

    assert result.exit_code == 0, result.output
    assert "Wrote ov.md" in result.output
    assert "Open questions: OQ.md" in result.output


def test_draft_overview_reports_no_open_questions(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(overview_stage, "run_draft_overview", lambda cfg: (Path("ov.md"), None))

    result = runner.invoke(cli, ["plan", "draft-overview"])

    assert result.exit_code == 0, result.output
    assert "No open questions raised." in result.output


def test_draft_overview_error_exits_1(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)

    monkeypatch.setattr(overview_stage, "run_draft_overview", _raiser(overview_stage.OverviewError("overview boom")))

    result = runner.invoke(cli, ["plan", "draft-overview"])

    assert result.exit_code == 1
    assert "overview boom" in result.output


def test_draft_overview_overwrite_prompt(tmp_path, monkeypatch, runner: CliRunner) -> None:
    ws = _workspace(tmp_path, monkeypatch)
    (ws / overview_stage.OVERVIEW_FILENAME).write_text("old")
    calls = []
    monkeypatch.setattr(overview_stage, "run_draft_overview", lambda cfg: calls.append(1) or (Path("ov.md"), None))

    declined = runner.invoke(cli, ["plan", "draft-overview"], input="n\n")
    assert declined.exit_code == 1
    assert "Aborted!" in declined.output
    assert calls == []

    skipped = runner.invoke(cli, ["plan", "draft-overview", "--yes"])
    assert skipped.exit_code == 0, skipped.output
    assert calls == [1]


# --- draft-requirements ---


def test_draft_requirements_success(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(requirements_stage, "run_draft_requirements", lambda cfg: Path("req.md"))

    result = runner.invoke(cli, ["plan", "draft-requirements"])

    assert result.exit_code == 0, result.output
    assert "Wrote req.md" in result.output


def test_draft_requirements_error_exits_1(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)

    monkeypatch.setattr(
        requirements_stage, "run_draft_requirements", _raiser(requirements_stage.RequirementsError("req boom"))
    )

    result = runner.invoke(cli, ["plan", "draft-requirements"])

    assert result.exit_code == 1
    assert "req boom" in result.output


def test_draft_requirements_overwrite_prompt(tmp_path, monkeypatch, runner: CliRunner) -> None:
    ws = _workspace(tmp_path, monkeypatch)
    (ws / requirements_stage.REQUIREMENTS_FILENAME).write_text("old")
    calls = []
    monkeypatch.setattr(requirements_stage, "run_draft_requirements", lambda cfg: calls.append(1) or Path("req.md"))

    declined = runner.invoke(cli, ["plan", "draft-requirements"], input="n\n")
    assert declined.exit_code == 1
    assert "Aborted!" in declined.output
    assert calls == []

    skipped = runner.invoke(cli, ["plan", "draft-requirements", "--yes"])
    assert skipped.exit_code == 0, skipped.output
    assert calls == [1]


# --- decompose ---


def _stub_decompose(monkeypatch):
    calls = []

    def fake(cfg, depth_cap, resume):
        calls.append({"depth_cap": depth_cap, "resume": resume})
        return Path("tree.json"), Path("graph.json")

    monkeypatch.setattr(decompose, "run_decompose", fake)
    return calls


def test_decompose_success_passes_depth_cap(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)
    calls = _stub_decompose(monkeypatch)

    result = runner.invoke(cli, ["plan", "decompose", "--depth-cap", "2"])

    assert result.exit_code == 0, result.output
    assert "Wrote tree.json" in result.output
    assert "Wrote graph.json" in result.output
    assert calls == [{"depth_cap": 2, "resume": False}]


def test_decompose_error_exits_1(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)

    monkeypatch.setattr(decompose, "run_decompose", _raiser(decompose.DecomposeError("decompose boom")))

    result = runner.invoke(cli, ["plan", "decompose"])

    assert result.exit_code == 1
    assert "decompose boom" in result.output


def test_decompose_journal_prompt_yes_resumes(tmp_path, monkeypatch, runner: CliRunner) -> None:
    ws = _workspace(tmp_path, monkeypatch)
    (ws / JOURNAL_FILENAME).write_text("{}")
    (ws / decompose.TREE_FILENAME).write_text("{}")
    calls = _stub_decompose(monkeypatch)

    result = runner.invoke(cli, ["plan", "decompose"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Resume it?" in result.output
    assert "Overwrite existing" not in result.output  # tree prompt skipped when resuming
    assert calls == [{"depth_cap": decompose.DEFAULT_DEPTH_CAP, "resume": True}]


def test_decompose_journal_prompt_no_reaches_overwrite_confirm(tmp_path, monkeypatch, runner: CliRunner) -> None:
    ws = _workspace(tmp_path, monkeypatch)
    (ws / JOURNAL_FILENAME).write_text("{}")
    (ws / decompose.TREE_FILENAME).write_text("{}")
    calls = _stub_decompose(monkeypatch)

    declined = runner.invoke(cli, ["plan", "decompose"], input="n\nn\n")
    assert declined.exit_code == 1
    assert "Resume it?" in declined.output
    assert "Overwrite existing" in declined.output
    assert "Aborted!" in declined.output
    assert calls == []

    accepted = runner.invoke(cli, ["plan", "decompose"], input="n\ny\n")
    assert accepted.exit_code == 0, accepted.output
    assert calls == [{"depth_cap": decompose.DEFAULT_DEPTH_CAP, "resume": False}]


def test_decompose_resume_flag_bypasses_prompts(tmp_path, monkeypatch, runner: CliRunner) -> None:
    ws = _workspace(tmp_path, monkeypatch)
    (ws / JOURNAL_FILENAME).write_text("{}")
    (ws / decompose.TREE_FILENAME).write_text("{}")
    calls = _stub_decompose(monkeypatch)

    result = runner.invoke(cli, ["plan", "decompose", "--resume"])

    assert result.exit_code == 0, result.output
    assert "Resume it?" not in result.output
    assert "Overwrite existing" not in result.output
    assert calls == [{"depth_cap": decompose.DEFAULT_DEPTH_CAP, "resume": True}]


def test_decompose_yes_flag_bypasses_prompts(tmp_path, monkeypatch, runner: CliRunner) -> None:
    ws = _workspace(tmp_path, monkeypatch)
    (ws / JOURNAL_FILENAME).write_text("{}")
    (ws / decompose.TREE_FILENAME).write_text("{}")
    calls = _stub_decompose(monkeypatch)

    result = runner.invoke(cli, ["plan", "decompose", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Resume it?" not in result.output
    assert "Overwrite existing" not in result.output
    assert calls == [{"depth_cap": decompose.DEFAULT_DEPTH_CAP, "resume": False}]


# --- draft-phases ---


def _phases_result(outliers=None):
    return draft_phases.DraftPhasesResult(
        written=[Path("01-a.md")], leaves_drafted=1, internal_nodes_skipped=2, outliers=outliers or []
    )


def test_draft_phases_success_lists_outliers(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(draft_phases, "target_phase_paths", lambda cfg: [tmp_path / "01-a.md"])
    monkeypatch.setattr(draft_phases, "stale_phase_paths", lambda cfg: [])
    monkeypatch.setattr(draft_phases, "run_draft_phases", lambda cfg: _phases_result(["01-a.md too big"]))

    result = runner.invoke(cli, ["plan", "draft-phases"])

    assert result.exit_code == 0, result.output
    assert "1 leaves drafted, 2 internal nodes skipped." in result.output
    assert "Wrote 01-a.md" in result.output
    assert "Sizing outliers (flagged, not blocking):" in result.output
    assert "  - 01-a.md too big" in result.output


def test_draft_phases_overwrite_prompt_lists_file_names(tmp_path, monkeypatch, runner: CliRunner) -> None:
    existing = tmp_path / "01-a.md"
    existing.write_text("old")
    _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(draft_phases, "target_phase_paths", lambda cfg: [existing, tmp_path / "02-b.md"])
    monkeypatch.setattr(draft_phases, "stale_phase_paths", lambda cfg: [])
    calls = []
    monkeypatch.setattr(draft_phases, "run_draft_phases", lambda cfg: calls.append(1) or _phases_result())

    declined = runner.invoke(cli, ["plan", "draft-phases"], input="n\n")
    assert declined.exit_code == 1
    assert "Overwrite existing phase file(s) (01-a.md)?" in declined.output
    assert "02-b.md" not in declined.output
    assert "Aborted!" in declined.output
    assert calls == []

    skipped = runner.invoke(cli, ["plan", "draft-phases", "--yes"])
    assert skipped.exit_code == 0, skipped.output
    assert calls == [1]


def test_draft_phases_stale_prompt(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(draft_phases, "target_phase_paths", lambda cfg: [])
    monkeypatch.setattr(draft_phases, "stale_phase_paths", lambda cfg: [tmp_path / "09-old.md"])
    monkeypatch.setattr(draft_phases, "run_draft_phases", lambda cfg: _phases_result())

    result = runner.invoke(cli, ["plan", "draft-phases"], input="n\n")

    assert result.exit_code == 1
    assert "Delete stale phase file(s) no longer produced by the tree (09-old.md)?" in result.output
    assert "Aborted!" in result.output


def test_draft_phases_error_from_target_paths_exits_1(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)

    monkeypatch.setattr(draft_phases, "target_phase_paths", _raiser(draft_phases.DraftPhasesError("target boom")))

    result = runner.invoke(cli, ["plan", "draft-phases"])

    assert result.exit_code == 1
    assert "target boom" in result.output


def test_draft_phases_error_from_run_exits_1(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(draft_phases, "target_phase_paths", lambda cfg: [])
    monkeypatch.setattr(draft_phases, "stale_phase_paths", lambda cfg: [])

    monkeypatch.setattr(draft_phases, "run_draft_phases", _raiser(draft_phases.DraftPhasesError("run boom")))

    result = runner.invoke(cli, ["plan", "draft-phases"])

    assert result.exit_code == 1
    assert "run boom" in result.output


# --- review ---


def test_review_all_pass_exits_0(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)
    report = review.ReviewReport(checks=[review.CheckResult("deps", []), review.CheckResult("naming", [])])
    monkeypatch.setattr(review, "run_review", lambda cfg: report)

    result = runner.invoke(cli, ["plan", "review"])

    assert result.exit_code == 0, result.output
    assert "deps: PASS" in result.output
    assert "naming: PASS" in result.output


def test_review_failure_prints_violations_and_exits_1(tmp_path, monkeypatch, runner: CliRunner) -> None:
    _workspace(tmp_path, monkeypatch)
    report = review.ReviewReport(checks=[review.CheckResult("deps", ["bad edge"]), review.CheckResult("naming", [])])
    monkeypatch.setattr(review, "run_review", lambda cfg: report)

    result = runner.invoke(cli, ["plan", "review"])

    assert result.exit_code == 1
    assert "deps: FAIL" in result.output
    assert "  - bad edge" in result.output
    assert "naming: PASS" in result.output
