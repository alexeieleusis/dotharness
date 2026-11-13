"""Host-based check readers in ``ci_checks``, driven through the in-memory host."""

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from dotharness.code_host import (
    Capability,
    Check,
    CheckStatus,
    CodeHostError,
    CodeHostTimeoutError,
    MergeableState,
    UnsupportedCapabilityError,
)
from dotharness.code_host.models import ChangeRequestState
from dotharness.code_host.testing import InMemoryCodeHost
from dotharness.pr_review.runners import ci_checks
from dotharness.pr_review.runners.ci_checks import (
    CI_BLOCKED,
    UNNAMED_CHECK,
    change_request_skip_reason,
    ci_blocker,
    host_has_workflows,
    latest_checks,
    list_own_open_change_requests,
    read_change_request,
    read_host_checks,
)

T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


@pytest.fixture
def fake() -> InMemoryCodeHost:
    return InMemoryCodeHost(repository="acme/frontend", user="me")


@pytest.fixture
def repo(fake):
    return fake.add_repository("acme/frontend")


def _check(name="t", status=CheckStatus.PASS, workflow=None, started=None, completed=None, job_id=None):
    return Check(name, status, None, workflow, started, completed, job_id)


async def _read(*args, **kwargs):
    result = await read_host_checks(*args, **kwargs)
    assert result is not None
    return result


def _names(checks):
    return [c.name for c in checks]


# -- latest_checks ---------------------------------------------------------------------------------------------------


def test_single_run_needs_no_timestamps():
    only = _check()
    assert latest_checks([only]) == [only]


def test_newest_completed_run_wins():
    old = _check(status=CheckStatus.FAIL, started=T0, completed=T0 + timedelta(minutes=5))
    new = _check(status=CheckStatus.PASS, started=T0 + timedelta(hours=1), completed=T0 + timedelta(hours=1, minutes=5))
    assert latest_checks([old, new]) == [new]
    assert latest_checks([new, old]) == [new]


def test_latest_run_without_completion_is_pending_whatever_its_status():
    old = _check(status=CheckStatus.PASS, started=T0, completed=T0 + timedelta(minutes=5))
    running = _check(status=CheckStatus.PASS, started=T0 + timedelta(hours=1), completed=None)
    assert latest_checks([old, running]) == [replace(running, status=CheckStatus.PENDING)]


def test_run_with_no_usable_timestamp_makes_the_read_undecidable():
    assert latest_checks([_check(started=T0, completed=T0), _check()]) is None


def test_tied_runs_are_all_kept():
    a = _check(status=CheckStatus.PASS, started=T0, completed=T0)
    b = _check(status=CheckStatus.FAIL, started=T0, completed=T0)
    assert latest_checks([a, b]) == [a, b]


def test_naive_timestamps_are_read_as_utc():
    naive = _check(status=CheckStatus.FAIL, started=T0.replace(tzinfo=None), completed=T0.replace(tzinfo=None))
    later = _check(status=CheckStatus.PASS, started=T0 + timedelta(hours=1), completed=T0 + timedelta(hours=1))
    assert latest_checks([naive, later]) == [later]


def test_workflow_name_is_part_of_the_key():
    a = _check(workflow="A", status=CheckStatus.FAIL)
    b = _check(workflow="B", status=CheckStatus.PASS)
    assert latest_checks([a, b]) == [a, b]


def test_empty_name_is_shown_as_unnamed():
    assert _names(latest_checks([_check(name="")])) == [UNNAMED_CHECK]


# -- read_host_checks ------------------------------------------------------------------------------------------------


async def test_required_checks_are_returned_without_default_branch_lookup_when_not_stack_root(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat")
    fake.add_check(repo, cr.number, name="req", status=CheckStatus.PASS, required=True)
    fake.add_check(repo, cr.number, name="other", status=CheckStatus.FAIL)
    read = await _read(fake.code_host(), repo, cr.number, "main", stack_root=False)
    assert _names(read.checks) == ["req"]
    assert read.missing == []
    assert "Repository.default_branch" not in [c.operation for c in fake.calls]
    assert fake.writes == []


async def test_no_required_checks_without_stack_root_returns_all_checks(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat")
    fake.add_check(repo, cr.number, name="a", status=CheckStatus.PASS)
    fake.add_check(repo, cr.number, name="b", status=CheckStatus.FAIL)
    read = await _read(fake.code_host(), repo, cr.number, "", stack_root=False)
    assert _names(read.checks) == ["a", "b"]
    assert "ChangeRequests.find_by_branch" not in [c.operation for c in fake.calls]


async def test_superseded_failed_run_does_not_block(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat")
    fake.add_check(repo, cr.number, name="t", status=CheckStatus.FAIL, required=True)
    fake.add_check(repo, cr.number, name="t", status=CheckStatus.PASS, required=True)
    read = await _read(fake.code_host(), repo, cr.number, "main")
    assert [c.status for c in read.checks] == [CheckStatus.PASS]


async def test_stack_root_required_names_apply_to_a_stacked_pr(fake, repo, caplog):
    root = fake.add_change_request(repo, head_reference="stack-a", base_reference="main")
    fake.add_check(repo, root.number, name="build", status=CheckStatus.PASS, required=True)
    fake.add_check(repo, root.number, name="lint", status=CheckStatus.PASS, required=True)
    top = fake.add_change_request(repo, head_reference="stack-b", base_reference="stack-a")
    fake.add_check(repo, top.number, name="build", status=CheckStatus.FAIL)
    fake.add_check(repo, top.number, name="noise", status=CheckStatus.FAIL)
    with caplog.at_level(logging.INFO):
        read = await _read(fake.code_host(), repo, top.number, "stack-a")
    assert _names(read.checks) == ["build"]
    assert read.missing == ["lint"]
    assert "required checks from stack root" in caplog.text


async def test_stack_walk_goes_through_several_prs(fake, repo):
    first = fake.add_change_request(repo, head_reference="s1", base_reference="main")
    fake.add_check(repo, first.number, name="build", status=CheckStatus.PASS, required=True)
    fake.add_change_request(repo, head_reference="s2", base_reference="s1")
    top = fake.add_change_request(repo, head_reference="s3", base_reference="s2")
    fake.add_check(repo, top.number, name="build", status=CheckStatus.PASS)
    read = await _read(fake.code_host(), repo, top.number, "s2")
    assert _names(read.checks) == ["build"]
    assert read.missing == []


async def test_no_stack_returns_all_checks(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat", base_reference="unprotected")
    fake.add_check(repo, cr.number, name="a", status=CheckStatus.PASS)
    read = await _read(fake.code_host(), repo, cr.number, "unprotected")
    assert _names(read.checks) == ["a"]
    assert read.missing == []


async def test_stack_root_without_required_checks_returns_all_checks(fake, repo):
    fake.add_change_request(repo, head_reference="stack-a", base_reference="main")
    top = fake.add_change_request(repo, head_reference="stack-b", base_reference="stack-a")
    fake.add_check(repo, top.number, name="a", status=CheckStatus.PASS)
    read = await _read(fake.code_host(), repo, top.number, "stack-a")
    assert _names(read.checks) == ["a"]
    assert read.missing == []


async def test_two_open_prs_for_one_head_name_fail_closed(fake, repo, caplog):
    fake.add_change_request(repo, head_reference="stack-a", base_reference="main")
    fake.add_change_request(repo, head_reference="stack-a", base_reference="main")
    top = fake.add_change_request(repo, head_reference="stack-b", base_reference="stack-a")
    with caplog.at_level(logging.ERROR):
        assert await read_host_checks(fake.code_host(), repo, top.number, "stack-a") is None
    assert "ambiguous" in caplog.text


async def test_stack_deeper_than_the_limit_fails_closed(fake, repo, caplog):
    depth = ci_checks._MAX_STACK_DEPTH + 2
    for i in range(depth):
        fake.add_change_request(repo, head_reference=f"s{i}", base_reference=f"s{i + 1}" if i + 1 < depth else "main")
    top = fake.add_change_request(repo, head_reference="top", base_reference="s0")
    with caplog.at_level(logging.ERROR):
        assert await read_host_checks(fake.code_host(), repo, top.number, "s0") is None
    assert "stack deeper" in caplog.text


@pytest.mark.parametrize(
    "operation",
    [
        "Repository.default_branch",
        "Checks.list_checks",
        "ChangeRequests.find_by_branch",
    ],
)
async def test_read_failure_fails_closed(fake, repo, operation):
    fake.add_change_request(repo, head_reference="stack-a", base_reference="main")
    top = fake.add_change_request(repo, head_reference="stack-b", base_reference="stack-a")
    fake.fail_next(operation, CodeHostError("boom"))
    assert await read_host_checks(fake.code_host(), repo, top.number, "stack-a") is None


async def test_read_timeout_fails_closed(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat")
    fake.fail_next("Checks.list_checks", CodeHostTimeoutError("slow", write=False))
    assert await read_host_checks(fake.code_host(), repo, cr.number, "main") is None


async def test_root_required_read_failure_fails_closed(fake, repo):
    root = fake.add_change_request(repo, head_reference="stack-a", base_reference="main")
    top = fake.add_change_request(repo, head_reference="stack-b", base_reference="stack-a")
    host = fake.code_host()
    real = host.checks.list_checks

    async def flaky(ref, number, *, required_only=False):
        if number == root.number:
            msg = "root read failed"
            raise CodeHostError(msg)
        return await real(ref, number, required_only=required_only)

    host.checks.list_checks = flaky
    assert await read_host_checks(host, repo, top.number, "stack-a") is None


async def test_unsupported_checks_capability_stops_instead_of_failing_closed(fake, repo):
    host = InMemoryCodeHost(repository="acme/frontend", capabilities=frozenset(Capability) - {Capability.CHECKS})
    ref = host.add_repository("acme/frontend")
    cr = host.add_change_request(ref, head_reference="feat")
    with pytest.raises(UnsupportedCapabilityError) as exc:
        await read_host_checks(host.code_host(), ref, cr.number, "main")
    assert exc.value.capability == "Checks"


async def test_unsupported_error_injected_in_stack_walk_stops(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat", base_reference="other")
    fake.fail_next("ChangeRequests.find_by_branch", UnsupportedCapabilityError("no", capability="ChangeRequests"))
    with pytest.raises(UnsupportedCapabilityError):
        await read_host_checks(fake.code_host(), repo, cr.number, "other")


# -- host_has_workflows ----------------------------------------------------------------------------------------------


async def test_workflows_present_and_absent(fake, repo):
    host = fake.code_host()
    assert await host_has_workflows(host, repo) is False
    fake.add_workflow(repo, name="CI")
    assert await host_has_workflows(host, repo) is True


async def test_workflows_read_failure_is_none(fake, repo):
    fake.fail_next("Checks.list_workflows", CodeHostError("boom"))
    assert await host_has_workflows(fake.code_host(), repo) is None


async def test_workflows_unsupported_degrades_to_unknown(fake, repo, caplog):
    fake.fail_next("Checks.list_workflows", UnsupportedCapabilityError("no", capability="Checks"))
    with caplog.at_level(logging.WARNING):
        assert await host_has_workflows(fake.code_host(), repo) is None
    assert caplog.text == ""  # degrade is silent


# -- ci_blocker ------------------------------------------------------------------------------------------------------


async def test_green_required_checks_do_not_block(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat")
    fake.add_check(repo, cr.number, name="a", status=CheckStatus.PASS, required=True)
    fake.add_check(repo, cr.number, name="b", status=CheckStatus.SKIPPING, required=True)
    assert await ci_blocker(fake.code_host(), repo, cr.number, "main") is None


@pytest.mark.parametrize("status", [CheckStatus.FAIL, CheckStatus.PENDING, CheckStatus.CANCEL])
async def test_non_passing_status_blocks(fake, repo, status, caplog):
    cr = fake.add_change_request(repo, head_reference="feat")
    fake.add_check(repo, cr.number, name="a", status=status, required=True)
    with caplog.at_level(logging.INFO):
        block = await ci_blocker(fake.code_host(), repo, cr.number, "main")
    assert block is not None
    assert (block.read_failed, block.reason) == (False, CI_BLOCKED)
    assert (f"failing checks: a({status.value})" in caplog.text) == (status is CheckStatus.FAIL)


async def test_failing_checks_naming_is_capped(fake, repo, caplog):
    cr = fake.add_change_request(repo, head_reference="feat")
    for i in range(ci_checks._MAX_NAMED_CHECKS + 3):
        fake.add_check(repo, cr.number, name=f"c{i:02d}", status=CheckStatus.FAIL, required=True)
    with caplog.at_level(logging.INFO):
        await ci_blocker(fake.code_host(), repo, cr.number, "main")
    assert "(+3 more omitted)" in caplog.text


async def test_required_check_with_no_run_blocks(fake, repo):
    root = fake.add_change_request(repo, head_reference="stack-a", base_reference="main")
    fake.add_check(repo, root.number, name="lint", status=CheckStatus.PASS, required=True)
    top = fake.add_change_request(repo, head_reference="stack-b", base_reference="stack-a")
    block = await ci_blocker(fake.code_host(), repo, top.number, "stack-a")
    assert block is not None
    assert block.reason == CI_BLOCKED


async def test_unreadable_checks_are_a_read_failure(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat")
    fake.fail_next("Checks.list_checks", CodeHostError("boom"))
    block = await ci_blocker(fake.code_host(), repo, cr.number, "main")
    assert block is not None
    assert block.read_failed


async def test_no_checks_and_no_workflows_does_not_block(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat")
    assert await ci_blocker(fake.code_host(), repo, cr.number, "main") is None


async def test_no_checks_but_workflows_is_pending(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat")
    fake.add_workflow(repo, name="CI")
    block = await ci_blocker(fake.code_host(), repo, cr.number, "main")
    assert block is not None
    assert (block.read_failed, block.reason) == (False, CI_BLOCKED)


async def test_no_checks_and_unreadable_workflows_is_a_read_failure(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat")
    fake.fail_next("Checks.list_workflows", CodeHostError("boom"))
    block = await ci_blocker(fake.code_host(), repo, cr.number, "main")
    assert block is not None
    assert block.read_failed


# -- change request helpers ------------------------------------------------------------------------------------------


def _with(fake, repo, number, **fields):
    items = fake._change_requests[repo]
    for i, item in enumerate(items):
        if item.number == number:
            items[i] = replace(item, **fields)
    fake._version += 1


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"mergeable": MergeableState.MERGEABLE}, None),
        ({"state": ChangeRequestState.CLOSED, "mergeable": MergeableState.MERGEABLE}, "not-open"),
        ({"state": ChangeRequestState.MERGED}, "not-open"),
        ({"is_draft": True, "mergeable": MergeableState.MERGEABLE}, "draft"),
        ({"mergeable": MergeableState.CONFLICTING}, "conflicting"),
        ({"mergeable": MergeableState.UNKNOWN}, "mergeable-unknown"),
        ({"mergeable": None}, "mergeable-unknown"),
        ({"mergeable": MergeableState.MERGEABLE, "auto_merge_enabled": True}, "auto-merge"),
    ],
)
def test_change_request_skip_reason(fake, repo, fields, reason):
    cr = fake.add_change_request(repo, head_reference="feat")
    assert change_request_skip_reason(replace(cr, **fields)) == reason


async def test_read_change_request_returns_it_or_none(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat")
    host = fake.code_host()
    found = await read_change_request(host, repo, cr.number, "merge")
    assert found is not None
    assert found.number == cr.number
    fake.fail_next("ChangeRequests.get", CodeHostError("boom"))
    assert await read_change_request(host, repo, cr.number, "merge") is None
    assert await read_change_request(host, repo, 999, "merge") is None


async def test_read_change_request_unsupported_stops(fake, repo):
    cr = fake.add_change_request(repo, head_reference="feat")
    fake.fail_next("ChangeRequests.get", UnsupportedCapabilityError("no", capability="ChangeRequests"))
    with pytest.raises(UnsupportedCapabilityError):
        await read_change_request(fake.code_host(), repo, cr.number, "merge")


async def test_read_change_request_unsupported_warns_at_a_warn_site(fake, repo, caplog):
    cr = fake.add_change_request(repo, head_reference="feat")
    fake.fail_next("ChangeRequests.get", UnsupportedCapabilityError("no", capability="ChangeRequests"))
    with caplog.at_level(logging.WARNING):
        assert await read_change_request(fake.code_host(), repo, cr.number, "reviewer_request") is None
    assert "ChangeRequests" in caplog.text


async def test_list_own_open_change_requests_filters_author_and_sorts(fake, repo):
    fake.add_change_request(repo, head_reference="b", author="me", number=5)
    fake.add_change_request(repo, head_reference="a", author="me", number=2)
    fake.add_change_request(repo, head_reference="c", author="other", number=3)
    fake.add_change_request(repo, head_reference="d", author="me", number=4, state=ChangeRequestState.CLOSED)
    failures: list[str] = []
    found = await list_own_open_change_requests(fake.code_host(), repo, "me", None, failures, "merge")
    assert [c.number for c in found] == [2, 5]
    assert failures == []


async def test_list_own_open_change_requests_single_pr(fake, repo):
    fake.add_change_request(repo, head_reference="a", author="me", number=2)
    failures: list[str] = []
    found = await list_own_open_change_requests(fake.code_host(), repo, "me", 2, failures, "merge")
    assert [c.number for c in found] == [2]
    assert "ChangeRequests.list" not in [c.operation for c in fake.calls]


@pytest.mark.parametrize("only_pr", [None, 2])
async def test_list_own_open_change_requests_failure_is_collected(fake, repo, only_pr):
    fake.add_change_request(repo, head_reference="a", author="me", number=2)
    fake.fail_next("ChangeRequests.list" if only_pr is None else "ChangeRequests.get", CodeHostError("listing broke"))
    failures: list[str] = []
    assert await list_own_open_change_requests(fake.code_host(), repo, "me", only_pr, failures, "merge") == []
    assert failures == ["listing broke"]


async def test_list_own_open_change_requests_unsupported_stops(fake, repo):
    fake.fail_next("ChangeRequests.list", UnsupportedCapabilityError("no", capability="ChangeRequests"))
    with pytest.raises(UnsupportedCapabilityError):
        await list_own_open_change_requests(fake.code_host(), repo, "me", None, [], "merge")
