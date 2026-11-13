import contextlib
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from dotharness.code_host import (
    Capability,
    ChangeRequestState,
    CodeHostError,
    CodeHostTimeoutError,
    ReviewState,
    UnsupportedCapabilityError,
    User,
)
from dotharness.pr_review.runners import review_requested
from dotharness.pr_review.runners.common import (
    DESIGN_REVIEW_MARKER,
    INLINE_REVIEW_MARKER,
    REGRET_REVIEW_MARKER,
    TRACEABILITY_REVIEW_MARKER,
    LinkedTicket,
    post_no_linked_ticket_comment,
)
from dotharness.pr_review.runners.regret_review import IntroducingHunk, RegretCandidate
from tests.pr_review.runners.approve_support import (
    T,
    make_host,
    put_comment,
    put_review,
    put_thread,
    seed_pr,
    update_pr,
)
from tests.pr_review.runners.runner_support import REPO, make_change_request, make_scaffold


def _pr(number, **fields):
    return make_change_request(number, **fields)


def _cfg(tmp_path):
    from dotharness.pr_review.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

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


@contextlib.contextmanager
def _full_run_mocks(*, prs=None, changed_files=None, skip_pr=False):
    """Patch every dependency `_run_locked` needs to drive a full PR review pass.

    Yields a namespace of the mocks so each test can tweak a return value or
    side effect for the piece it cares about, instead of repeating all ~15
    patch targets `_run_locked` touches on every call.
    """
    prs = prs if prs is not None else [_pr(1, url="u", head_reference="feat")]
    host = make_host()
    for pr in prs:
        seed_pr(host, pr.number)
    changed_files = changed_files if changed_files is not None else ["src/foo.py"]
    with (
        patch("dotharness.pr_review.runners.review_requested.open_host", return_value=make_scaffold(host)),
        patch("dotharness.pr_review.runners.review_requested.host_current_user", return_value="bot"),
        patch("dotharness.pr_review.runners.review_requested._get_prs", return_value=prs) as get_prs,
        patch("dotharness.pr_review.runners.review_requested._should_skip_pr", return_value=skip_pr) as should_skip_pr,
        # Design/traceability passes are decoupled and out of scope for these
        # correctness/state tests — treat them as already-done so neither invokes the
        # backend or hits `gh` for real (each is tested on its own below).
        patch("dotharness.pr_review.runners.review_requested.has_design_review_comment", return_value=True),
        patch("dotharness.pr_review.runners.review_requested.has_traceability_review_comment", return_value=True),
        # Not previously requested by default, so the re-request path (tested on its own
        # below) doesn't fire a real `gh` call for tests that don't care about it. These
        # are called from inside common.preserve_reviewer_request, not review_requested
        # itself — patched on their defining module accordingly.
        patch("dotharness.pr_review.runners.common.was_review_requested", return_value=False) as get_requested,
        patch("dotharness.pr_review.runners.common.add_reviewer") as add_reviewer,
        patch("dotharness.pr_review.runners.review_requested.git_detach_and_record", return_value="sha") as detach,
        patch("dotharness.pr_review.runners.review_requested.git_fetch_and_checkout") as fetch_checkout,
        patch("dotharness.pr_review.runners.review_requested.git_restore") as restore,
        patch("dotharness.pr_review.runners.review_requested.get_pr_base_branch", return_value="main"),
        patch("dotharness.pr_review.runners.review_requested.get_pr_head_sha", return_value="abc123"),
        patch(
            "dotharness.pr_review.runners.review_requested.get_change_request",
            return_value=make_change_request(1, url="u"),
        ),
        patch("dotharness.pr_review.runners.review_requested.get_changed_files", return_value=changed_files),
        patch("dotharness.pr_review.runners.review_requested.get_file_diff", return_value="@@diff"),
        patch("dotharness.pr_review.runners.review_requested.os") as os_mock,
        patch("dotharness.pr_review.runners.review_requested.Backend") as backend,
    ):
        os_mock.path.exists.return_value = True
        os_mock.path.join.side_effect = lambda *parts: "/".join(parts)
        backend.return_value.run = AsyncMock()
        backend.return_value.run.return_value = MagicMock(returncode=0)
        yield SimpleNamespace(
            get_prs=get_prs,
            should_skip_pr=should_skip_pr,
            detach=detach,
            fetch_checkout=fetch_checkout,
            restore=restore,
            host=host,
            os=os_mock,
            backend=backend,
            get_requested=get_requested,
            add_reviewer=add_reviewer,
        )


def _seeded_scaffold():
    host = make_host()
    seed_pr(host, 1)
    return host, make_scaffold(host)


async def _run_skip_scenario(tmp_path, scaffold, *, design_and_traceability_done=True):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        patch("dotharness.pr_review.runners.review_requested.open_host", return_value=scaffold),
        patch("dotharness.pr_review.runners.review_requested.host_current_user", return_value="me"),
        patch(
            "dotharness.pr_review.runners.review_requested._get_prs",
            return_value=[_pr(1, url="u", head_reference="b")],
        ),
        patch("dotharness.pr_review.runners.review_requested.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_requested.Backend") as mock_be,
    ):
        await review_requested._run_locked(cfg, pr_url=None)
    return mock_be


async def test_skips_already_approved_pr(tmp_xdg, tmp_path):
    host, scaffold = _seeded_scaffold()
    put_review(host, "me", ReviewState.APPROVED, T, 1)
    put_comment(host, "me", f"Design.{DESIGN_REVIEW_MARKER}", T, 1)
    put_comment(host, "me", f"Trace.{TRACEABILITY_REVIEW_MARKER}", T, 1)
    mock_be = await _run_skip_scenario(tmp_path, scaffold)
    mock_be.return_value.run.assert_not_called()
    assert host.writes == []


async def test_skips_pr_with_existing_dotharness_review(tmp_xdg, tmp_path):
    host, scaffold = _seeded_scaffold()
    put_comment(host, "me", "# Review Summary\nNo blocking issues found.", T, 1)
    put_comment(host, "me", f"Design.{DESIGN_REVIEW_MARKER}", T, 1)
    put_comment(host, "me", f"Trace.{TRACEABILITY_REVIEW_MARKER}", T, 1)
    mock_be = await _run_skip_scenario(tmp_path, scaffold)
    mock_be.return_value.run.assert_not_called()


async def test_skips_pr_with_partial_inline_review_comments(tmp_xdg, tmp_path):
    host, scaffold = _seeded_scaffold()
    put_thread(host, [("me", f"Nitpick.{INLINE_REVIEW_MARKER}", T)], 1)
    put_comment(host, "me", f"Design.{DESIGN_REVIEW_MARKER}", T, 1)
    put_comment(host, "me", f"Trace.{TRACEABILITY_REVIEW_MARKER}", T, 1)
    mock_be = await _run_skip_scenario(tmp_path, scaffold)
    mock_be.return_value.run.assert_not_called()


async def test_backend_called_once_per_file_plus_summary(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with _full_run_mocks(changed_files=["src/a.py", "src/b.py"]) as mocks:
        await review_requested._run_locked(cfg, pr_url=None)
    # 2 files + 1 summary = 3 backend calls
    assert mocks.backend.return_value.run.call_count == 3


async def test_vibe_heal_context_included_in_prompts(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.get_vibe_heal_context", return_value="sonar findings"),
    ):
        await review_requested._run_locked(cfg, pr_url=None)
    # backend.run(prompt, cwd=wdir) — prompt is first positional arg
    prompts = [c.args[0] for c in mocks.backend.return_value.run.call_args_list]
    assert all("## Static Analysis" in p for p in prompts)
    assert all("sonar findings" in p for p in prompts)


def _review_requested_host(*requested: tuple[int, str]):
    """A host with open PRs; each ``(number, reviewer)`` has that reviewer requested."""
    host = make_host()
    for number, reviewer in requested:
        seed_pr(host, number, requested_reviewers=(User(reviewer),) if reviewer else ())
    return host


async def test_get_prs_lists_open_prs_requesting_the_user_sorted_by_number():
    host = _review_requested_host((9, "me"), (3, "me"), (5, "someone-else"))
    prs = await review_requested._get_prs(host.code_host(), REPO, "me")
    assert [p.number for p in prs] == [3, 9]
    assert [p.head_reference for p in prs] == ["feat-3", "feat-9"]
    assert [c.operation for c in host.calls] == ["ChangeRequests.list"]
    call = host.calls[0]
    assert call.kwargs["requested_reviewer"] == "me"
    assert call.kwargs["state"] is ChangeRequestState.OPEN
    assert host.writes == []


async def test_get_prs_skips_closed_prs():
    host = _review_requested_host((1, "me"), (2, "me"))
    update_pr(host, 2, state=ChangeRequestState.MERGED)
    prs = await review_requested._get_prs(host.code_host(), REPO, "me")
    assert [p.number for p in prs] == [1]


async def test_get_prs_empty_result_is_an_empty_list_without_warning(caplog):
    host = _review_requested_host()
    with caplog.at_level("WARNING"):
        assert await review_requested._get_prs(host.code_host(), REPO, "me") == []
    assert "Could not list" not in caplog.text


async def test_get_prs_returns_empty_on_a_failed_read(caplog):
    host = _review_requested_host((1, "me"))
    host.fail_next("ChangeRequests.list", CodeHostError("boom"))
    with caplog.at_level("WARNING"):
        assert await review_requested._get_prs(host.code_host(), REPO, "me") == []
    assert "Could not list review-requested PRs for acme/frontend" in caplog.text


async def test_get_prs_with_no_login_makes_no_host_call():
    # An empty login filter would list every open PR, not the user's.
    host = _review_requested_host((1, "me"))
    assert await review_requested._get_prs(host.code_host(), REPO, "") == []
    assert host.calls == []


async def test_get_prs_unsupported_capability_stops():
    host = make_host(capabilities=frozenset(Capability) - {Capability.CHANGE_REQUESTS})
    with pytest.raises(UnsupportedCapabilityError):
        await review_requested._get_prs(host.code_host(), REPO, "me")


async def test_run_locked_reviews_the_prs_that_request_the_user(tmp_xdg, tmp_path):
    """End to end on the fake: the login from the session picks the PRs; a PR requesting someone else is left alone."""
    _setup_knowledge(tmp_path)
    host = _review_requested_host((1, "me"), (2, "someone-else"))
    real_get_prs = review_requested._get_prs
    with (
        _full_run_mocks(prs=[]) as mocks,
        patch("dotharness.pr_review.runners.review_requested.open_host", return_value=make_scaffold(host)),
        patch("dotharness.pr_review.runners.review_requested.host_current_user", return_value="me"),
        patch("dotharness.pr_review.runners.review_requested._get_prs", wraps=real_get_prs) as get_prs,
    ):
        await review_requested._run_locked(_cfg(tmp_path), pr_url=None)
    assert get_prs.call_args.args[2] == "me"
    assert [c.args[0] for c in mocks.fetch_checkout.call_args_list] == ["feat-1"]


async def test_run_locked_single_pr_path_reads_the_change_request(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    host = make_host()
    seed_pr(host, 4)
    url = host._change_requests[REPO][0].url
    with (
        _full_run_mocks(prs=[]) as mocks,
        patch("dotharness.pr_review.runners.review_requested.open_host", return_value=make_scaffold(host)),
        patch(
            "dotharness.pr_review.runners.review_requested.pr_from_url", return_value=_pr(4, head_reference="feat-4")
        ),
    ):
        await review_requested._run_locked(_cfg(tmp_path), pr_url=url)
    assert [c.args[0] for c in mocks.fetch_checkout.call_args_list] == ["feat-4"]


async def test_run_locked_single_pr_path_skips_the_run_when_the_pr_cannot_be_read(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    with (
        _full_run_mocks(prs=[]) as mocks,
        patch("dotharness.pr_review.runners.review_requested.pr_from_url", return_value=None),
    ):
        await review_requested._run_locked(_cfg(tmp_path), pr_url="https://x/pull/4")
    mocks.fetch_checkout.assert_not_called()


async def test_has_user_approved_reads_every_review_for_an_approval_by_the_user():
    host = make_host()
    seed_pr(host, 1)
    put_review(host, "someone-else", ReviewState.APPROVED, T, 1)
    put_review(host, "me", ReviewState.COMMENTED, T, 1)
    put_review(host, None, ReviewState.APPROVED, T, 1)
    assert (await review_requested._has_user_approved(host.code_host(), REPO, 1, "me")) is False
    put_review(host, "me", ReviewState.APPROVED, T, 1)
    assert (await review_requested._has_user_approved(host.code_host(), REPO, 1, "me")) is True
    assert {c.operation for c in host.calls} == {"ChangeRequests.reviews"}


async def test_has_user_approved_is_false_on_a_failed_read():
    host = make_host()
    seed_pr(host, 1)
    put_review(host, "me", ReviewState.APPROVED, T, 1)
    host.fail_next("ChangeRequests.reviews", CodeHostError("boom"))
    assert (await review_requested._has_user_approved(host.code_host(), REPO, 1, "me")) is False


async def test_has_user_approved_unsupported_capability_stops():
    host = make_host(capabilities=frozenset(Capability) - {Capability.CHANGE_REQUESTS})
    with pytest.raises(UnsupportedCapabilityError):
        await review_requested._has_user_approved(host.code_host(), REPO, 1, "me")


async def test_vibe_heal_context_absent_when_empty(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.get_vibe_heal_context", return_value=""),
    ):
        await review_requested._run_locked(cfg, pr_url=None)
    prompts = [c.args[0] for c in mocks.backend.return_value.run.call_args_list]
    assert all("## Static Analysis" not in p for p in prompts)


async def test_design_review_invoked_when_no_marker_present(tmp_xdg, tmp_path):
    """With no persisted state, has_design_review_comment is the only idempotency
    signal — when it's absent, the design pass must actually invoke the backend."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_design_review_comment", return_value=False),
    ):
        await review_requested._run_locked(cfg, pr_url=None)
    # 1 file + 1 summary + 1 design = 3 backend calls
    assert mocks.backend.return_value.run.call_count == 3
    assert mocks.backend.return_value.run.call_args_list[-1].kwargs["context"] == "PR #1 design review"


async def test_design_review_noop_when_marker_already_present(tmp_xdg, tmp_path):
    """A design-review comment from a prior attempt makes this cycle's design step a
    noop — the backend must not be invoked a second time for it."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_design_review_comment", return_value=True),
    ):
        await review_requested._run_locked(cfg, pr_url=None)
    # 1 file + 1 summary = 2 backend calls; design is skipped entirely.
    assert mocks.backend.return_value.run.call_count == 2


async def test_continues_to_next_pr_on_backend_exception(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    prs = [_pr(1, url="u1", head_reference="feat1"), _pr(2, url="u2", head_reference="feat2")]
    with _full_run_mocks(prs=prs) as mocks:
        mocks.backend.return_value.run = AsyncMock()
        mocks.backend.return_value.run.side_effect = [RuntimeError("backend crash"), MagicMock(returncode=0)]
        await review_requested._run_locked(cfg, pr_url=None)

    restore_calls = mocks.restore.call_args_list
    assert len(restore_calls) == 2
    assert restore_calls[0][0][0] == "sha"
    assert restore_calls[0][0][1] == "feat1"
    assert restore_calls[1][0][1] == "feat2"
    assert mocks.detach.call_count == 2


async def test_restores_head_on_backend_failure(tmp_xdg, tmp_path):
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with _full_run_mocks() as mocks:
        mocks.backend.return_value.run = AsyncMock()
        mocks.backend.return_value.run.side_effect = RuntimeError("backend crash")
        await review_requested._run_locked(cfg, pr_url=None)
    mocks.restore.assert_called_once_with("sha", "feat", str(cfg.repo.working_dir), ANY)


_TICKET = LinkedTicket(3, "acme/frontend", "Do the thing", "body", "closing_keyword")


async def test_traceability_review_invoked_when_no_marker_present(tmp_xdg, tmp_path):
    """With no persisted state, has_traceability_review_comment is the only idempotency
    signal — when it's absent and a ticket resolves, the pass must actually invoke the
    backend."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_traceability_review_comment", return_value=False),
        patch("dotharness.pr_review.runners.review_requested.resolve_linked_tickets", return_value=[_TICKET]),
        patch("dotharness.pr_review.runners.review_requested.build_early_comment_context", return_value=""),
        patch(
            "dotharness.pr_review.runners.review_requested.get_traceability_review_flagged_locations", return_value=[]
        ),
    ):
        await review_requested._run_locked(cfg, pr_url=None)
    # 1 file + 1 summary + 1 traceability = 3 backend calls
    assert mocks.backend.return_value.run.call_count == 3
    assert mocks.backend.return_value.run.call_args_list[-1].kwargs["context"] == "PR #1 traceability review"
    prompt = mocks.backend.return_value.run.call_args_list[-1].args[0]
    assert "## Linked Ticket(s)" in prompt
    assert "Do the thing" in prompt


async def test_traceability_review_noop_when_marker_already_present(tmp_xdg, tmp_path):
    """A traceability-review comment from a prior attempt makes this cycle's step a
    noop — the backend must not be invoked a second time for it, and no ticket
    resolution is needed."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_traceability_review_comment", return_value=True),
    ):
        await review_requested._run_locked(cfg, pr_url=None)
    # 1 file + 1 summary = 2 backend calls; traceability is skipped entirely.
    assert mocks.backend.return_value.run.call_count == 2


async def test_traceability_review_no_linked_ticket_posts_comment_without_backend(tmp_xdg, tmp_path):
    """When resolve_linked_tickets finds nothing, the "no linked ticket" comment is
    posted directly — no backend call for this pass, since there's nothing to judge."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_traceability_review_comment", return_value=False),
        patch("dotharness.pr_review.runners.review_requested.resolve_linked_tickets", return_value=[]),
    ):
        await review_requested._run_locked(cfg, pr_url=None)
    # 1 file + 1 summary = 2 backend calls; traceability never invokes the backend.
    assert mocks.backend.return_value.run.call_count == 2
    posts = [c for c in mocks.host.writes if c.operation == "Discussion.post_comment"]
    assert len(posts) == 1
    assert posts[0].args[:2] == (REPO, 1)
    assert posts[0].args[2].startswith("# Requirement Traceability\nNo linked ticket found")
    assert posts[0].args[2].endswith(TRACEABILITY_REVIEW_MARKER)


async def test_traceability_review_no_linked_ticket_post_failure_is_not_retried(tmp_xdg, tmp_path, caplog):
    """A failed post is logged and the pass reports failure; the write is not retried."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_traceability_review_comment", return_value=False),
        patch("dotharness.pr_review.runners.review_requested.resolve_linked_tickets", return_value=[]),
        caplog.at_level("ERROR"),
    ):
        mocks.host.fail_next("Discussion.post_comment", CodeHostTimeoutError("slow", write=True), apply=True)
        await review_requested._run_locked(cfg, pr_url=None)
    attempts = [c for c in mocks.host.calls if c.operation == "Discussion.post_comment"]
    assert len(attempts) == 1
    assert "failed to post 'no linked ticket found' comment" in caplog.text


async def test_post_no_linked_ticket_comment_unsupported_capability_stops():
    host = make_host(capabilities=frozenset(Capability) - {Capability.DISCUSSION})
    with pytest.raises(UnsupportedCapabilityError):
        await post_no_linked_ticket_comment(host.code_host(), REPO, 1)


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


async def test_regret_review_not_run_when_disabled(tmp_xdg, tmp_path):
    """[regret_review].enabled is false by default — the pass is never entered at all,
    and no regret marker check is made (the enabled gate is unique to this pass;
    design/traceability have none)."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_regret_review_comment") as mock_has_regret,
        patch("dotharness.pr_review.runners.review_requested._run_regret_review") as mock_regret,
    ):
        await review_requested._run_locked(cfg, pr_url=None)
    mock_has_regret.assert_not_called()
    mock_regret.assert_not_called()
    # 1 file + 1 summary = 2 backend calls; regret is never entered.
    assert mocks.backend.return_value.run.call_count == 2


async def test_regret_review_noop_when_marker_already_present(tmp_xdg, tmp_path):
    """A regret-review comment from a prior attempt makes this cycle's regret step a
    noop — no candidate-pipeline calls and no backend call for it. This runner has no
    persisted state, so the marker check is the pass's only idempotency signal."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_regret_review_comment", return_value=True),
        patch("dotharness.pr_review.runners.review_requested.find_introducing_prs") as mock_find_prs,
        patch("dotharness.pr_review.runners.review_requested.find_regret_candidates") as mock_find_candidates,
    ):
        await review_requested._run_locked(cfg, pr_url=None)
    mock_find_prs.assert_not_called()
    mock_find_candidates.assert_not_called()
    # 1 file + 1 summary = 2 backend calls; regret is skipped entirely.
    assert mocks.backend.return_value.run.call_count == 2


async def test_regret_review_posts_comment_and_confirms_marker_on_findings(tmp_xdg, tmp_path):
    """The full pipeline: candidate search → backend verdict lines → findings parsed
    from the recorded reply → regret comment posted by the harness (the backend posts
    nothing) → marker confirmed via the plain-bool has_regret_review_comment."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        _full_run_mocks() as mocks,
        # False when the loop checks regret_done; True when the post-run confirmation
        # re-checks the marker after the harness posts.
        patch("dotharness.pr_review.runners.review_requested.has_regret_review_comment", side_effect=[False, True]),
        patch("dotharness.pr_review.runners.review_requested.find_introducing_prs", return_value=[_regret_hunk()]),
        patch(
            "dotharness.pr_review.runners.review_requested.find_regret_candidates",
            return_value=[_regret_candidate(_regret_hunk())],
        ),
        patch("dotharness.pr_review.runners.review_requested.post_regret_comment", return_value=True) as mock_post,
    ):
        # file review, then summary, then a regret-review call with verdict lines
        mocks.backend.return_value.run = AsyncMock()
        mocks.backend.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=b"CANDIDATE 1: YES - it predicted the empty-input failure\n"),
        ]
        await review_requested._run_locked(cfg, pr_url=None)
    assert mocks.backend.return_value.run.call_count == 3
    assert mocks.backend.return_value.run.call_args_list[-1].kwargs["context"] == "PR #1 regret review"
    assert mock_post.call_count == 1
    body = mock_post.call_args.args[3]
    assert "# Previously flagged review comments" in body
    assert "src/foo.py:3" in body
    assert "#42" in body
    assert "https://github.com/acme/frontend/pull/42#discussion_r123" in body
    assert "This will break when the input is empty" in body
    assert REGRET_REVIEW_MARKER in body


async def test_regret_review_no_candidates_no_backend_call(tmp_xdg, tmp_path):
    """Blame resolved an introducing PR but no comment matched the blamed hunk — there
    is structurally nothing to judge: no backend call for this pass, no comment posted.
    Nothing is persisted in this stateless runner, so a later run of this PR simply
    gets a fresh candidate search."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_regret_review_comment", return_value=False),
        patch("dotharness.pr_review.runners.review_requested.find_introducing_prs", return_value=[_regret_hunk()]),
        patch("dotharness.pr_review.runners.review_requested.find_regret_candidates", return_value=[]),
        patch("dotharness.pr_review.runners.review_requested.post_regret_comment") as mock_post,
    ):
        await review_requested._run_locked(cfg, pr_url=None)
    # 1 file + 1 summary = 2 backend calls; regret never invokes the backend.
    assert mocks.backend.return_value.run.call_count == 2
    mock_post.assert_not_called()


async def test_regret_review_no_findings_counts_complete_without_posting(tmp_xdg, tmp_path):
    """The backend judged every candidate NO — nothing is posted, but the run still
    counts as complete: there is nothing to post. Unlike self_review.py, nothing is
    persisted here, so a later run simply re-judges the candidates."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_regret_review_comment", return_value=False),
        patch("dotharness.pr_review.runners.review_requested.find_introducing_prs", return_value=[_regret_hunk()]),
        patch(
            "dotharness.pr_review.runners.review_requested.find_regret_candidates",
            return_value=[_regret_candidate(_regret_hunk())],
        ),
        patch("dotharness.pr_review.runners.review_requested.post_regret_comment") as mock_post,
    ):
        mocks.backend.return_value.run = AsyncMock()
        mocks.backend.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=b"CANDIDATE 1: NO - it was about a different edge case\n"),
        ]
        await review_requested._run_locked(cfg, pr_url=None)
    assert mocks.backend.return_value.run.call_count == 3
    mock_post.assert_not_called()


async def test_regret_review_writes_no_persisted_state(tmp_xdg, tmp_path):
    """This runner is deliberately stateless (like its design/traceability passes): a
    regret run that posts a comment must not create self-review's state file or add
    the PR to regret_reviewed_prs the way self_review.py's wiring does."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    cfg.regret_review.enabled = True
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_regret_review_comment", side_effect=[False, True]),
        patch("dotharness.pr_review.runners.review_requested.find_introducing_prs", return_value=[_regret_hunk()]),
        patch(
            "dotharness.pr_review.runners.review_requested.find_regret_candidates",
            return_value=[_regret_candidate(_regret_hunk())],
        ),
        patch("dotharness.pr_review.runners.review_requested.post_regret_comment", return_value=True),
    ):
        mocks.backend.return_value.run = AsyncMock()
        mocks.backend.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=b"CANDIDATE 1: YES - it predicted the empty-input failure\n"),
        ]
        await review_requested._run_locked(cfg, pr_url=None)
    assert not (tmp_xdg / "state" / "acme-frontend" / "self_review.json").exists()


async def test_re_requests_review_when_incomplete_and_previously_requested(tmp_xdg, tmp_path):
    """Submitting a review via the GitHub API clears the submitter from the PR's
    requested-reviewers list. If the design pass fails but the user was a requested
    reviewer to begin with, the runner must re-add them so the PR stays in the
    "user-review-requested:@me" inbox other loops (or a later run of this one) rely
    on — mirrors review_prs.py."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_design_review_comment", return_value=False),
    ):
        mocks.get_requested.return_value = True
        # file review, then summary, then a failing design-review call
        mocks.backend.return_value.run = AsyncMock()
        mocks.backend.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=1),
        ]
        await review_requested._run_locked(cfg, pr_url=None)
    mocks.add_reviewer.assert_called_once_with(ANY, REPO, 1, "bot")


async def test_does_not_re_request_review_if_not_previously_requested(tmp_xdg, tmp_path):
    """The re-add only fires if the user was already a requested reviewer — it must
    not add a reviewer who was never on the PR to begin with."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with (
        _full_run_mocks() as mocks,
        patch("dotharness.pr_review.runners.review_requested.has_design_review_comment", return_value=False),
    ):
        mocks.get_requested.return_value = False
        mocks.backend.return_value.run = AsyncMock()
        mocks.backend.return_value.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0),
            MagicMock(returncode=1),
        ]
        await review_requested._run_locked(cfg, pr_url=None)
    mocks.add_reviewer.assert_not_called()


async def test_re_requests_review_even_when_fully_done(tmp_xdg, tmp_path):
    """This runner never concludes the review on the human's behalf — even when every
    pass succeeds, the user must still be re-added as a requested reviewer if they held
    that status going in. Regression test for the old behavior, where a fully successful
    run would instead call remove_reviewer and skip the re-add."""
    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    with _full_run_mocks() as mocks:
        mocks.get_requested.return_value = True
        await review_requested._run_locked(cfg, pr_url=None)
    mocks.add_reviewer.assert_called_once_with(ANY, REPO, 1, "bot")


async def test_fatal_git_error_stops_loop_and_restores(tmp_xdg, tmp_path):
    """FatalGitError must propagate out of _process_pr (not be swallowed by the generic
    handler) so _run_locked aborts the loop; git_restore still runs via finally."""
    from dotharness.pr_review.runners.common import FatalGitError

    _setup_knowledge(tmp_path)
    cfg = _cfg(tmp_path)
    prs = [_pr(n, url=f"u{n}", head_reference=f"b{n}") for n in (1, 2, 3)]
    with _full_run_mocks(prs=prs) as mocks:
        mocks.fetch_checkout.side_effect = FatalGitError("boom")
        await review_requested._run_locked(cfg, pr_url=None)
    assert mocks.fetch_checkout.call_count == 1
    mocks.restore.assert_called_once()


async def test_process_pr_skips_summary_when_file_reviews_fail(tmp_path):
    """A failed file review must not be followed by the summary: the summary comment marks
    the PR "done" in _should_skip_pr, which would permanently suppress the retry."""
    mod = "dotharness.pr_review.runners.review_requested"
    pr = _pr(7, head_reference="feat")
    with (
        patch(f"{mod}.git_detach_and_record", return_value="sha"),
        patch(f"{mod}.git_restore"),
        patch(f"{mod}.git_fetch_and_checkout"),
        patch(f"{mod}.preserve_reviewer_request", return_value=contextlib.nullcontext()),
        patch(f"{mod}._gather_pr_context", return_value={}),
        patch(f"{mod}._run_file_reviews", return_value=False),
        patch(f"{mod}._run_summary_review") as summary,
        patch(f"{mod}._run_design_review"),
        patch(f"{mod}._run_traceability_review"),
    ):
        await review_requested._process_pr(
            pr,
            _cfg(tmp_path),
            MagicMock(),
            REPO,
            tmp_path,
            None,
            MagicMock(),
            str(tmp_path),
            {},
            "me",
            True,
            review_requested.PrLevelPassesDone(design=True, traceability=True, regret=True),
        )
    summary.assert_not_called()
