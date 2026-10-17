import tomllib
from unittest.mock import patch

from click.testing import CliRunner

from harness.cli import TEMPLATE, cli


def test_run_review_prs_dispatches(minimal_toml, tmp_xdg):
    runner = CliRunner()
    with patch("harness.runners.review_prs.run") as mock_run:
        result = runner.invoke(cli, ["run", "--config", str(minimal_toml), "review-prs"])
    assert result.exit_code == 0
    mock_run.assert_called_once()


def test_run_review_prs_with_pr_url(minimal_toml, tmp_xdg):
    runner = CliRunner()
    with patch("harness.runners.review_prs.run") as mock_run:
        runner.invoke(
            cli,
            [
                "run",
                "--config",
                str(minimal_toml),
                "review-prs",
                "--pr",
                "https://github.com/acme/frontend/pull/1",
            ],
        )
    _, kwargs = mock_run.call_args
    assert kwargs.get("pr_url") == "https://github.com/acme/frontend/pull/1"


def test_run_review_requested_dispatches(minimal_toml, tmp_xdg):
    runner = CliRunner()
    with patch("harness.runners.review_requested.run") as mock_run:
        _ = runner.invoke(cli, ["run", "--config", str(minimal_toml), "review-requested"])
    mock_run.assert_called_once()


def test_run_review_requested_with_pr_url(minimal_toml, tmp_xdg):
    runner = CliRunner()
    with patch("harness.runners.review_requested.run") as mock_run:
        runner.invoke(
            cli,
            [
                "run",
                "--config",
                str(minimal_toml),
                "review-requested",
                "--pr",
                "https://github.com/acme/frontend/pull/1",
            ],
        )
    _, kwargs = mock_run.call_args
    assert kwargs.get("pr_url") == "https://github.com/acme/frontend/pull/1"


def test_run_focused_review_dispatches(minimal_toml, tmp_xdg):
    runner = CliRunner()
    with patch("harness.runners.focused_review.run") as mock_run:
        result = runner.invoke(cli, ["run", "--config", str(minimal_toml), "focused-review"])
    assert result.exit_code == 0
    mock_run.assert_called_once()


def test_init_creates_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".harness.toml").exists()


def test_init_aborts_if_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".harness.toml").write_text("existing")
    runner = CliRunner()
    result = runner.invoke(cli, ["init"])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_validate_exits_0_on_valid(minimal_toml):
    runner = CliRunner()
    with patch("shutil.which", return_value="/usr/bin/stub"):
        result = runner.invoke(cli, ["validate", "--config", str(minimal_toml)])
    assert result.exit_code == 0


def test_validate_exits_1_on_invalid(tmp_path):
    bad = tmp_path / ".harness.toml"
    bad.write_text("[harness]\n[repo]\nworking_dir = '/tmp'\n")  # missing name
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", "--config", str(bad)])
    assert result.exit_code == 1


def test_run_all_calls_runners_in_order(minimal_toml, tmp_xdg):
    _enable_auto_approve(minimal_toml)
    runner = CliRunner()
    call_order = []

    def make_side_effect(name):
        def _fn(*args, **kwargs):
            call_order.append(name)

        return _fn

    with (
        patch("harness.runners.review_prs.run", side_effect=make_side_effect("review_prs")),
        patch("harness.runners.focused_review.run", side_effect=make_side_effect("focused_review")),
        patch("harness.runners.self_review.run", side_effect=make_side_effect("self_review")),
        patch("harness.runners.review_requested.run", side_effect=make_side_effect("review_requested")),
        patch("harness.runners.address_comments.run", side_effect=make_side_effect("address_comments")),
        patch("harness.runners.auto_approve.run", side_effect=make_side_effect("auto_approve")),
    ):
        result = runner.invoke(cli, ["run", "--config", str(minimal_toml), "all"])

    assert result.exit_code == 0, result.output
    assert call_order == [
        "review_prs",
        "focused_review",
        "self_review",
        "review_requested",
        "address_comments",
        "auto_approve",
    ]


def test_run_all_continues_after_failure_and_exits_nonzero(minimal_toml, tmp_xdg):
    _enable_auto_approve(minimal_toml)
    runner = CliRunner()
    call_order = []

    def make_side_effect(name, fail=False):
        def _fn(*args, **kwargs):
            call_order.append(name)
            if fail:
                raise RuntimeError(f"{name} exploded")  # noqa: TRY003

        return _fn

    with (
        patch("harness.runners.review_prs.run", side_effect=make_side_effect("review_prs", fail=True)),
        patch("harness.runners.focused_review.run", side_effect=make_side_effect("focused_review")),
        patch("harness.runners.self_review.run", side_effect=make_side_effect("self_review")),
        patch("harness.runners.review_requested.run", side_effect=make_side_effect("review_requested")),
        patch("harness.runners.address_comments.run", side_effect=make_side_effect("address_comments")),
        patch("harness.runners.auto_approve.run", side_effect=make_side_effect("auto_approve")),
    ):
        result = runner.invoke(cli, ["run", "--config", str(minimal_toml), "all"])

    assert result.exit_code != 0
    assert call_order == [
        "review_prs",
        "focused_review",
        "self_review",
        "review_requested",
        "address_comments",
        "auto_approve",
    ]


def test_state_reset_yes_deletes_state(minimal_toml, tmp_xdg):
    from harness import state

    state.record_reviewed_sha("acme-frontend", 5, "sha5", 1000.0)
    runner = CliRunner()
    _ = runner.invoke(cli, ["state", "reset", "review-prs", "--config", str(minimal_toml), "--yes"])
    assert state.read_vibe_heal_state("acme-frontend")["reviewed_shas"] == {}


def _enable_auto_approve(minimal_toml, enabled=True):
    minimal_toml.write_text(minimal_toml.read_text() + f"\n[auto_approve]\nenabled = {str(enabled).lower()}\n")


def test_auto_approve_registered_under_run():
    result = CliRunner().invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "auto-approve" in result.output


def test_run_auto_approve_forwards_dry_run(minimal_toml, tmp_xdg):
    _enable_auto_approve(minimal_toml)
    with patch("harness.runners.auto_approve.run") as mock_run:
        result = CliRunner().invoke(cli, ["run", "--config", str(minimal_toml), "auto-approve", "--dry-run"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs == {"dry_run": True}


def test_run_auto_approve_default_not_dry_run(minimal_toml, tmp_xdg):
    _enable_auto_approve(minimal_toml)
    with patch("harness.runners.auto_approve.run") as mock_run:
        result = CliRunner().invoke(cli, ["run", "--config", str(minimal_toml), "auto-approve"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs == {"dry_run": False}


def test_run_auto_approve_runner_error_exits_nonzero(minimal_toml, tmp_xdg):
    _enable_auto_approve(minimal_toml)
    with patch("harness.runners.auto_approve.run", side_effect=RuntimeError("boom")):
        result = CliRunner().invoke(cli, ["run", "--config", str(minimal_toml), "auto-approve"])
    assert result.exit_code != 0


def test_run_auto_approve_disabled_is_noop(minimal_toml, tmp_xdg):
    with (
        patch("harness.runners.auto_approve.acquire_lock"),
        patch("harness.runners.auto_approve.get_gh_token") as mock_token,
    ):
        result = CliRunner().invoke(cli, ["run", "--config", str(minimal_toml), "auto-approve"])
    assert result.exit_code == 0, result.output
    mock_token.assert_not_called()


def test_auto_approve_run_takes_lock(minimal_toml, tmp_xdg):
    from harness.config import load_config
    from harness.runners import auto_approve

    with patch("harness.runners.auto_approve.acquire_lock") as mock_lock:
        auto_approve.run(load_config(minimal_toml))
    mock_lock.assert_called_once()


def test_init_template_has_commented_auto_approve_block():
    assert "# [auto_approve]" in TEMPLATE
    block = TEMPLATE.split("# [auto_approve]")[1].split("# [[repo.subdir]]")[0]
    assert "# enabled = false" in block
    assert "# viewed_max_lines = 75" in block
    assert "# approve_max_lines = 500" in block
    assert "auto_approve" not in tomllib.loads(TEMPLATE)
