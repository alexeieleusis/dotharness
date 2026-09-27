import contextlib
import json
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

from harness.runners import review_requested
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
        # Design/traceability passes are decoupled and out of scope for these
        # correctness/state tests — treat them as already-done so neither invokes the
        # backend or hits `gh` for real (each is tested on its own below).
        patch("harness.runners.review_requested.has_design_review_comment", return_value=True),
        patch("harness.runners.review_requested.has_traceability_review_comment", return_value=True),
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
        patch("harness.runners.review_requested.has_design_review_comment", return_value=True),
        patch("harness.runners.review_requested.has_traceability_review_comment", return_value=True),
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
        patch("harness.runners.review_requested.has_design_review_comment", return_value=True),
        patch("harness.runners.review_requested.has_traceability_review_comment", return_value=True),
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


def test_design_review_invoked_when_no_marker_present(tmp_xdg, tmp_path):
    """With no persisted state, has_design_review_comment is the only idempotency
    signal — when it's absent, the design pass must actually invoke the backend."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_design_review_comment", return_value=False),
    ):
        review_requested._run_locked(cfg, pr_url=None)
    # 1 file + 1 summary + 1 design = 3 backend calls
    assert mocks.backend.return_value.run.call_count == 3
    assert mocks.backend.return_value.run.call_args_list[-1].kwargs["context"] == "PR #1 design review"


def test_design_review_noop_when_marker_already_present(tmp_xdg, tmp_path):
    """A design-review comment from a prior attempt makes this cycle's design step a
    noop — the backend must not be invoked a second time for it."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_design_review_comment", return_value=True),
    ):
        review_requested._run_locked(cfg, pr_url=None)
    # 1 file + 1 summary = 2 backend calls; design is skipped entirely.
    assert mocks.backend.return_value.run.call_count == 2


def test_remove_reviewer_blocked_when_design_review_fails(tmp_xdg, tmp_path):
    """Files and summary succeed, but the design pass fails — the reviewer must not be
    removed, so the PR stays in the queue and gets retried next run."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_design_review_comment", return_value=False),
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        # file review, then summary, then a failing design-review call
        mocks.backend.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=1),
        ]
        review_requested._run_locked(cfg, pr_url=None)
    mock_remove.assert_not_called()


def test_remove_reviewer_blocked_when_marker_comment_never_lands(tmp_xdg, tmp_path):
    """The backend can exit 0 without the PR-level marker comment actually landing (e.g.
    it crashed after doing the work but before posting). That must not be treated as
    success — the reviewer stays on the PR so the design pass retries next cycle."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_design_review_comment", return_value=False),
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        mocks.backend.return_value.run.return_value = MagicMock(returncode=0)
        review_requested._run_locked(cfg, pr_url=None)
    mock_remove.assert_not_called()


def test_remove_reviewer_proceeds_when_design_review_noops(tmp_xdg, tmp_path):
    """An already-posted design comment still counts as success for the remove_reviewer
    gate — the pass being a noop must not block clearing the review request."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks(),
        patch("harness.runners.review_requested.has_design_review_comment", return_value=True),
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        review_requested._run_locked(cfg, pr_url=None)
    mock_remove.assert_called_once()


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


_TICKET = {"number": 3, "repo": "acme/frontend", "title": "Do the thing", "body": "body", "source": "closing_keyword"}


def test_traceability_review_invoked_when_no_marker_present(tmp_xdg, tmp_path):
    """With no persisted state, has_traceability_review_comment is the only idempotency
    signal — when it's absent and a ticket resolves, the pass must actually invoke the
    backend."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_traceability_review_comment", return_value=False),
        patch("harness.runners.review_requested.resolve_linked_tickets", return_value=[_TICKET]),
        patch("harness.runners.review_requested.build_early_comment_context", return_value=""),
        patch("harness.runners.review_requested.get_traceability_review_flagged_locations", return_value=[]),
    ):
        review_requested._run_locked(cfg, pr_url=None)
    # 1 file + 1 summary + 1 traceability = 3 backend calls
    assert mocks.backend.return_value.run.call_count == 3
    assert mocks.backend.return_value.run.call_args_list[-1].kwargs["context"] == "PR #1 traceability review"
    prompt = mocks.backend.return_value.run.call_args_list[-1].args[0]
    assert "## Linked Ticket(s)" in prompt
    assert "Do the thing" in prompt


def test_traceability_review_noop_when_marker_already_present(tmp_xdg, tmp_path):
    """A traceability-review comment from a prior attempt makes this cycle's step a
    noop — the backend must not be invoked a second time for it, and no ticket
    resolution is needed."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_traceability_review_comment", return_value=True),
    ):
        review_requested._run_locked(cfg, pr_url=None)
    # 1 file + 1 summary = 2 backend calls; traceability is skipped entirely.
    assert mocks.backend.return_value.run.call_count == 2


def test_traceability_review_no_linked_ticket_posts_comment_without_backend(tmp_xdg, tmp_path):
    """When resolve_linked_tickets finds nothing, the "no linked ticket" comment is
    posted directly — no backend call for this pass, since there's nothing to judge."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_traceability_review_comment", return_value=False),
        patch("harness.runners.review_requested.resolve_linked_tickets", return_value=[]),
        patch("harness.runners.review_requested.post_no_linked_ticket_comment", return_value=True) as mock_post,
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        review_requested._run_locked(cfg, pr_url=None)
    # 1 file + 1 summary = 2 backend calls; traceability never invokes the backend.
    assert mocks.backend.return_value.run.call_count == 2
    mock_post.assert_called_once()
    assert mock_post.call_args.args[:2] == (1, "acme/frontend")
    mock_remove.assert_called_once()


def test_remove_reviewer_blocked_when_ticket_lookup_is_inconclusive(tmp_xdg, tmp_path):
    """When resolve_linked_tickets returns None (a closing-keyword ref existed but failed
    to resolve due to an API failure, not a confirmed absence), the reviewer must not be
    removed and no terminal "no linked ticket" comment must be posted — the PR is retried
    next cycle instead of being permanently marked as having no ticket."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks(),
        patch("harness.runners.review_requested.has_traceability_review_comment", return_value=False),
        patch("harness.runners.review_requested.resolve_linked_tickets", return_value=None),
        patch("harness.runners.review_requested.post_no_linked_ticket_comment") as mock_post,
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        review_requested._run_locked(cfg, pr_url=None)
    mock_post.assert_not_called()
    mock_remove.assert_not_called()


def test_remove_reviewer_blocked_when_traceability_no_ticket_comment_post_fails(tmp_xdg, tmp_path):
    """If posting the "no linked ticket" comment itself fails, the reviewer must not be
    removed, so the outcome is retried next cycle rather than silently lost."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks(),
        patch("harness.runners.review_requested.has_traceability_review_comment", return_value=False),
        patch("harness.runners.review_requested.resolve_linked_tickets", return_value=[]),
        patch("harness.runners.review_requested.post_no_linked_ticket_comment", return_value=False),
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        review_requested._run_locked(cfg, pr_url=None)
    mock_remove.assert_not_called()


def test_remove_reviewer_blocked_when_traceability_review_fails(tmp_xdg, tmp_path):
    """Files and summary succeed, but the traceability pass fails — the reviewer must
    not be removed, so the PR stays in the queue and gets retried next run."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_traceability_review_comment", return_value=False),
        patch("harness.runners.review_requested.resolve_linked_tickets", return_value=[_TICKET]),
        patch("harness.runners.review_requested.build_early_comment_context", return_value=""),
        patch("harness.runners.review_requested.get_traceability_review_flagged_locations", return_value=[]),
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        # file review, then summary, then a failing traceability-review call
        mocks.backend.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=1),
        ]
        review_requested._run_locked(cfg, pr_url=None)
    mock_remove.assert_not_called()


def test_remove_reviewer_blocked_when_traceability_marker_comment_never_lands(tmp_xdg, tmp_path):
    """The backend can exit 0 without the PR-level marker comment actually landing. That
    must not be treated as success — the reviewer stays on the PR so the traceability
    pass retries next cycle."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_traceability_review_comment", return_value=False),
        patch("harness.runners.review_requested.resolve_linked_tickets", return_value=[_TICKET]),
        patch("harness.runners.review_requested.build_early_comment_context", return_value=""),
        patch("harness.runners.review_requested.get_traceability_review_flagged_locations", return_value=[]),
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        mocks.backend.return_value.run.return_value = MagicMock(returncode=0)
        review_requested._run_locked(cfg, pr_url=None)
    mock_remove.assert_not_called()


def test_remove_reviewer_proceeds_when_traceability_review_noops(tmp_xdg, tmp_path):
    """An already-posted traceability comment still counts as success for the
    remove_reviewer gate — the pass being a noop must not block clearing the review
    request."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks(),
        patch("harness.runners.review_requested.has_traceability_review_comment", return_value=True),
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        review_requested._run_locked(cfg, pr_url=None)
    mock_remove.assert_called_once()


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
    and no regret marker check is made (the enabled gate is unique to this pass;
    design/traceability have none)."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_regret_review_comment") as mock_has_regret,
        patch("harness.runners.review_requested._run_regret_review") as mock_regret,
    ):
        review_requested._run_locked(cfg, pr_url=None)
    mock_has_regret.assert_not_called()
    mock_regret.assert_not_called()
    # 1 file + 1 summary = 2 backend calls; regret is never entered.
    assert mocks.backend.return_value.run.call_count == 2


def test_regret_review_noop_when_marker_already_present(tmp_xdg, tmp_path):
    """A regret-review comment from a prior attempt makes this cycle's regret step a
    noop — no candidate-pipeline calls and no backend call for it. This runner has no
    persisted state, so the marker check is the pass's only idempotency signal, and an
    already-posted comment still counts as success for the remove_reviewer gate."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_regret_review_comment", return_value=True),
        patch("harness.runners.review_requested.find_introducing_prs") as mock_find_prs,
        patch("harness.runners.review_requested.find_regret_candidates") as mock_find_candidates,
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        review_requested._run_locked(cfg, pr_url=None)
    mock_find_prs.assert_not_called()
    mock_find_candidates.assert_not_called()
    # 1 file + 1 summary = 2 backend calls; regret is skipped entirely.
    assert mocks.backend.return_value.run.call_count == 2
    mock_remove.assert_called_once()


def test_regret_review_posts_comment_and_confirms_marker_on_findings(tmp_xdg, tmp_path):
    """The full pipeline: candidate search → backend verdict lines → findings parsed
    from the recorded reply → regret comment posted by the harness (the backend posts
    nothing) → marker confirmed via the plain-bool has_regret_review_comment →
    reviewer removed."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        _full_run_mocks() as mocks,
        # False when the loop checks regret_done; True when the post-run confirmation
        # re-checks the marker after the harness posts.
        patch("harness.runners.review_requested.has_regret_review_comment", side_effect=[False, True]),
        patch("harness.runners.review_requested.find_introducing_prs", return_value=[_regret_hunk()]),
        patch(
            "harness.runners.review_requested.find_regret_candidates",
            return_value=[_regret_candidate(_regret_hunk())],
        ),
        patch("harness.runners.review_requested.post_regret_comment", return_value=True) as mock_post,
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        # file review, then summary, then a regret-review call with verdict lines
        mocks.backend.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=b"CANDIDATE 1: YES - it predicted the empty-input failure\n"),
        ]
        review_requested._run_locked(cfg, pr_url=None)
    assert mocks.backend.return_value.run.call_count == 3
    assert mocks.backend.return_value.run.call_args_list[-1].kwargs["context"] == "PR #1 regret review"
    assert mock_post.call_count == 1
    body = mock_post.call_args.args[2]
    assert "# Previously flagged review comments" in body
    assert "src/foo.py:3" in body
    assert "#42" in body
    assert "https://github.com/acme/frontend/pull/42#discussion_r123" in body
    assert "This will break when the input is empty" in body
    assert REGRET_REVIEW_MARKER in body
    mock_remove.assert_called_once()


def test_regret_review_no_candidates_no_backend_call(tmp_xdg, tmp_path):
    """Blame resolved an introducing PR but no comment matched the blamed hunk — there
    is structurally nothing to judge: no backend call for this pass, no comment posted.
    Nothing is persisted in this stateless runner, so a later run of this PR simply
    gets a fresh candidate search; for this run the empty outcome counts as complete,
    so the reviewer is removed like any other pass that succeeded."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_regret_review_comment", return_value=False),
        patch("harness.runners.review_requested.find_introducing_prs", return_value=[_regret_hunk()]),
        patch("harness.runners.review_requested.find_regret_candidates", return_value=[]),
        patch("harness.runners.review_requested.post_regret_comment") as mock_post,
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        review_requested._run_locked(cfg, pr_url=None)
    # 1 file + 1 summary = 2 backend calls; regret never invokes the backend.
    assert mocks.backend.return_value.run.call_count == 2
    mock_post.assert_not_called()
    mock_remove.assert_called_once()


def test_regret_review_no_findings_counts_complete_without_posting(tmp_xdg, tmp_path):
    """The backend judged every candidate NO — nothing is posted, but the run still
    counts as complete: there is nothing to post. Unlike self_review.py, nothing is
    persisted here, so a later run simply re-judges the candidates; for this run the
    reviewer is removed like any other completed pass."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_regret_review_comment", return_value=False),
        patch("harness.runners.review_requested.find_introducing_prs", return_value=[_regret_hunk()]),
        patch(
            "harness.runners.review_requested.find_regret_candidates",
            return_value=[_regret_candidate(_regret_hunk())],
        ),
        patch("harness.runners.review_requested.post_regret_comment") as mock_post,
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        mocks.backend.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=b"CANDIDATE 1: NO - it was about a different edge case\n"),
        ]
        review_requested._run_locked(cfg, pr_url=None)
    assert mocks.backend.return_value.run.call_count == 3
    mock_post.assert_not_called()
    mock_remove.assert_called_once()


def test_regret_review_post_failure_blocks_remove_reviewer(tmp_xdg, tmp_path):
    """A backend verdict that parses into findings is not enough: if posting the regret
    comment fails, the reviewer must not be removed, so the outcome is retried next
    cycle rather than silently lost. This runner has no persisted state to record the
    failure, so the still-absent marker is what keeps the retry alive."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_regret_review_comment", return_value=False),
        patch("harness.runners.review_requested.find_introducing_prs", return_value=[_regret_hunk()]),
        patch(
            "harness.runners.review_requested.find_regret_candidates",
            return_value=[_regret_candidate(_regret_hunk())],
        ),
        patch("harness.runners.review_requested.post_regret_comment", return_value=False) as mock_post,
        patch("harness.runners.review_requested.remove_reviewer") as mock_remove,
    ):
        mocks.backend.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=b"CANDIDATE 1: YES - it predicted the empty-input failure\n"),
        ]
        review_requested._run_locked(cfg, pr_url=None)
    assert mock_post.call_count == 1
    mock_remove.assert_not_called()


def test_regret_review_writes_no_persisted_state(tmp_xdg, tmp_path):
    """This runner is deliberately stateless (like its design/traceability passes): a
    regret run that posts a comment must not create self-review's state file or add
    the PR to regret_reviewed_prs the way self_review.py's wiring does."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        _full_run_mocks() as mocks,
        patch("harness.runners.review_requested.has_regret_review_comment", side_effect=[False, True]),
        patch("harness.runners.review_requested.find_introducing_prs", return_value=[_regret_hunk()]),
        patch(
            "harness.runners.review_requested.find_regret_candidates",
            return_value=[_regret_candidate(_regret_hunk())],
        ),
        patch("harness.runners.review_requested.post_regret_comment", return_value=True),
        patch("harness.runners.review_requested.remove_reviewer"),
    ):
        mocks.backend.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=b"CANDIDATE 1: YES - it predicted the empty-input failure\n"),
        ]
        review_requested._run_locked(cfg, pr_url=None)
    assert not (tmp_xdg / "state" / "acme-frontend" / "self_review.json").exists()
