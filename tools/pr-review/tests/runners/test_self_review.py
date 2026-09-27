import json
import subprocess
from unittest.mock import MagicMock, patch

from harness import state
from harness.runners import self_review
from harness.runners.common import REGRET_REVIEW_MARKER
from harness.runners.regret_review import IntroducingHunk, RegretCandidate


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
    (kdir / "review-design.md").write_text(content)
    (kdir / "review-traceability.md").write_text(content)
    (kdir / "review-regret.md").write_text(content)


def test_skips_already_reviewed_pr(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [5])
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        # design_reviewed_prs is empty, so the design pass still runs even though
        # files/summary are already reviewed — has_design_review_comment must actually
        # be reached (and the context-gathering mocks below let it get there) for this
        # test to prove the backend is skipped for the right reason.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 5, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    assert 5 in state.get_design_reviewed_prs("acme-frontend")


def test_backup_check_marks_reviewed_without_running(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [])
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        # design_reviewed_prs is empty, so the design pass still runs even though
        # files/summary are already reviewed — has_design_review_comment must actually
        # be reached (and the context-gathering mocks below let it get there) for this
        # test to prove the backend is skipped for the right reason.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 3, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.check_review_summary_comment_status", return_value=True),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    assert 3 in state.read_self_review_state("acme-frontend")["reviewed_prs"]
    assert 3 in state.get_design_reviewed_prs("acme-frontend")


def test_updates_state_on_success(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        # Design pass is decoupled and out of scope for these correctness/state tests —
        # treat it as already-done so it never invokes the backend or hits `gh` for real.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
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
        # Design pass is decoupled and out of scope for these correctness/state tests —
        # treat it as already-done so it never invokes the backend or hits `gh` for real.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
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
    assert state.get_partial_reviewed_files("acme-frontend", 15) == ["src/foo.py"]


def test_does_not_update_state_on_failure(tmp_xdg, tmp_path):
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        # Design pass is decoupled and out of scope for these correctness/state tests —
        # treat it as already-done so it never invokes the backend or hits `gh` for real.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
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
        # Design pass is decoupled and out of scope for these correctness/state tests —
        # treat it as already-done so it never invokes the backend or hits `gh` for real.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
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
        # Design pass is decoupled and out of scope for these correctness/state tests —
        # treat it as already-done so it never invokes the backend or hits `gh` for real.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
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
        # Design pass is decoupled and out of scope for these correctness/state tests —
        # treat it as already-done so it never invokes the backend or hits `gh` for real.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
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
        # Design pass is decoupled and out of scope for these correctness/state tests —
        # treat it as already-done so it never invokes the backend or hits `gh` for real.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
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
        # Design pass is decoupled and out of scope for these correctness/state tests —
        # treat it as already-done so it never invokes the backend or hits `gh` for real.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
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
        # Design pass is decoupled and out of scope for these correctness/state tests —
        # treat it as already-done so it never invokes the backend or hits `gh` for real.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
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
        # design_reviewed_prs is empty, so the design pass still runs even though
        # files/summary status is inconclusive — has_design_review_comment must
        # actually be reached (and the context-gathering mocks below let it get there)
        # for this test to prove the backend is skipped for the right reason.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 30, "url": "u", "headRefName": "b"}]
        ),
        patch("harness.runners.self_review.check_review_summary_comment_status", return_value=None),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    assert 30 not in state.read_self_review_state("acme-frontend")["reviewed_prs"]
    assert 30 in state.get_design_reviewed_prs("acme-frontend")


def test_prunes_stale_entries_for_closed_prs(tmp_xdg, tmp_path):
    """A PR that's no longer in the open-PR list gets its reviewed_prs/partial_reviews
    entries dropped, even though it's not reprocessed this cycle."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [5, 40])
    state.set_partial_reviewed_files("acme-frontend", 40, ["stale.py"])
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        # design_reviewed_prs is empty, so the design pass still runs for the
        # surviving PR even though files/summary are already reviewed —
        # has_design_review_comment must actually be reached (and the
        # context-gathering mocks below let it get there) for this test to prove
        # the backend is skipped for the right reason.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 5, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    result = state.read_self_review_state("acme-frontend")
    assert result["reviewed_prs"] == [5]
    assert result["partial_reviews"] == {}
    assert 5 in state.get_design_reviewed_prs("acme-frontend")


def test_failed_pr_fetch_skips_cycle_without_touching_state(tmp_xdg, tmp_path):
    """When _list_my_prs can't confirm the open-PR set (gh failure or malformed JSON), the
    cycle must skip entirely rather than pruning against a possibly-wrong empty set."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [5])
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        # Design pass is decoupled and out of scope for these correctness/state tests —
        # treat it as already-done so it never invokes the backend or hits `gh` for real.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review._list_my_prs", return_value=None),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    assert state.read_self_review_state("acme-frontend")["reviewed_prs"] == [5]


def test_list_my_prs_returns_none_on_gh_failure():
    with patch("harness.runners.self_review.run_cmd", return_value=MagicMock(returncode=1, stdout=b"")):
        assert self_review._list_my_prs("acme/frontend", {}) is None


def test_list_my_prs_returns_none_on_malformed_json():
    with patch("harness.runners.self_review.run_cmd", return_value=MagicMock(returncode=0, stdout=b"not json")):
        assert self_review._list_my_prs("acme/frontend", {}) is None


def test_list_my_prs_requests_author_and_returns_prs_from_single_call():
    # gh pr list --json can return everything in a single call; author must be among
    # the requested fields, because the regret pass's author gate
    # (regret_review.find_introducing_prs -> _pr_gate_setup) reads
    # pr["author"]["login"] from the PR dict and fails closed on an authorless PR —
    # without the field no PR could ever pass the gate in this runner (the same fix
    # review_requested._get_prs received). The returned PR dicts must carry it through.
    pr = {
        "number": 42,
        "url": "https://github.com/acme/frontend/pull/42",
        "headRefName": "feat/my-branch",
        "author": {"login": "alice"},
    }
    with patch(
        "harness.runners.self_review.run_cmd",
        return_value=MagicMock(returncode=0, stdout=json.dumps([pr]).encode()),
    ) as mock_run:
        prs = self_review._list_my_prs("acme/frontend", {})

    assert prs == [pr]
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    fields = args[args.index("--json") + 1].split(",")
    assert "author" in fields


def test_design_review_runs_independently_of_already_reviewed_files(tmp_xdg, tmp_path):
    """A PR whose files/summary already succeeded (reviewed_prs) still gets a design
    pass if design_reviewed_prs doesn't have it yet — fate is decoupled (dotharness#3)."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [7])
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 7, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.has_design_review_comment", return_value=False),
        patch("harness.runners.self_review.check_design_review_comment_status", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
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
    # Only the design pass runs (files/summary already done) — one backend call.
    assert mock_be.return_value.run.call_count == 1
    assert mock_be.return_value.run.call_args.kwargs["context"] == "PR #7 design review"
    assert 7 in state.get_design_reviewed_prs("acme-frontend")


def test_design_review_failure_does_not_block_files_summary_state(tmp_xdg, tmp_path):
    """A design-pass failure must not prevent reviewed_prs from being updated, and must
    not itself be recorded as done — the two are tracked independently."""
    state.write_self_review_state("acme-frontend", [])
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 8, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.check_review_summary_comment_status", side_effect=[False, True]),
        patch("harness.runners.self_review.has_design_review_comment", return_value=False),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        # File review + summary succeed (returncode 0); the design-review call — the
        # third and last backend invocation — fails.
        mock_be.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=1),
        ]
        self_review._run_locked(cfg)
    assert 8 in state.read_self_review_state("acme-frontend")["reviewed_prs"]
    assert 8 not in state.get_design_reviewed_prs("acme-frontend")


def test_design_review_already_done_is_skipped_without_any_backend_call(tmp_xdg, tmp_path):
    """Once design_reviewed_prs has the PR, and files/summary are also already done,
    nothing runs at all this cycle — not even a checkout."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [9])
    state.add_design_reviewed_pr("acme-frontend", 9)
    state.add_traceability_reviewed_pr("acme-frontend", 9)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 9, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout") as mock_checkout,
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    mock_checkout.assert_not_called()


def test_design_review_marker_found_marks_done_without_invoking_backend(tmp_xdg, tmp_path):
    """Defense-in-depth: if a design-review comment is already on GitHub (e.g. a prior
    state write didn't persist), the pass is recorded as done without re-invoking."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [10])
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 10, "url": "u", "headRefName": "b"}]
        ),
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    assert 10 in state.get_design_reviewed_prs("acme-frontend")


def test_design_review_not_marked_done_when_comment_confirmed_missing(tmp_xdg, tmp_path):
    """The backend exiting 0 is not proof it posted — if the post-run GitHub check
    confirms no design-review comment exists, the pass must not be recorded as done,
    so a future run retries instead of silently skipping it forever."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [11])
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 11, "url": "u", "headRefName": "b"}]
        ),
        patch("harness.runners.self_review.has_design_review_comment", return_value=False),
        patch("harness.runners.self_review.check_design_review_comment_status", return_value=False),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
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
    assert 11 not in state.get_design_reviewed_prs("acme-frontend")


def test_design_review_not_marked_done_when_comment_check_inconclusive(tmp_xdg, tmp_path):
    """If the post-run GitHub check itself fails (e.g. transient API error), that's
    inconclusive, not a confirmed absence — the pass must still not be marked done."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [12])
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 12, "url": "u", "headRefName": "b"}]
        ),
        patch("harness.runners.self_review.has_design_review_comment", return_value=False),
        patch("harness.runners.self_review.check_design_review_comment_status", return_value=None),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
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
    assert 12 not in state.get_design_reviewed_prs("acme-frontend")


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
        # Design pass is decoupled and out of scope for these correctness/state tests —
        # treat it as already-done so it never invokes the backend or hits `gh` for real.
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
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


def test_traceability_review_runs_independently_of_already_reviewed_files(tmp_xdg, tmp_path):
    """A PR whose files/summary/design already succeeded still gets a traceability pass
    if traceability_reviewed_prs doesn't have it yet — fate is decoupled (dotharness#4)."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [7])
    state.add_design_reviewed_pr("acme-frontend", 7)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    ticket = {
        "number": 3,
        "repo": "acme/frontend",
        "title": "Do the thing",
        "body": "body",
        "source": "closing_keyword",
    }
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 7, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=False),
        patch("harness.runners.self_review.resolve_linked_tickets", return_value=[ticket]),
        patch("harness.runners.self_review.check_traceability_review_comment_status", return_value=True),
        patch("harness.runners.self_review.build_early_comment_context", return_value=""),
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
    # Only the traceability pass runs (files/summary/design already done) — one backend call.
    assert mock_be.return_value.run.call_count == 1
    assert mock_be.return_value.run.call_args.kwargs["context"] == "PR #7 traceability review"
    prompt = mock_be.return_value.run.call_args.args[0]
    assert "## Linked Ticket(s)" in prompt
    assert "Do the thing" in prompt
    assert 7 in state.get_traceability_reviewed_prs("acme-frontend")


def test_traceability_review_no_linked_ticket_posts_comment_without_backend(tmp_xdg, tmp_path):
    """When resolve_linked_tickets finds nothing via either mechanism, the "no linked
    ticket" comment is posted directly and the pass is marked done — no backend call,
    since there's nothing for a model to judge (§7.1/§7.3)."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [7])
    state.add_design_reviewed_pr("acme-frontend", 7)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 7, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=False),
        patch("harness.runners.self_review.resolve_linked_tickets", return_value=[]),
        patch("harness.runners.self_review.post_no_linked_ticket_comment", return_value=True) as mock_post,
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    mock_post.assert_called_once()
    assert mock_post.call_args.args[:2] == (7, "acme/frontend")
    assert 7 in state.get_traceability_reviewed_prs("acme-frontend")


def test_traceability_review_inconclusive_ticket_lookup_skips_without_posting(tmp_xdg, tmp_path):
    """When resolve_linked_tickets returns None (a closing-keyword ref existed but failed
    to resolve due to an API failure, not a confirmed absence), the PR must be skipped —
    no backend call, no terminal "no linked ticket" comment, and left off
    traceability_reviewed_prs so the next run retries."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [7])
    state.add_design_reviewed_pr("acme-frontend", 7)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 7, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=False),
        patch("harness.runners.self_review.resolve_linked_tickets", return_value=None),
        patch("harness.runners.self_review.post_no_linked_ticket_comment") as mock_post,
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    mock_post.assert_not_called()
    assert 7 not in state.get_traceability_reviewed_prs("acme-frontend")


def test_traceability_review_no_linked_ticket_comment_post_failure_leaves_unmarked(tmp_xdg, tmp_path):
    """If posting the "no linked ticket" comment itself fails, the PR is left off
    traceability_reviewed_prs so the next run retries rather than silently losing the
    outcome."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [7])
    state.add_design_reviewed_pr("acme-frontend", 7)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 7, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=False),
        patch("harness.runners.self_review.resolve_linked_tickets", return_value=[]),
        patch("harness.runners.self_review.post_no_linked_ticket_comment", return_value=False),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    assert 7 not in state.get_traceability_reviewed_prs("acme-frontend")


def test_traceability_review_failure_does_not_block_files_summary_state(tmp_xdg, tmp_path):
    """A traceability-pass failure must not prevent reviewed_prs from being updated, and
    must not itself be recorded as done — the two are tracked independently."""
    state.write_self_review_state("acme-frontend", [])
    state.add_design_reviewed_pr("acme-frontend", 8)
    _setup_knowledge(tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    ticket = {"number": 3, "repo": "acme/frontend", "title": "t", "body": "b", "source": "closing_keyword"}
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 8, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.check_review_summary_comment_status", side_effect=[False, True]),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=False),
        patch("harness.runners.self_review.resolve_linked_tickets", return_value=[ticket]),
        patch("harness.runners.self_review.build_early_comment_context", return_value=""),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=["src/foo.py"]),
        patch("harness.runners.self_review.get_file_diff", return_value="@@diff"),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        # File review + summary succeed (returncode 0); the traceability-review call —
        # the third and last backend invocation — fails.
        mock_be.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=1),
        ]
        self_review._run_locked(cfg)
    assert 8 in state.read_self_review_state("acme-frontend")["reviewed_prs"]
    assert 8 not in state.get_traceability_reviewed_prs("acme-frontend")


def test_traceability_review_marker_found_marks_done_without_invoking_backend(tmp_xdg, tmp_path):
    """Defense-in-depth: if a traceability-review comment is already on GitHub (e.g. a
    prior state write didn't persist), the pass is recorded as done without re-invoking."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [10])
    state.add_design_reviewed_pr("acme-frontend", 10)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 10, "url": "u", "headRefName": "b"}]
        ),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    assert 10 in state.get_traceability_reviewed_prs("acme-frontend")


def test_traceability_review_not_marked_done_when_comment_confirmed_missing(tmp_xdg, tmp_path):
    """The backend exiting 0 is not proof it posted — if the post-run GitHub check
    confirms no traceability-review comment exists, the pass must not be recorded as
    done, so a future run retries instead of silently skipping it forever."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [11])
    state.add_design_reviewed_pr("acme-frontend", 11)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    ticket = {"number": 3, "repo": "acme/frontend", "title": "t", "body": "b", "source": "closing_keyword"}
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 11, "url": "u", "headRefName": "b"}]
        ),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=False),
        patch("harness.runners.self_review.resolve_linked_tickets", return_value=[ticket]),
        patch("harness.runners.self_review.build_early_comment_context", return_value=""),
        patch("harness.runners.self_review.check_traceability_review_comment_status", return_value=False),
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
    assert 11 not in state.get_traceability_reviewed_prs("acme-frontend")


def test_traceability_review_not_marked_done_when_comment_check_inconclusive(tmp_xdg, tmp_path):
    """If the post-run GitHub check itself fails (e.g. transient API error), that's
    inconclusive, not a confirmed absence — the pass must still not be marked done."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [12])
    state.add_design_reviewed_pr("acme-frontend", 12)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "foo.py").write_text("def foo():\n    pass\n")
    cfg = _cfg(tmp_path)
    ticket = {"number": 3, "repo": "acme/frontend", "title": "t", "body": "b", "source": "closing_keyword"}
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs", return_value=[{"number": 12, "url": "u", "headRefName": "b"}]
        ),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=False),
        patch("harness.runners.self_review.resolve_linked_tickets", return_value=[ticket]),
        patch("harness.runners.self_review.build_early_comment_context", return_value=""),
        patch("harness.runners.self_review.check_traceability_review_comment_status", return_value=None),
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
    assert 12 not in state.get_traceability_reviewed_prs("acme-frontend")


def test_traceability_review_already_done_is_skipped_without_any_backend_call(tmp_xdg, tmp_path):
    """Once traceability_reviewed_prs has the PR, and files/summary/design are also
    already done, nothing runs at all this cycle — not even a checkout."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [9])
    state.add_design_reviewed_pr("acme-frontend", 9)
    state.add_traceability_reviewed_pr("acme-frontend", 9)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch("harness.runners.self_review._list_my_prs", return_value=[{"number": 9, "url": "u", "headRefName": "b"}]),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout") as mock_checkout,
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    mock_checkout.assert_not_called()


def _regret_hunk():
    return IntroducingHunk(
        path="src/foo.py",
        old_start=3,
        old_end=5,
        new_start=3,
        new_end=4,
        introducing_sha="cafe0001",
        introducing_pr_number=42,
    )


def _regret_candidate(hunk):
    return RegretCandidate(
        comment_id=123,
        comment_body="This will break when the input is empty",
        comment_author="bob",
        comment_diff_hunk="",
        comment_url="https://github.com/acme/frontend/pull/42#discussion_r123",
        hunk=hunk,
    )


def test_regret_review_not_run_when_disabled(tmp_xdg, tmp_path):
    """[regret_review].enabled is false by default — the pass is never entered at all,
    even for a PR that is in none of the state lists yet (the enabled gate is unique to
    this pass; design/traceability have none)."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [])
    state.add_design_reviewed_pr("acme-frontend", 50)
    state.add_traceability_reviewed_pr("acme-frontend", 50)
    cfg = _cfg(tmp_path)
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs",
            return_value=[{"number": 50, "url": "u", "headRefName": "b", "author": {"login": "alice"}}],
        ),
        patch("harness.runners.self_review.check_review_summary_comment_status", return_value=True),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout") as mock_checkout,
        patch("harness.runners.self_review._run_regret_review") as mock_regret,
    ):
        self_review._run_locked(cfg)
    mock_regret.assert_not_called()
    mock_checkout.assert_not_called()


def test_regret_review_not_run_when_already_regret_reviewed(tmp_xdg, tmp_path):
    """Once regret_reviewed_prs has the PR (pass enabled), and files/summary/design/
    traceability are also already done, nothing runs at all this cycle — not even a
    checkout."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [51])
    state.add_design_reviewed_pr("acme-frontend", 51)
    state.add_traceability_reviewed_pr("acme-frontend", 51)
    state.add_regret_reviewed_pr("acme-frontend", 51)
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs",
            return_value=[{"number": 51, "url": "u", "headRefName": "b", "author": {"login": "alice"}}],
        ),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout") as mock_checkout,
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    mock_checkout.assert_not_called()


def test_regret_review_marker_found_marks_done_without_invoking_backend(tmp_xdg, tmp_path):
    """Idempotency: if a regret comment is already on GitHub (e.g. a prior state write
    didn't persist), the pass is recorded as done without re-invoking — zero calls to
    the candidate pipeline and no backend call."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [52])
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs",
            return_value=[{"number": 52, "url": "u", "headRefName": "b", "author": {"login": "alice"}}],
        ),
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
        patch("harness.runners.self_review.has_regret_review_comment", return_value=True),
        patch("harness.runners.self_review.find_introducing_prs") as mock_find_prs,
        patch("harness.runners.self_review.find_regret_candidates") as mock_find_candidates,
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_vibe_heal_context", return_value=None),
        patch("harness.runners.self_review.get_pr_description", return_value=None),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
    ):
        self_review._run_locked(cfg)
    mock_find_prs.assert_not_called()
    mock_find_candidates.assert_not_called()
    mock_be.return_value.run.assert_not_called()
    assert 52 in state.get_regret_reviewed_prs("acme-frontend")


def test_regret_review_posts_comment_and_marks_done_on_findings(tmp_xdg, tmp_path):
    """The full pipeline: candidate search → backend verdict lines → findings parsed
    from the reply → regret comment posted by the harness (the backend posts nothing) →
    marker confirmed → marked done."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [53])
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs",
            return_value=[{"number": 53, "url": "u", "headRefName": "b", "author": {"login": "alice"}}],
        ),
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
        patch("harness.runners.self_review.has_regret_review_comment", return_value=False),
        patch("harness.runners.self_review.find_introducing_prs", return_value=[_regret_hunk()]),
        patch("harness.runners.self_review.find_regret_candidates", return_value=[_regret_candidate(_regret_hunk())]),
        patch("harness.runners.self_review.check_regret_review_comment_status", return_value=True),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_vibe_heal_context", return_value=None),
        patch("harness.runners.self_review.get_pr_description", return_value=None),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
        patch("harness.runners.self_review.post_regret_comment", return_value=True) as mock_post,
    ):
        mock_be.return_value.run.return_value = MagicMock(
            returncode=0, stdout=b"CANDIDATE 1: YES - it predicted the empty-input failure\n"
        )
        self_review._run_locked(cfg)
    assert mock_be.return_value.run.call_count == 1
    assert mock_be.return_value.run.call_args.kwargs["context"] == "PR #53 regret review"
    assert 53 in state.get_regret_reviewed_prs("acme-frontend")
    assert mock_post.call_count == 1
    body = mock_post.call_args.args[2]
    assert "# Regret Review" in body
    assert "src/foo.py:3" in body
    assert "#42" in body
    assert "https://github.com/acme/frontend/pull/42#discussion_r123" in body
    assert "This will break when the input is empty" in body
    assert REGRET_REVIEW_MARKER in body


def test_regret_review_no_candidates_no_backend_call_and_unmarked(tmp_xdg, tmp_path):
    """Blame resolved an introducing PR but no comment matched the blamed hunk — there
    is structurally nothing to judge: no backend call, no comment posted, and the PR is
    NOT marked regret-reviewed, so a later PR update gets a fresh candidate search."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [54])
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs",
            return_value=[{"number": 54, "url": "u", "headRefName": "b", "author": {"login": "alice"}}],
        ),
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
        patch("harness.runners.self_review.has_regret_review_comment", return_value=False),
        patch("harness.runners.self_review.find_introducing_prs", return_value=[_regret_hunk()]),
        patch("harness.runners.self_review.find_regret_candidates", return_value=[]),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_vibe_heal_context", return_value=None),
        patch("harness.runners.self_review.get_pr_description", return_value=None),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
        patch("harness.runners.self_review.post_regret_comment") as mock_post,
    ):
        self_review._run_locked(cfg)
    mock_be.return_value.run.assert_not_called()
    mock_post.assert_not_called()
    assert 54 not in state.get_regret_reviewed_prs("acme-frontend")


def test_regret_review_no_findings_marks_done_without_posting(tmp_xdg, tmp_path):
    """The backend judged every candidate NO — nothing is posted, but the run still
    counts as complete: there is nothing to post, and the PR is marked done like any
    other success, so the same candidates aren't re-judged on every future run."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [55])
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs",
            return_value=[{"number": 55, "url": "u", "headRefName": "b", "author": {"login": "alice"}}],
        ),
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
        patch("harness.runners.self_review.has_regret_review_comment", return_value=False),
        patch("harness.runners.self_review.find_introducing_prs", return_value=[_regret_hunk()]),
        patch("harness.runners.self_review.find_regret_candidates", return_value=[_regret_candidate(_regret_hunk())]),
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_vibe_heal_context", return_value=None),
        patch("harness.runners.self_review.get_pr_description", return_value=None),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
        patch("harness.runners.self_review.post_regret_comment") as mock_post,
    ):
        mock_be.return_value.run.return_value = MagicMock(
            returncode=0, stdout=b"CANDIDATE 1: NO - it was about a different edge case\n"
        )
        self_review._run_locked(cfg)
    assert mock_be.return_value.run.call_count == 1
    mock_post.assert_not_called()
    assert 55 in state.get_regret_reviewed_prs("acme-frontend")


def test_regret_review_post_failure_leaves_unmarked(tmp_xdg, tmp_path):
    """A backend verdict that parses into findings is not enough: if posting the regret
    comment fails, the PR must not be marked regret-reviewed, so the next run retries."""
    _setup_knowledge(tmp_path)
    state.write_self_review_state("acme-frontend", [56])
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        patch("harness.runners.self_review.get_gh_token", return_value="tok"),
        patch("harness.runners.self_review.get_current_user", return_value="alice"),
        patch(
            "harness.runners.self_review._list_my_prs",
            return_value=[{"number": 56, "url": "u", "headRefName": "b", "author": {"login": "alice"}}],
        ),
        patch("harness.runners.self_review.has_design_review_comment", return_value=True),
        patch("harness.runners.self_review.has_traceability_review_comment", return_value=True),
        patch("harness.runners.self_review.has_regret_review_comment", return_value=False),
        patch("harness.runners.self_review.find_introducing_prs", return_value=[_regret_hunk()]),
        patch("harness.runners.self_review.find_regret_candidates", return_value=[_regret_candidate(_regret_hunk())]),
        patch("harness.runners.self_review.check_regret_review_comment_status") as mock_check,
        patch("harness.runners.self_review.git_detach_and_record", return_value="sha"),
        patch("harness.runners.self_review.git_fetch_and_checkout"),
        patch("harness.runners.self_review.git_restore"),
        patch("harness.runners.self_review.get_vibe_heal_context", return_value=None),
        patch("harness.runners.self_review.get_pr_description", return_value=None),
        patch("harness.runners.self_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.self_review.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.self_review.get_changed_files", return_value=[]),
        patch("harness.runners.self_review.Backend") as mock_be,
        patch("harness.runners.self_review.post_regret_comment", return_value=False),
    ):
        mock_be.return_value.run.return_value = MagicMock(
            returncode=0, stdout=b"CANDIDATE 1: YES - it predicted the empty-input failure\n"
        )
        self_review._run_locked(cfg)
    mock_check.assert_not_called()
    assert 56 not in state.get_regret_reviewed_prs("acme-frontend")
