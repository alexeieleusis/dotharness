import contextlib
import json
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

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


def _strict_run_cmd(mapping: dict[str, MagicMock]):
    """Build a run_cmd side_effect that raises on any unmocked command.

    `mapping` maps a substring of the command line to a pre-built MagicMock result.
    This turns silent test failures into loud assertion errors when a command
    in the source changes or a mapping key has a typo.
    """

    def side_effect(cmd, **_kwargs):
        key = " ".join(str(a) for a in cmd)
        for k, v in mapping.items():
            if k in key:
                return v
        raise AssertionError(f"Unmocked command: {' '.join(str(a) for a in cmd)}")  # noqa: TRY003

    return side_effect


@contextlib.contextmanager
def _full_run_mocks(*, prs=None, changed_files=None, skip_pr=False):
    """Patch every dependency `_run_locked` needs to drive a full PR review pass.

    Yields a namespace of the mocks so each test can tweak a return value or
    side effect for the piece it cares about, instead of repeating all ~15
    patch targets `_run_locked` touches on every call.
    """
    prs = prs if prs is not None else [{"number": 1, "url": "u", "headRefName": "feat"}]
    changed_files = changed_files if changed_files is not None else ["src/foo.py"]
    with (
        patch("harness.runners.review_requested.get_gh_token", return_value="tok"),
        patch("harness.runners.review_requested.get_current_user", return_value="bot"),
        patch("harness.runners.review_requested._get_prs", return_value=prs) as get_prs,
        patch("harness.runners.review_requested._should_skip_pr", return_value=skip_pr) as should_skip_pr,
        patch("harness.runners.review_requested.git_detach_and_record", return_value="sha") as detach,
        patch("harness.runners.review_requested.git_fetch_and_checkout") as fetch_checkout,
        patch("harness.runners.review_requested.git_restore") as restore,
        patch("harness.runners.review_requested.run_cmd", return_value=MagicMock(returncode=0, stdout=b"")) as run_cmd,
        patch("harness.runners.review_requested.get_pr_base_branch", return_value="main"),
        patch("harness.runners.review_requested.get_pr_head_sha", return_value="abc123"),
        patch("harness.runners.review_requested.get_changed_files", return_value=changed_files),
        patch("harness.runners.review_requested.get_file_diff", return_value="@@diff"),
        patch("harness.runners.review_requested.os") as os_mock,
        patch("harness.runners.review_requested.Backend") as backend,
    ):
        os_mock.path.exists.return_value = True
        os_mock.path.join.side_effect = lambda *parts: "/".join(parts)
        backend.return_value.run.return_value = MagicMock(returncode=0)
        yield SimpleNamespace(
            get_prs=get_prs,
            should_skip_pr=should_skip_pr,
            detach=detach,
            fetch_checkout=fetch_checkout,
            restore=restore,
            run_cmd=run_cmd,
            os=os_mock,
            backend=backend,
        )


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
    with _full_run_mocks(changed_files=["src/a.py", "src/b.py"]) as mocks:
        review_requested._run_locked(cfg, pr_url=None)
    # 2 files + 1 summary = 3 backend calls
    assert mocks.backend.return_value.run.call_count == 3


def test_vibe_heal_context_included_in_prompts(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.get_vibe_heal_context", return_value="sonar findings"),
    ):
        review_requested._run_locked(cfg, pr_url=None)
    # backend.run(prompt, cwd=wdir) — prompt is first positional arg
    prompts = [c.args[0] for c in mocks.backend.return_value.run.call_args_list]
    assert all("## Static Analysis" in p for p in prompts)
    assert all("sonar findings" in p for p in prompts)


def test_get_prs_returns_head_ref_name_from_single_call(tmp_xdg, tmp_path):
    # gh pr list --json can return headRefName directly, avoiding the N+1 pattern
    # of gh search prs + individual gh pr view calls.
    list_result = [{"number": 42, "url": "https://github.com/acme/frontend/pull/42", "headRefName": "feat/my-branch"}]

    with patch(
        "harness.runners.review_requested.run_cmd",
        side_effect=_strict_run_cmd({"gh pr list": MagicMock(returncode=0, stdout=json.dumps(list_result).encode())}),
    ) as mock_run:
        prs = review_requested._get_prs("acme/frontend", {})

    assert prs == list_result
    mock_run.assert_called_once()


def test_get_prs_returns_empty_on_search_failure(tmp_xdg, tmp_path):
    with patch(
        "harness.runners.review_requested.run_cmd",
        side_effect=_strict_run_cmd({"gh pr list": MagicMock(returncode=1, stdout=b"")}),
    ):
        prs = review_requested._get_prs("acme/frontend", {})
    assert prs == []


def test_get_prs_searches_user_review_requested_not_reviewer(tmp_xdg, tmp_path):
    # "reviewer:@me" does not reliably match pending review requests on GitHub's
    # search API; only "user-review-requested:@me" does. Regression test for a bug
    # where this qualifier was swapped during a refactor, silently making the runner
    # find zero PRs. Must be "user-review-requested:@me" specifically (not the bare
    # "review-requested:@me"), which also matches team-based requests Alexei doesn't
    # want surfaced here.
    with patch("harness.runners.review_requested.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        review_requested._get_prs("acme/frontend", {})

    args = mock_run.call_args[0][0]
    assert "--search" in args
    assert args[args.index("--search") + 1] == "user-review-requested:@me"


def test_has_user_approved_returns_false_on_non_list_response(tmp_xdg, tmp_path):
    # gh api can return a JSON error object (e.g. {"message": "Not Found"}) instead
    # of a list of reviews; iterating over that dict yields its keys as strings,
    # which used to crash with "string indices must be integers, not 'str'".
    with patch("harness.runners.review_requested.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps({"message": "Not Found"}).encode())
        approved = review_requested._has_user_approved(1, "acme/frontend", "me", {})
    assert approved is False


def test_vibe_heal_context_absent_when_empty(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.get_vibe_heal_context", return_value=""),
    ):
        review_requested._run_locked(cfg, pr_url=None)
    prompts = [c.args[0] for c in mocks.backend.return_value.run.call_args_list]
    assert all("## Static Analysis" not in p for p in prompts)


def test_continues_to_next_pr_on_backend_exception(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    prs = [
        {"number": 1, "url": "u1", "headRefName": "feat1"},
        {"number": 2, "url": "u2", "headRefName": "feat2"},
    ]
    with _full_run_mocks(prs=prs) as mocks:
        mocks.backend.return_value.run.side_effect = [RuntimeError("backend crash"), MagicMock(returncode=0)]
        review_requested._run_locked(cfg, pr_url=None)

    restore_calls = mocks.restore.call_args_list
    assert len(restore_calls) == 2
    assert restore_calls[0][0][0] == "sha"
    assert restore_calls[0][0][1] == "feat1"
    assert restore_calls[1][0][1] == "feat2"
    assert mocks.detach.call_count == 2


def test_restores_head_on_backend_failure(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with _full_run_mocks() as mocks:
        mocks.backend.return_value.run.side_effect = RuntimeError("backend crash")
        review_requested._run_locked(cfg, pr_url=None)
    mocks.restore.assert_called_once_with("sha", "feat", str(cfg.repo.working_dir), ANY)
