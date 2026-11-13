"""Approval safety: the state, approval and CI verdicts of ``check_approval_safety`` through the in-memory host.

How the CI checks themselves are read and judged (latest run, stack root, workflows) is tested in
``test_ci_checks.py``; here only how that verdict maps onto a ``SafetyResult``.
"""

import logging

import pytest

from dotharness.code_host import (
    Capability,
    ChangeRequestState,
    CheckStatus,
    CodeHostError,
    MergeableState,
    ReviewState,
    UnsupportedCapabilityError,
)
from dotharness.pr_review.runners.auto_approve_github import (
    REASON_ALREADY_APPROVED,
    SafetyStatus,
    check_approval_safety,
)
from dotharness.pr_review.runners.ci_checks import (
    CI_BLOCKED,
    REASON_AUTO_MERGE,
    REASON_CONFLICTING,
    REASON_DRAFT,
    REASON_MERGEABLE_UNKNOWN,
    REASON_NOT_OPEN,
)
from tests.pr_review.runners.approve_support import (
    HEAD,
    ME,
    NUMBER,
    REPO,
    make_host,
    put_approval,
    seed_pr,
    update_pr,
)


async def run(host=None, *, user=ME, setup=None, **pr_fields):
    host = host or make_host()
    if not host._change_requests:
        seed_pr(host, **pr_fields)
    if setup:
        setup(host)
    result = await check_approval_safety(host.code_host(), REPO, NUMBER, user)
    assert host.writes == []
    return result, host


def check(host, status=CheckStatus.PASS, name="build"):
    host.add_check(REPO, NUMBER, name=name, status=status, required=True)


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"mergeable": MergeableState.CONFLICTING}, REASON_CONFLICTING),
        ({"mergeable": MergeableState.UNKNOWN}, REASON_MERGEABLE_UNKNOWN),
        ({"mergeable": None}, REASON_MERGEABLE_UNKNOWN),
        ({"auto_merge_enabled": True}, REASON_AUTO_MERGE),
        ({"is_draft": True}, REASON_DRAFT),
        ({"state": ChangeRequestState.MERGED}, REASON_NOT_OPEN),
        ({"state": ChangeRequestState.CLOSED}, REASON_NOT_OPEN),
    ],
)
async def test_state_skips_carry_the_head(fields, reason):
    result, _ = await run(**fields)
    assert (result.status, result.reason, result.head_sha) == (SafetyStatus.SKIP, reason, HEAD)


async def test_state_skip_makes_no_review_or_check_read():
    _, host = await run(is_draft=True)
    assert [c.operation for c in host.calls] == ["ChangeRequests.get"]


async def test_safe_returns_the_head_sha_and_reads_in_order():
    host = make_host()
    seed_pr(host)
    check(host)
    result, host = await run(host)
    assert (result.status, result.head_sha) == (SafetyStatus.SAFE, HEAD)
    ops = [c.operation for c in host.calls]
    assert ops[:2] == ["ChangeRequests.get", "ChangeRequests.reviews"]
    assert "Checks.list_checks" in ops


async def test_any_approval_by_the_acting_account_skips_without_reading_checks():
    host = make_host()
    seed_pr(host)
    put_approval(host, revision="old")  # an older commit counts too (FR-6.4, D-14)
    result, host = await run(host)
    assert (result.status, result.reason, result.head_sha) == (SafetyStatus.SKIP, REASON_ALREADY_APPROVED, HEAD)
    assert not [c for c in host.calls if c.operation.startswith("Checks.")]


async def test_other_user_or_dismissed_approval_does_not_skip():
    host = make_host()
    seed_pr(host)
    put_approval(host, "other")
    put_approval(host, state=ReviewState.DISMISSED)
    assert (await run(host))[0].status is SafetyStatus.SAFE


async def test_approval_login_case_insensitive():
    host = make_host()
    seed_pr(host)
    put_approval(host, "Reviewer")
    assert (await run(host, user="REVIEWER"))[0].status is SafetyStatus.SKIP


@pytest.mark.parametrize("status", [CheckStatus.FAIL, CheckStatus.PENDING, CheckStatus.CANCEL])
async def test_blocking_ci_skips_with_the_head(status):
    host = make_host()
    seed_pr(host)
    check(host)
    check(host, status, "other")
    result, _ = await run(host)
    assert (result.status, result.reason, result.head_sha) == (SafetyStatus.SKIP, CI_BLOCKED, HEAD)
    assert CI_BLOCKED == "ci-blocked"


async def test_passing_and_skipping_checks_are_safe():
    host = make_host()
    seed_pr(host)
    check(host)
    check(host, CheckStatus.SKIPPING, "optional")
    assert (await run(host))[0].status is SafetyStatus.SAFE


async def test_ci_read_failure_is_read_failed_with_the_head():
    result, _ = await run(setup=lambda h: h.fail_next("Checks.list_checks", CodeHostError("boom")))
    assert (result.status, result.reason, result.head_sha) == (SafetyStatus.READ_FAILED, "checks read failed", HEAD)


async def test_no_checks_and_unreadable_workflows_is_read_failed():
    result, _ = await run(setup=lambda h: h.fail_next("Checks.list_workflows", CodeHostError("boom")))
    assert (result.status, result.reason) == (SafetyStatus.READ_FAILED, "workflows read failed")


async def test_no_checks_but_workflows_skips():
    result, _ = await run(setup=lambda h: h.add_workflow(REPO, name="CI"))
    assert (result.status, result.reason) == (SafetyStatus.SKIP, CI_BLOCKED)


async def test_no_checks_and_no_workflows_is_safe():
    assert (await run())[0].status is SafetyStatus.SAFE


async def test_failing_checks_are_named_in_the_log(caplog):
    host = make_host()
    seed_pr(host)
    check(host, CheckStatus.PASS, "lint")
    check(host, CheckStatus.FAIL, "unit")
    check(host, CheckStatus.PENDING, "e2e")
    with caplog.at_level(logging.INFO):
        result, _ = await run(host)
    assert result.reason == CI_BLOCKED
    assert "failing checks: unit(fail)" in caplog.text
    assert "lint" not in caplog.text and "e2e" not in caplog.text  # passing and pending are not named


async def test_state_read_failure_is_read_failed_without_a_head():
    result, _ = await run(setup=lambda h: h.fail_next("ChangeRequests.get", CodeHostError("boom")))
    assert (result.status, result.head_sha) == (SafetyStatus.READ_FAILED, "")


async def test_change_request_without_a_head_revision_is_read_failed():
    host = make_host()
    seed_pr(host)
    update_pr(host, NUMBER, head_revision="")
    result, _ = await run(host)
    assert (result.status, result.head_sha) == (SafetyStatus.READ_FAILED, "")


async def test_reviews_read_failure_is_read_failed_with_the_head():
    result, _ = await run(setup=lambda h: h.fail_next("ChangeRequests.reviews", CodeHostError("boom")))
    assert (result.status, result.head_sha) == (SafetyStatus.READ_FAILED, HEAD)


async def test_unknown_acting_account_is_read_failed_without_any_read():
    result, host = await run(user="")
    assert result.status is SafetyStatus.READ_FAILED and host.calls == []


async def test_raised_os_error_is_read_failed(monkeypatch):
    host = make_host()
    seed_pr(host)
    ch = host.code_host()

    async def raiser(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(ch.change_requests, "get", raiser)
    assert (await check_approval_safety(ch, REPO, NUMBER, ME)).status is SafetyStatus.READ_FAILED


@pytest.mark.parametrize("capability", [Capability.CHANGE_REQUESTS, Capability.CHECKS])
async def test_unsupported_capability_stops(capability):
    host = make_host(capabilities=frozenset(Capability) - {capability})
    host.add_change_request(REPO, head_reference="f", number=NUMBER)
    if capability is Capability.CHECKS:
        update_pr(host, NUMBER, mergeable=MergeableState.MERGEABLE)
    with pytest.raises(UnsupportedCapabilityError) as exc:
        await check_approval_safety(host.code_host(), REPO, NUMBER, ME)
    assert exc.value.capability == capability.value
