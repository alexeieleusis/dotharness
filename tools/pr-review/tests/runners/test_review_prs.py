import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from harness import state
from harness.config import PreCommand, SubDir
from harness.runners import review_prs


def _make_config(tmp_path, authors="*", subdirs=None):
    from harness.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    return HarnessConfig(
        harness=HarnessSection(
            backend="opencode",
            gh_token_cmd="echo tok",  # noqa: S106
            knowledge_dir=tmp_path / "knowledge",
            path_prepend=[],
            env={},
            backend_timeout_seconds=900,
        ),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path, subdirs=subdirs or []),
        vibe_heal=VibehealConfig(
            enabled=True,
            python="/venv/bin/python3",
            authors=authors,
            vibe_heal_timeout=10,
            vibe_heal_post_timeout=5,
        ),
    )


def test_skips_prs_at_or_below_last_pr(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 10)
    cfg = _make_config(tmp_path)
    prs = [
        {"number": 8, "headRefName": "b", "author": {"login": "alice"}, "isDraft": False},
        {"number": 10, "headRefName": "b", "author": {"login": "alice"}, "isDraft": False},
    ]
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.list_open_prs_matching_authors", return_value=prs),
        patch("harness.runners.review_prs.run_cmd") as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore"),
        patch("harness.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        review_prs._run_locked(cfg)
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert vibe_calls == []


def test_processes_new_prs(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 0)
    from harness.config import SubDir

    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    prs = [{"number": 5, "headRefName": "feat", "author": {"login": "alice"}, "isDraft": False}]
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.get_current_user", return_value="alice"),
        patch("harness.runners.review_prs.get_requested_reviewers", return_value=[]),
        patch("harness.runners.review_prs.list_open_prs_matching_authors", return_value=prs),
        patch("harness.runners.review_prs.run_cmd") as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore"),
        patch("harness.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        review_prs._run_locked(cfg)
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert len(vibe_calls) >= 1


def test_updates_state_after_success(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 0)
    from harness.config import SubDir

    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    prs = [{"number": 7, "headRefName": "feat", "author": {"login": "alice"}, "isDraft": False}]
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.get_current_user", return_value="alice"),
        patch("harness.runners.review_prs.get_requested_reviewers", return_value=[]),
        patch("harness.runners.review_prs.list_open_prs_matching_authors", return_value=prs),
        patch("harness.runners.review_prs.run_cmd") as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore"),
        patch("harness.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        review_prs._run_locked(cfg)
    assert state.read_vibe_heal_state("acme-frontend")["last_pr"] == 7


def test_coverage_flag_passed_when_true(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 0)
    from harness.config import SubDir

    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], coverage=True, timeout=30)])
    prs = [{"number": 3, "headRefName": "feat", "author": {"login": "alice"}, "isDraft": False}]
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.get_current_user", return_value="alice"),
        patch("harness.runners.review_prs.get_requested_reviewers", return_value=[]),
        patch("harness.runners.review_prs.list_open_prs_matching_authors", return_value=prs),
        patch("harness.runners.review_prs.run_cmd") as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore"),
        patch("harness.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        review_prs._run_locked(cfg)
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c) and "--coverage" in str(c)]
    assert len(vibe_calls) >= 1


def test_pr_url_graceful_on_pr_from_url_returns_empty(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 10)
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.pr_from_url", return_value={}),
        patch("harness.runners.review_prs._run_base_analysis", return_value=True),
        patch("harness.runners.review_prs.run_cmd") as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore"),
        patch("harness.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        review_prs._run_locked(cfg, pr_url="https://github.com/acme/frontend/pull/3")
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert vibe_calls == []


def test_pr_url_graceful_on_pr_from_url_raises(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 10)
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.pr_from_url", side_effect=RuntimeError("network error")),
        patch("harness.runners.review_prs._run_base_analysis", return_value=True),
        patch("harness.runners.review_prs.run_cmd") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        with pytest.raises(RuntimeError):
            review_prs._run_locked(cfg, pr_url="https://github.com/acme/frontend/pull/3")
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert vibe_calls == []


def test_pr_url_bypasses_last_pr_filtering(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 10)
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    pr = {"number": 3, "headRefName": "feat", "baseRefName": "main"}
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.get_current_user", return_value="alice"),
        patch("harness.runners.review_prs.get_requested_reviewers", return_value=[]),
        patch("harness.runners.review_prs.pr_from_url", return_value=pr),
        patch("harness.runners.review_prs.run_cmd") as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore"),
        patch("harness.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        review_prs._run_locked(cfg, pr_url="https://github.com/acme/frontend/pull/3")
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert len(vibe_calls) >= 1


def test_pr_url_does_not_update_state(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 10)
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    pr = {"number": 3, "headRefName": "feat", "baseRefName": "main"}
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.get_current_user", return_value="alice"),
        patch("harness.runners.review_prs.get_requested_reviewers", return_value=[]),
        patch("harness.runners.review_prs.pr_from_url", return_value=pr),
        patch("harness.runners.review_prs.run_cmd") as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore"),
        patch("harness.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        review_prs._run_locked(cfg, pr_url="https://github.com/acme/frontend/pull/3")
    assert state.read_vibe_heal_state("acme-frontend")["last_pr"] == 10


def test_run_pre_commands_continues_after_non_critical_failure(tmp_path):
    from harness.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(),
    )
    subdir = SubDir(path=".", pre_commands=[PreCommand(cmd="pnpm ci"), PreCommand(cmd="echo hi")])
    with patch("harness.runners.review_prs.run_cmd") as mock_run:
        mock_run.side_effect = [subprocess.CalledProcessError(1, "pnpm ci"), MagicMock(returncode=0)]
        result = review_prs._run_pre_commands(subdir, cfg, {})
    assert result is True
    assert mock_run.call_count == 2


def test_run_pre_commands_aborts_after_critical_failure(tmp_path):
    from harness.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(),
    )
    subdir = SubDir(
        path=".",
        pre_commands=[PreCommand(cmd="poetry install", critical=True), PreCommand(cmd="echo hi")],
    )
    with patch("harness.runners.review_prs.run_cmd") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "poetry install")
        result = review_prs._run_pre_commands(subdir, cfg, {})
    assert result is False
    assert mock_run.call_count == 1


def test_run_pre_commands_continues_after_non_critical_timeout(tmp_path):
    from harness.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(),
    )
    subdir = SubDir(path=".", pre_commands=[PreCommand(cmd="pnpm ci"), PreCommand(cmd="echo hi")])
    with patch("harness.runners.review_prs.run_cmd") as mock_run:
        mock_run.side_effect = [subprocess.TimeoutExpired("pnpm ci", 30), MagicMock(returncode=0)]
        result = review_prs._run_pre_commands(subdir, cfg, {})
    assert result is True
    assert mock_run.call_count == 2


def test_run_pre_commands_aborts_after_critical_timeout(tmp_path):
    from harness.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(),
    )
    subdir = SubDir(
        path=".",
        pre_commands=[PreCommand(cmd="poetry install", critical=True), PreCommand(cmd="echo hi")],
    )
    with patch("harness.runners.review_prs.run_cmd") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired("poetry install", 30)
        result = review_prs._run_pre_commands(subdir, cfg, {})
    assert result is False
    assert mock_run.call_count == 1


def test_run_locked_re_requests_review_once_per_pr_across_subdirs(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 0)

    cfg = _make_config(
        tmp_path,
        subdirs=[SubDir(path="a", pre_commands=[], coverage=False, timeout=30), SubDir(path="b")],
    )
    prs = [{"number": 9, "headRefName": "feat", "author": {"login": "alice"}, "isDraft": False, "baseRefName": "main"}]
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.get_current_user", return_value="alice"),
        patch("harness.runners.review_prs.get_requested_reviewers", return_value=["alice"]) as mock_get_requested,
        patch("harness.runners.review_prs.add_reviewer") as mock_add_reviewer,
        patch("harness.runners.review_prs.list_open_prs_matching_authors", return_value=prs),
        patch("harness.runners.review_prs.get_changed_files", return_value=["a/foo.py", "b/bar.py"]),
        patch("harness.runners.review_prs.run_cmd") as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore"),
        patch("harness.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        review_prs._run_locked(cfg)
    assert mock_get_requested.call_count == 1
    assert mock_get_requested.call_args.args[:2] == (9, "acme/frontend")
    assert mock_add_reviewer.call_count == 1
    assert mock_add_reviewer.call_args.args[:3] == (9, "acme/frontend", "alice")


def test_run_locked_does_not_re_request_review_if_not_previously_requested(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 0)

    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    prs = [{"number": 9, "headRefName": "feat", "author": {"login": "alice"}, "isDraft": False}]
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.get_current_user", return_value="alice"),
        patch("harness.runners.review_prs.get_requested_reviewers", return_value=[]),
        patch("harness.runners.review_prs.add_reviewer") as mock_add_reviewer,
        patch("harness.runners.review_prs.list_open_prs_matching_authors", return_value=prs),
        patch("harness.runners.review_prs.run_cmd") as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore"),
        patch("harness.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        review_prs._run_locked(cfg)
    mock_add_reviewer.assert_not_called()


def test_process_subdir_skips_vibe_heal_after_critical_pre_command_failure(tmp_path):
    from harness.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(enabled=True, python="/venv/bin/python3"),
    )
    subdir = SubDir(path=".", pre_commands=[PreCommand(cmd="poetry install", critical=True)])
    with patch("harness.runners.review_prs.run_cmd") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "poetry install")
        result = review_prs._process_subdir(1, subdir, cfg, {})
    assert result is False
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert vibe_calls == []


def test_subdir_has_changes_root_always_matches():
    assert review_prs._subdir_has_changes(".", []) is True
    assert review_prs._subdir_has_changes(".", ["anything.py"]) is True


def test_subdir_has_changes_bare_name_is_not_a_match():
    assert review_prs._subdir_has_changes("frontend", ["frontend"]) is False


def test_subdir_has_changes_prefix_match():
    assert review_prs._subdir_has_changes("frontend", ["frontend/src/app.tsx"]) is True


def test_subdir_has_changes_no_match():
    assert review_prs._subdir_has_changes("backend", ["frontend/src/app.tsx"]) is False


def test_subdir_has_changes_does_not_false_positive_on_sibling_name():
    assert review_prs._subdir_has_changes("frontend", ["frontend-legacy/src/app.tsx"]) is False


def test_subdir_has_changes_handles_trailing_slash_in_config():
    assert review_prs._subdir_has_changes("frontend/", ["frontend/src/app.tsx"]) is True


def test_run_locked_skips_subdir_with_no_changes(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 0)

    cfg = _make_config(
        tmp_path,
        subdirs=[SubDir(path="a", pre_commands=[], coverage=False, timeout=30), SubDir(path="b")],
    )
    prs = [{"number": 11, "headRefName": "feat", "author": {"login": "alice"}, "isDraft": False, "baseRefName": "main"}]
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.get_current_user", return_value="alice"),
        patch("harness.runners.review_prs.get_requested_reviewers", return_value=[]),
        patch("harness.runners.review_prs.list_open_prs_matching_authors", return_value=prs),
        patch("harness.runners.review_prs.get_changed_files", return_value=["a/src/foo.py"]),
        patch("harness.runners.review_prs._run_base_analysis", return_value=True),
        patch("harness.runners.review_prs.run_cmd") as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore"),
        patch("harness.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        review_prs._run_locked(cfg)
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert len(vibe_calls) >= 1
    subdir_b_path = str(tmp_path / "b")
    assert all(c.kwargs.get("cwd") != subdir_b_path for c in vibe_calls)


def test_run_base_analysis_skips_when_sha_unchanged(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="deadbeef")
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    with patch("harness.runners.review_prs.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"deadbeef\n", stderr=b"")
        result = review_prs._run_base_analysis(cfg, {})
    assert result is True
    # Only fetch + rev-parse should run; no checkout, since the SHA already matches.
    assert not any("checkout" in str(c) for c in mock_run.call_args_list)


def test_run_base_analysis_runs_subdirs_and_persists_sha_on_success(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="old-sha")
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    with (
        patch("harness.runners.review_prs.run_cmd") as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="original-sha"),
        patch("harness.runners.review_prs.git_restore") as mock_restore,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")
        result = review_prs._run_base_analysis(cfg, {})
    assert result is True
    assert state.read_vibe_heal_state("acme-frontend")["last_main_sha"] == "new-sha"
    vibe_calls = [c for c in mock_run.call_args_list if "--baseline" in str(c)]
    assert len(vibe_calls) == 1
    mock_restore.assert_called_once_with("original-sha", "", str(tmp_path), {})


def test_run_base_analysis_does_not_persist_sha_when_subdir_fails(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="old-sha")
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])

    def fake_run_cmd(cmd, **kwargs):
        if "--baseline" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")

    with (
        patch("harness.runners.review_prs.run_cmd", side_effect=fake_run_cmd),
        patch("harness.runners.review_prs.git_detach_and_record", return_value="original-sha"),
        patch("harness.runners.review_prs.git_restore") as mock_restore,
    ):
        result = review_prs._run_base_analysis(cfg, {})
    assert result is False
    assert state.read_vibe_heal_state("acme-frontend")["last_main_sha"] == "old-sha"
    mock_restore.assert_called_once_with("original-sha", "", str(tmp_path), {})


def test_run_base_analysis_returns_false_on_git_fetch_failure(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    with patch.object(type(cfg), "repo_slug", "acme/frontend"):
        state.write_vibe_heal_state("acme/frontend", last_main_sha="old-sha")

    def fake_run_cmd(cmd, **kwargs):
        if "git" in cmd and "fetch" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"fetch error")
        return MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")

    with (
        patch.object(type(cfg), "repo_slug", "acme/frontend"),
        patch("harness.runners.review_prs.run_cmd", side_effect=fake_run_cmd) as mock_run,
    ):
        result = review_prs._run_base_analysis(cfg, {})
    assert result is False
    assert mock_run.call_count == 1
    assert "fetch" in str(mock_run.call_args_list[0])


def test_run_base_analysis_returns_false_on_rev_parse_failure(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    with patch.object(type(cfg), "repo_slug", "acme/frontend"):
        state.write_vibe_heal_state("acme/frontend", last_main_sha="old-sha")

    def fake_run_cmd(cmd, **kwargs):
        if "git" in cmd and "fetch" in cmd:
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if "rev-parse" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"rev-parse error")
        return MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")

    with (
        patch.object(type(cfg), "repo_slug", "acme/frontend"),
        patch("harness.runners.review_prs.run_cmd", side_effect=fake_run_cmd) as mock_run,
    ):
        result = review_prs._run_base_analysis(cfg, {})
    assert result is False
    assert mock_run.call_count == 2


def test_run_base_analysis_returns_false_on_checkout_failure(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    with patch.object(type(cfg), "repo_slug", "acme/frontend"):
        state.write_vibe_heal_state("acme/frontend", last_main_sha="old-sha")

    def fake_run_cmd(cmd, **kwargs):
        if "git" in cmd and "fetch" in cmd:
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if "rev-parse" in cmd:
            return MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")
        if "checkout" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"checkout error")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with (
        patch.object(type(cfg), "repo_slug", "acme/frontend"),
        patch("harness.runners.review_prs.run_cmd", side_effect=fake_run_cmd),
        patch("harness.runners.review_prs.git_detach_and_record", return_value="original-sha"),
        patch("harness.runners.review_prs.git_restore") as mock_restore,
    ):
        result = review_prs._run_base_analysis(cfg, {})
    assert result is False
    mock_restore.assert_called_once_with("original-sha", "", str(tmp_path), {})


def test_run_locked_skips_when_subdirs_empty(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path)
    with (
        patch("harness.runners.review_prs.get_gh_token") as mock_token,
        patch("harness.runners.review_prs.list_open_prs_matching_authors") as mock_list,
    ):
        review_prs._run_locked(cfg)
    mock_token.assert_not_called()
    mock_list.assert_not_called()


def test_run_locked_skips_pr_processing_when_base_analysis_fails(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    with (
        patch("harness.runners.review_prs._run_base_analysis", return_value=False),
        patch("harness.runners.review_prs.list_open_prs_matching_authors") as mock_list,
    ):
        review_prs._run_locked(cfg)
    mock_list.assert_not_called()


def test_run_locked_does_not_advance_state_and_posts_fail_marker_on_subdir_failure(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 0)
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    prs = [{"number": 7, "headRefName": "feat", "author": {"login": "alice"}, "isDraft": False}]

    def fake_run_cmd(cmd, **kwargs):
        if "vibe_heal" in str(cmd) and "--pr" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return MagicMock(returncode=0, stdout=b"[]", stderr=b"")

    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.get_current_user", return_value="alice"),
        patch("harness.runners.review_prs.get_requested_reviewers", return_value=[]),
        patch("harness.runners.review_prs.list_open_prs_matching_authors", return_value=prs),
        patch("harness.runners.review_prs._run_base_analysis", return_value=True),
        patch("harness.runners.review_prs.run_cmd", side_effect=fake_run_cmd) as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore"),
        patch("harness.runners.review_prs.git_fetch_and_checkout"),
    ):
        review_prs._run_locked(cfg)
    assert state.read_vibe_heal_state("acme-frontend")["last_pr"] == 0
    fail_comment_calls = [c for c in mock_run.call_args_list if "vibe-heal-bot-fail" in str(c)]
    assert len(fail_comment_calls) >= 1


def test_run_locked_does_not_advance_state_and_posts_fail_marker_on_generic_exception(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 0)
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    prs = [{"number": 7, "headRefName": "feat", "author": {"login": "alice"}, "isDraft": False}]

    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.get_current_user", return_value="alice"),
        patch("harness.runners.review_prs.get_requested_reviewers", return_value=[]),
        patch("harness.runners.review_prs.list_open_prs_matching_authors", return_value=prs),
        patch("harness.runners.review_prs._run_base_analysis", return_value=True),
        patch("harness.runners.review_prs._process_pr", side_effect=ValueError("boom")),
        patch(
            "harness.runners.review_prs.run_cmd", return_value=MagicMock(returncode=0, stdout=b"[]", stderr=b"")
        ) as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore"),
        patch("harness.runners.review_prs.git_fetch_and_checkout"),
    ):
        review_prs._run_locked(cfg)
    assert state.read_vibe_heal_state("acme-frontend")["last_pr"] == 0
    fail_comment_calls = [c for c in mock_run.call_args_list if "vibe-heal-bot-fail" in str(c)]
    assert len(fail_comment_calls) >= 1


def test_run_locked_returns_early_when_vibe_heal_disabled(tmp_path):
    from harness.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(enabled=False),
    )
    with patch("harness.runners.review_prs.run_cmd") as mock_run:
        review_prs._run_locked(cfg)
    mock_run.assert_not_called()


def test_fatal_git_error_breaks_pr_processing_loop(tmp_xdg, tmp_path):
    from harness.runners.common import FatalGitError

    state.write_vibe_heal_state("acme-frontend", 0)
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    prs = [
        {"number": 3, "headRefName": "pr3", "author": {"login": "alice"}, "isDraft": False},
        {"number": 4, "headRefName": "pr4", "author": {"login": "alice"}, "isDraft": False},
    ]
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.get_current_user", return_value="alice"),
        patch("harness.runners.review_prs.get_requested_reviewers", return_value=[]),
        patch("harness.runners.review_prs.list_open_prs_matching_authors", return_value=prs),
        patch("harness.runners.review_prs._run_base_analysis", return_value=True),
        patch("harness.runners.review_prs.run_cmd", return_value=MagicMock(returncode=0, stdout=b"[]")),
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore") as mock_restore,
        patch("harness.runners.review_prs.git_fetch_and_checkout") as mock_fetch,
    ):
        mock_fetch.side_effect = [None, FatalGitError("corrupted ref")]
        review_prs._run_locked(cfg)
    # Only PR 3 was processed; FatalGitError on PR 4 broke the loop.
    assert mock_fetch.call_count == 1
    assert state.read_vibe_heal_state("acme-frontend")["last_pr"] == 3
    assert mock_restore.call_count == 2


def test_generic_exception_continues_pr_processing_loop(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", 0)
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    prs = [
        {"number": 2, "headRefName": "pr2", "author": {"login": "alice"}, "isDraft": False},
        {"number": 4, "headRefName": "pr4", "author": {"login": "alice"}, "isDraft": False},
    ]
    with (
        patch("harness.runners.review_prs.get_gh_token", return_value="tok"),
        patch("harness.runners.review_prs.get_current_user", return_value="alice"),
        patch("harness.runners.review_prs.get_requested_reviewers", return_value=[]),
        patch("harness.runners.review_prs.list_open_prs_matching_authors", return_value=prs),
        patch("harness.runners.review_prs._run_base_analysis", return_value=True),
        patch("harness.runners.review_prs.run_cmd", return_value=MagicMock(returncode=0, stdout=b"[]")) as mock_run,
        patch("harness.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_prs.git_restore") as mock_restore,
        patch("harness.runners.review_prs.git_fetch_and_checkout") as mock_fetch,
    ):
        mock_fetch.side_effect = [None, RuntimeError("unexpected failure")]
        review_prs._run_locked(cfg)
    # Both PRs were processed; generic Exception on PR 2 did not break the loop.
    assert mock_fetch.call_count == 2
    assert mock_restore.call_count == 2
    # batch_failed=True from PR 2 prevents state advancement for remaining PRs.
    assert state.read_vibe_heal_state("acme-frontend")["last_pr"] == 0
    fail_comment_calls = [c for c in mock_run.call_args_list if "vibe-heal-bot-fail" in str(c)]
    assert len(fail_comment_calls) >= 1


def test_post_comment_if_needed_skips_when_marker_exists():
    comments = [{"body": "Some comment", "id": 1}, {"body": "[vibe-heal-bot] already posted", "id": 2}]
    stdout = json.dumps(comments).encode()

    call_count = 0

    def fake_run_cmd(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        return MagicMock(returncode=0, stdout=stdout)

    with patch("harness.runners.review_prs.run_cmd", side_effect=fake_run_cmd):
        review_prs._post_comment_if_needed(
            1, "acme/frontend", {"GH_TOKEN": "tok"}, marker="[vibe-heal-bot]", body="new comment"
        )
    assert call_count == 1
