import subprocess
from unittest.mock import MagicMock, patch

from harness import state
from harness.runners import self_review


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


def test_skips_already_reviewed_pr(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [5])
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 5, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()


def test_backup_check_marks_reviewed_without_running(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [])
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 3, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.check_review_summary_comment_status", return_value=True),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    assert 3 in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_updates_state_on_success(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 7, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.check_review_summary_comment_status", side_effect=[False, True]),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        self_review._run_locked(cfg)
    assert 7 in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_does_not_update_state_when_summary_comment_missing(tmp_xdg, tmp_path):
    """Backend exits 0 for every call, but no summary comment is found on GitHub afterward —
    the PR must not be marked reviewed so the summary gets retried on the next run."""
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 15, "url": "u", "headRefName": "b"}]
        ),
        # first call: _should_skip_pr's up-front check (not already reviewed);
        # second call: _run_summary's post-backend verification (comment missing)
        patch("harness.runners.self_review.check_review_summary_comment_status", side_effect=[False, False]),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        self_review._run_locked(cfg)
    assert 15 not in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_does_not_update_state_on_failure(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 9, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.check_review_summary_comment_status", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=1)
        self_review._run_locked(cfg)
    assert 9 not in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_backend_called_once_per_file_plus_summary(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "a.py").write_text("a = 1\n")
    (tmp_path / "src" / "b.py").write_text("b = 2\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 11, "url": "u", "headRefName": "feat"}]
        ),
        patch("harness.runners.self_review.check_review_summary_comment_status", side_effect=[False, True]),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/a.py", "src/b.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        self_review.run(cfg)
    # 2 files + 1 summary = 3 backend calls
    assert mock_be.return_value.run.call_count == 3
    assert 11 in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_does_not_update_state_when_file_call_fails(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "a.py").write_text("a = 1\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 13, "url": "u", "headRefName": "feat"}]
        ),
        patch("harness.runners.self_review.check_review_summary_comment_status", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/a.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=1)
        self_review.run(cfg)
    assert 13 not in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_vibe_heal_context_included_in_prompts(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 7, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.check_review_summary_comment_status", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.get_vibe_heal_context", return_value="sonar findings"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        self_review._run_locked(cfg)
    # backend.run(prompt, cwd=wdir) — prompt is first positional arg
    prompts = [c.args[0] for c in mock_be.return_value.run.call_args_list]
    assert all("## Static Analysis" in p for p in prompts)
    assert all("sonar findings" in p for p in prompts)


def test_vibe_heal_context_absent_when_empty(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 7, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.check_review_summary_comment_status", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.get_vibe_heal_context", return_value=""),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        self_review._run_locked(cfg)
    prompts = [c.args[0] for c in mock_be.return_value.run.call_args_list]
    assert all("## Static Analysis" not in p for p in prompts)


def test_git_restore_called_on_context_gathering_exception(tmp_xdg, tmp_path):
    """When a context-gathering call raises, git_restore still runs in finally."""
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 20, "url": "u", "headRefName": "b"}]
        ),
        patch("harness.runners.self_review.check_review_summary_comment_status", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore") as mock_restore,
        patch("harness.runners.self_review.get_pr_base_branch", side_effect=RuntimeError("network failure")),
        patch("harness.runners.self_review.Backend"),
    ):
        self_review._run_locked(cfg)
    mock_restore.assert_called_once_with("sha", "b", str(tmp_path), mock_restore.call_args[0][3])
    assert 20 not in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_timeout_expired_does_not_mark_reviewed(tmp_xdg, tmp_path):
    """When backend.run raises TimeoutExpired, the PR is not marked reviewed and no crash propagates."""
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 21, "url": "u", "headRefName": "b"}]
        ),
        patch("harness.runners.self_review.check_review_summary_comment_status", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.side_effect = subprocess.TimeoutExpired("cmd", 10)
        self_review._run_locked(cfg)
    assert 21 not in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_inconclusive_comment_check_skips_cycle_without_marking_reviewed(tmp_xdg, tmp_path):
    """When the GitHub API check itself fails (rate limit, transient 5xx), the up-front
    check can't confirm anything either way — the PR must be left alone this cycle rather
    than processed (risking a duplicate post) or marked reviewed (risking a false positive)."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [])
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 30, "url": "u", "headRefName": "b"}]
        ),
        patch("harness.runners.self_review.check_review_summary_comment_status", return_value=None),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    assert 30 not in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_does_not_update_state_when_summary_check_inconclusive(tmp_xdg, tmp_path):
    """If the post-backend verification GET fails transiently, that's inconclusive, not a
    confirmed missing comment — the PR must still not be marked reviewed on this run."""
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 31, "url": "u", "headRefName": "b"}]
        ),
        # first call: up-front check (not already reviewed); second call: post-backend
        # verification, which is inconclusive rather than a confirmed absence
        patch("harness.runners.self_review.check_review_summary_comment_status", side_effect=[False, None]),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        self_review._run_locked(cfg)
    assert 31 not in state.read_self_review_state("acme-frontend")["reviewed_prs"]
