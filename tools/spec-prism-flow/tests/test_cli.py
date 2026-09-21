import json

from click.testing import CliRunner

from spec_prism_flow.cli import cli


def test_cli_help() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "plan" in result.output
    assert "build" in result.output


def test_plan_and_build_are_registered_groups() -> None:
    runner = CliRunner()

    assert runner.invoke(cli, ["plan", "--help"]).exit_code == 0
    assert runner.invoke(cli, ["build", "--help"]).exit_code == 0


MINIMAL_TOML = """
[plan]
workspace_dir = "workspace"
phase_dir = "phases"
"""


def _write_config(tmp_path) -> None:
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)


def test_plan_init_creates_manifest(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")
    runner = CliRunner()

    result = runner.invoke(cli, ["plan", "init", "brief.md"])

    assert result.exit_code == 0, result.output
    manifest = json.loads((tmp_path / "workspace" / "init_manifest.json").read_text())
    assert manifest["code"] is None
    assert manifest["conventions"] is None
    assert manifest["links"] == []
    assert manifest["brief"].endswith("brief.md")


def test_plan_init_rejects_missing_brief(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["plan", "init", "missing-brief.md"])

    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_plan_init_rejects_missing_code_path(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")
    runner = CliRunner()

    result = runner.invoke(cli, ["plan", "init", "brief.md", "--code", "missing-code"])

    assert result.exit_code != 0
    assert "--code" in result.output


def test_plan_init_accepts_links_verbatim(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")
    runner = CliRunner()

    result = runner.invoke(cli, ["plan", "init", "brief.md", "--links", "https://example.com/a, https://example.com/b"])

    assert result.exit_code == 0, result.output
    manifest = json.loads((tmp_path / "workspace" / "init_manifest.json").read_text())
    assert manifest["links"] == ["https://example.com/a", "https://example.com/b"]


def test_plan_init_prompts_before_overwriting_existing_manifest(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")
    runner = CliRunner()
    runner.invoke(cli, ["plan", "init", "brief.md"])

    result = runner.invoke(cli, ["plan", "init", "brief.md"], input="n\n")

    assert result.exit_code != 0
    manifest = json.loads((tmp_path / "workspace" / "init_manifest.json").read_text())
    assert manifest["brief"].endswith("brief.md")


def test_plan_init_yes_skips_confirmation(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "brief.md").write_text("brief content")
    runner = CliRunner()
    runner.invoke(cli, ["plan", "init", "brief.md"])

    result = runner.invoke(cli, ["plan", "init", "brief.md", "--yes"])

    assert result.exit_code == 0, result.output
