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
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 3, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review._has_osc_review_comment", return_value=True),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    assert 3 in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_updates_state_on_success(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 7, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review._has_osc_review_comment", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.os") as mock_os,
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        mock_os.path.exists.return_value = True
        mock_os.path.join.side_effect = lambda *parts: "/".join(parts)
        self_review._run_locked(cfg)
    assert 7 in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_does_not_update_state_on_failure(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 9, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review._has_osc_review_comment", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.os") as mock_os,
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=1)
        mock_os.path.exists.return_value = True
        mock_os.path.join.side_effect = lambda *parts: "/".join(parts)
        self_review._run_locked(cfg)
    assert 9 not in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_backend_called_once_per_file_plus_summary(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 11, "url": "u", "headRefName": "feat"}]
        ),
        patch("harness.runners.self_review._has_osc_review_comment", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/a.py", "src/b.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.os") as mock_os,
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        mock_os.path.exists.return_value = True
        mock_os.path.join.side_effect = lambda *parts: "/".join(parts)
        self_review.run(cfg)
    # 2 files + 1 summary = 3 backend calls
    assert mock_be.return_value.run.call_count == 3
    assert 11 in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_does_not_update_state_when_file_call_fails(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 13, "url": "u", "headRefName": "feat"}]
        ),
        patch("harness.runners.self_review._has_osc_review_comment", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/a.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.os") as mock_os,
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=1)
        mock_os.path.exists.return_value = True
        mock_os.path.join.side_effect = lambda *parts: "/".join(parts)
        self_review.run(cfg)
    assert 13 not in state.read_self_review_state("acme-frontend")["reviewed_prs"]


def test_vibe_heal_context_included_in_prompts(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 7, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review._has_osc_review_comment", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.os") as mock_os,
        patch("harness.runners.self_review.get_vibe_heal_context", return_value="sonar findings"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        mock_os.path.exists.return_value = True
        mock_os.path.join.side_effect = lambda *parts: "/".join(parts)
        self_review._run_locked(cfg)
    # backend.run(prompt, cwd=wdir) — prompt is first positional arg
    prompts = [c.args[0] for c in mock_be.return_value.run.call_args_list]
    assert all("## Static Analysis" in p for p in prompts)
    assert all("sonar findings" in p for p in prompts)


def test_vibe_heal_context_absent_when_empty(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 7, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review._has_osc_review_comment", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.os") as mock_os,
        patch("harness.runners.self_review.get_vibe_heal_context", return_value=""),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        mock_be.return_value.run.return_value = MagicMock(returncode=0)
        mock_os.path.exists.return_value = True
        mock_os.path.join.side_effect = lambda *parts: "/".join(parts)
        self_review._run_locked(cfg)
    prompts = [c.args[0] for c in mock_be.return_value.run.call_args_list]
    assert all("## Static Analysis" not in p for p in prompts)
