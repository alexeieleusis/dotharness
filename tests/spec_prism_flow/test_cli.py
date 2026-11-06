import json

from typer.testing import CliRunner

from dotharness.spec_prism_flow.cli import cli

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
    assert "does not exist" in result.output


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
