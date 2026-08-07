import json
from unittest.mock import MagicMock, patch

from harness.runners import review_requested


def _cfg(tmp_path):
    from harness.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    return HarnessConfig(
        harness=HarnessSection(
            "opencode", "echo tok", knowledge_dir=tmp_path / "k", path_prepend=[], env={}, backend_timeout_seconds=10
        ),
        repo=RepoConfig("acme/frontend", tmp_path),
        vibe_heal=VibehealConfig(),
    )


def _setup_knowledge(tmp_path, content="instructions"):
    kdir = tmp_path / "k" / "pr-review"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "review-file.md").write_text(content)
    (kdir / "review-summary.md").write_text(content)


def test_skips_already_approved_pr(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.review_requested.get_gh_token", return_value="tok"),
        patch("harness.runners.review_requested.get_current_user", return_value="me"),
        patch(
            "harness.runners.review_requested._get_prs", return_value=[{"number": 1, "url": "u", "headRefName": "b"}]
        ),
        patch("harness.runners.review_requested._has_user_approved", return_value=True),
        patch("harness.runners.review_requested.has_review_summary_comment", return_value=False),
        patch("harness.runners.review_requested.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_requested.Backend") as mock_be,
    ):
        review_requested._run_locked(cfg, pr_url=None)
    mock_be.return_value.run.assert_not_called()


def test_skips_pr_with_existing_osc_review(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.review_requested.get_gh_token", return_value="tok"),
        patch("harness.runners.review_requested.get_current_user", return_value="me"),
        patch(
            "harness.runners.review_requested._get_prs", return_value=[{"number": 1, "url": "u", "headRefName": "b"}]
        ),
        patch("harness.runners.review_requested._has_user_approved", return_value=False),
        patch("harness.runners.review_requested.has_review_summary_comment", return_value=True),
        patch("harness.runners.review_requested.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_requested.Backend") as mock_be,
    ):
        review_requested._run_locked(cfg, pr_url=None)
    mock_be.return_value.run.assert_not_called()


def test_backend_called_once_per_file_plus_summary(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.review_requested.get_gh_token", return_value="tok"),
        patch("harness.runners.review_requested.get_current_user", return_value="bot"),
        patch(
            "harness.runners.review_requested._get_prs", return_value=[{"number": 1, "url": "u", "headRefName": "feat"}]
        ),
        patch("harness.runners.review_requested._has_user_approved", return_value=False),
        patch("harness.runners.review_requested.has_review_summary_comment", return_value=False),
        patch("harness.runners.review_requested.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_requested.git_fetch_and_checkout"),
        patch("harness.runners.review_requested.git_restore"),
        patch("harness.runners.review_requested.run_cmd") as mock_run,
        patch("harness.runners.review_requested.get_pr_base_branch", return_value="main"),
        patch("harness.runners.review_requested.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.review_requested.get_changed_files", return_value=["src/a.py", "src/b.py"]),
        patch("harness.runners.review_requested.get_file_diff", return_value="@@diff"),
        patch("harness.runners.review_requested.os") as mock_os,
        patch("harness.runners.review_requested.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        mock_os.path.exists.return_value = True
        mock_os.path.join.side_effect = lambda *parts: "/".join(parts)
        review_requested.run(cfg)
    # 2 files + 1 summary = 3 backend calls
    assert mock_be.return_value.run.call_count == 3


def test_vibe_heal_context_included_in_prompts(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.review_requested.get_gh_token", return_value="tok"),
        patch("harness.runners.review_requested.get_current_user", return_value="bot"),
        patch(
            "harness.runners.review_requested._get_prs", return_value=[{"number": 1, "url": "u", "headRefName": "feat"}]
        ),
        patch("harness.runners.review_requested._has_user_approved", return_value=False),
        patch("harness.runners.review_requested.has_review_summary_comment", return_value=False),
        patch("harness.runners.review_requested.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_requested.git_fetch_and_checkout"),
        patch("harness.runners.review_requested.git_restore"),
        patch("harness.runners.review_requested.run_cmd") as mock_run,
        patch("harness.runners.review_requested.get_pr_base_branch", return_value="main"),
        patch("harness.runners.review_requested.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.review_requested.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.review_requested.get_file_diff", return_value="@@diff"),
        patch("harness.runners.review_requested.os") as mock_os,
        patch("harness.runners.review_requested.get_vibe_heal_context", return_value="sonar findings"),
        patch("harness.runners.review_requested.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        mock_os.path.exists.return_value = True
        mock_os.path.join.side_effect = lambda *parts: "/".join(parts)
        review_requested._run_locked(cfg, pr_url=None)
    # backend.run(prompt, cwd=wdir) — prompt is first positional arg
    prompts = [c.args[0] for c in mock_be.return_value.run.call_args_list]
    assert all("## Static Analysis" in p for p in prompts)
    assert all("sonar findings" in p for p in prompts)


def test_get_prs_returns_head_ref_name_from_single_call(tmp_xdg, tmp_path):
    # gh pr list --json can return headRefName directly, avoiding the N+1 pattern
    # of gh search prs + individual gh pr view calls.
    list_result = [{"number": 42, "url": "https://github.com/acme/frontend/pull/42", "headRefName": "feat/my-branch"}]

    with patch("harness.runners.review_requested.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(list_result).encode())
        prs = review_requested._get_prs("acme/frontend", {})

    assert prs == list_result
    mock_run.assert_called_once()


def test_get_prs_returns_empty_on_search_failure(tmp_xdg, tmp_path):
    with patch("harness.runners.review_requested.run_cmd") as mock_run:
        m = MagicMock()
        m.returncode = 1
        m.stdout = b""
        mock_run.return_value = m
        prs = review_requested._get_prs("acme/frontend", {})
    assert prs == []


def test_vibe_heal_context_absent_when_empty(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.review_requested.get_gh_token", return_value="tok"),
        patch("harness.runners.review_requested.get_current_user", return_value="bot"),
        patch(
            "harness.runners.review_requested._get_prs", return_value=[{"number": 1, "url": "u", "headRefName": "feat"}]
        ),
        patch("harness.runners.review_requested._has_user_approved", return_value=False),
        patch("harness.runners.review_requested.has_review_summary_comment", return_value=False),
        patch("harness.runners.review_requested.git_detach_and_record", return_value="sha"),
        patch("harness.runners.review_requested.git_fetch_and_checkout"),
        patch("harness.runners.review_requested.git_restore"),
        patch("harness.runners.review_requested.run_cmd") as mock_run,
        patch("harness.runners.review_requested.get_pr_base_branch", return_value="main"),
        patch("harness.runners.review_requested.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.review_requested.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.review_requested.get_file_diff", return_value="@@diff"),
        patch("harness.runners.review_requested.os") as mock_os,
        patch("harness.runners.review_requested.get_vibe_heal_context", return_value=""),
        patch("harness.runners.review_requested.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        mock_os.path.exists.return_value = True
        mock_os.path.join.side_effect = lambda *parts: "/".join(parts)
        review_requested._run_locked(cfg, pr_url=None)
    prompts = [c.args[0] for c in mock_be.return_value.run.call_args_list]
    assert all("## Static Analysis" not in p for p in prompts)
