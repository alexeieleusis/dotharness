import pytest

from dotharness.code_host import (
    Capability,
    CodeHostError,
    CodeHostTimeoutError,
    ReviewState,
    UnsupportedCapabilityError,
)
from dotharness.pr_review.runners.auto_approve_github import (
    ApprovalStatus,
    GateResult,
    GateStatus,
    SafetyResult,
    SafetyStatus,
    approve_pr,
)
from dotharness.pr_review.runners.auto_approve_size import SizeEvaluation
from tests.pr_review.runners.approve_support import HEAD, ME, NUMBER, REPO, make_host, put_approval, seed_pr


async def run(
    host=None,
    *,
    total=100,
    gate=True,
    safe=True,
    sha=HEAD,
    user=ME,
    dry_run=False,
    all_viewed=False,
    setup=None,
):
    host = host or make_host()
    if not host._change_requests:
        seed_pr(host)
    if setup:
        setup(host)
    gate_result = GateResult(GateStatus.PASSED if gate else GateStatus.NOT_PASSED, "")
    safety = SafetyResult(SafetyStatus.SAFE if safe else SafetyStatus.SKIP, "", sha)
    size = SizeEvaluation(file_counts={"a.py": total}, total_lines=total, small_files=[], pr_is_small=False)
    result = await approve_pr(
        host.code_host(), REPO, NUMBER, gate_result, safety, size, 500, user, dry_run, all_viewed=all_viewed
    )
    return result, host


def approvals(host):
    return [c for c in host.calls if c.operation == "ChangeRequests.approve"]  # attempts, applied or not


async def test_approves_at_the_head_revision():
    result, host = await run()
    assert result.status is ApprovalStatus.APPROVED
    (call,) = approvals(host)
    assert call.args == (REPO, NUMBER)
    assert call.kwargs == {"head_revision": HEAD}
    assert [(r.author.login, r.state, r.revision) for r in host.reviews[REPO][NUMBER]] == [
        (ME, ReviewState.APPROVED, HEAD)
    ]


@pytest.mark.parametrize("kwargs", [{"gate": False}, {"safe": False}, {"sha": ""}, {"total": 500}])
async def test_precheck_failure_makes_no_call(kwargs):
    result, host = await run(**kwargs)
    assert result.status is ApprovalStatus.SKIPPED and host.calls == []


async def test_size_threshold_is_strict():
    assert (await run(total=499))[0].status is ApprovalStatus.APPROVED


async def test_already_approved_on_head_skips():
    host = make_host()
    seed_pr(host)
    put_approval(host)
    result, host = await run(host)
    assert result.status is ApprovalStatus.SKIPPED and approvals(host) == []


async def test_approval_on_older_commit_skips():
    # FR-6.4 / D-14: any APPROVED review by the acting account counts; re-approval after a push is manual.
    host = make_host()
    seed_pr(host)
    put_approval(host, revision="old")
    result, host = await run(host)
    assert result.status is ApprovalStatus.SKIPPED and result.reason == "already-approved"
    assert approvals(host) == []


async def test_other_user_or_dismissed_approval_does_not_skip():
    host = make_host()
    seed_pr(host)
    put_approval(host, "someone")
    put_approval(host, state=ReviewState.DISMISSED)
    result, host = await run(host)
    assert result.status is ApprovalStatus.APPROVED and len(approvals(host)) == 1


async def test_reviews_read_failure_makes_no_write():
    # A plain boolean read would return False here; the tri-state helper must not approve.
    result, host = await run(setup=lambda h: h.fail_next("ChangeRequests.reviews", CodeHostError("boom")))
    assert result.status is ApprovalStatus.READ_FAILED and approvals(host) == []


async def test_pr_state_read_failure_makes_no_write():
    result, host = await run(setup=lambda h: h.fail_next("ChangeRequests.get", CodeHostError("boom")))
    assert result.status is ApprovalStatus.READ_FAILED and approvals(host) == []


async def test_head_moved_makes_no_write():
    result, host = await run(sha="older")
    assert result.status is ApprovalStatus.SKIPPED and result.reason == "head-moved"
    assert approvals(host) == []


async def test_unknown_acting_account_makes_no_write():
    result, host = await run(user="")
    assert result.status is ApprovalStatus.READ_FAILED and host.calls == []


@pytest.mark.parametrize("error", [CodeHostError("boom"), CodeHostTimeoutError("slow", write=True)])
@pytest.mark.parametrize("apply", [False, True])
async def test_write_failure_is_reported_and_never_retried(error, apply):
    # apply=True: the approval took effect although the call timed out. The next run sees it as already approved.
    result, host = await run(setup=lambda h: h.fail_next("ChangeRequests.approve", error, apply=apply))
    assert result.status is ApprovalStatus.WRITE_FAILED
    assert len(approvals(host)) == 1  # one attempt, no retry and no fallback write
    assert [c.operation for c in host.calls].count("ChangeRequests.approve") == 1
    assert len(host.writes) == (1 if apply else 0)
    again, _ = await run(host)
    assert again.status is (ApprovalStatus.SKIPPED if apply else ApprovalStatus.APPROVED)


async def test_dry_run_reads_but_never_writes():
    result, host = await run(dry_run=True)
    assert result.status is ApprovalStatus.DRY_RUN
    assert host.writes == []
    assert [c.operation for c in host.calls] == ["ChangeRequests.reviews", "ChangeRequests.get"]


async def test_too_large_skipped_unless_all_viewed():
    result, host = await run(total=900)
    assert result.status is ApprovalStatus.SKIPPED and host.calls == []
    result, host = await run(total=900, all_viewed=True)
    assert result.status is ApprovalStatus.APPROVED and len(approvals(host)) == 1


async def test_all_viewed_does_not_waive_gate_or_safety():
    assert (await run(all_viewed=True, gate=False))[0].status is ApprovalStatus.SKIPPED
    assert (await run(all_viewed=True, safe=False))[0].status is ApprovalStatus.SKIPPED


async def test_unsupported_approve_stops():
    host = make_host(capabilities=frozenset(Capability) - {Capability.CHANGE_REQUESTS})
    seed_pr(host)
    with pytest.raises(UnsupportedCapabilityError):
        await run(host)


async def test_unsupported_error_at_the_write_stops_and_writes_nothing():
    host = make_host()
    seed_pr(host)
    host.fail_next("ChangeRequests.approve", UnsupportedCapabilityError("no", capability="ChangeRequests"))
    with pytest.raises(UnsupportedCapabilityError):
        await run(host)
    assert host.writes == []
