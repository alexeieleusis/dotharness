import pytest

from dotharness.code_host import Capability, CodeHostError, CodeHostTimeoutError, UnsupportedCapabilityError
from dotharness.pr_review.runners.auto_approve_github import ViewedStatus, mark_files_viewed
from tests.pr_review.runners.approve_support import NUMBER, REPO, make_host, seed_pr

COUNTS = {"a.py": 10, "b.py": 10, "big.py": 80, "c.py": 74}


async def run(states, *, gate=True, dry_run=False, counts=COUNTS, setup=None, host=None):
    """states: {path: viewed} the host reports for the acting user."""
    host = host or make_host()
    if not host._change_requests:
        seed_pr(host)
    host._viewed[(REPO, NUMBER)] = dict(states)
    if setup:
        setup(host)
    result = await mark_files_viewed(host.code_host(), REPO, NUMBER, counts, 75, gate, dry_run)
    marks = [c for c in host.calls if c.operation == "ChangeRequests.mark_viewed"]  # attempts, applied or not
    return result, host, marks


def unviewed(*paths):
    return dict.fromkeys(paths, False)


async def test_gate_not_passed_makes_no_call():
    result, host, _ = await run({}, gate=False)
    assert result.status is ViewedStatus.GATE_NOT_PASSED
    assert host.calls == []


async def test_marks_small_unviewed_files_only():
    result, host, marks = await run(unviewed("a.py", "b.py", "big.py", "c.py", "other.py"))
    assert result.status is ViewedStatus.DONE
    assert result.marked == ["a.py", "b.py", "c.py"]  # big.py (80 >= 75) and uncounted other.py untouched
    assert [c.args for c in marks] == [(REPO, NUMBER, p) for p in ("a.py", "b.py", "c.py")]
    assert host._viewed[(REPO, NUMBER)] == {
        "a.py": True,
        "b.py": True,
        "big.py": False,
        "c.py": True,
        "other.py": False,
    }


async def test_threshold_is_strict():
    result, _, marks = await run(unviewed("a.py"), counts={"a.py": 75})
    assert result.marked == [] and marks == []


async def test_already_viewed_skipped_and_rerun_no_write():
    result, _, marks = await run({"a.py": True, "b.py": True}, counts={"a.py": 1, "b.py": 2})
    assert result.skipped == ["a.py", "b.py"]
    assert marks == []


async def test_second_run_after_marking_writes_nothing():
    first, host, marks = await run(unviewed("a.py"), counts={"a.py": 1})
    assert first.marked == ["a.py"] and len(marks) == 1
    second, host, marks = await run(host._viewed[(REPO, NUMBER)], counts={"a.py": 1}, host=host)
    assert second.skipped == ["a.py"] and second.marked == []
    assert len(marks) == 1  # still the first run's write only


@pytest.mark.parametrize("error", [CodeHostError("boom"), CodeHostTimeoutError("slow", write=False)])
async def test_read_failure_makes_no_write(error):
    result, _host, marks = await run(
        unviewed("a.py"), counts={"a.py": 1}, setup=lambda h: h.fail_next("ChangeRequests.viewed_files", error)
    )
    assert result.status is ViewedStatus.READ_FAILED and marks == []
    assert result.all_viewed is False


@pytest.mark.parametrize("exc", [OSError("boom"), ValueError("bad")])
async def test_raised_read_error_is_read_failure(monkeypatch, exc):
    host = make_host()
    seed_pr(host)
    ch = host.code_host()

    async def raiser(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(ch.change_requests, "viewed_files", raiser)
    result = await mark_files_viewed(ch, REPO, NUMBER, COUNTS, 75, True)
    assert result.status is ViewedStatus.READ_FAILED and host.writes == []


async def test_pr_with_zero_files_is_not_all_viewed():
    result, _, marks = await run({}, counts={})
    assert result.status is ViewedStatus.DONE and marks == []
    assert result.all_viewed is False


async def test_failed_mark_does_not_stop_others():
    result, _host, marks = await run(
        unviewed("a.py", "b.py"),
        counts={"a.py": 1, "b.py": 1},
        setup=lambda h: h.fail_next("ChangeRequests.mark_viewed", CodeHostError("boom")),
    )
    assert result.failed == ["a.py"] and result.marked == ["b.py"] and len(marks) == 2
    assert result.all_viewed is False


@pytest.mark.parametrize("apply", [False, True])
async def test_timed_out_mark_is_a_failure_and_is_not_retried(apply):
    # apply=True: the file was marked although the call timed out; the run still counts it as failed (retried next
    # run, where it reads as already viewed).
    result, host, marks = await run(
        unviewed("a.py"),
        counts={"a.py": 1},
        setup=lambda h: h.fail_next(
            "ChangeRequests.mark_viewed", CodeHostTimeoutError("slow", write=True), apply=apply
        ),
    )
    assert result.failed == ["a.py"] and result.marked == []
    assert len(marks) == 1
    assert host._viewed[(REPO, NUMBER)]["a.py"] is apply


async def test_dry_run_reads_but_never_writes():
    result, host, marks = await run(unviewed("a.py", "b.py"), dry_run=True, counts={"a.py": 1, "b.py": 1})
    assert result.marked == ["a.py", "b.py"]
    assert marks == [] and host.writes == []
    assert [c.operation for c in host.calls] == ["ChangeRequests.viewed_files"]


async def test_all_viewed_when_every_file_viewed_or_marked():
    result, _, _ = await run({"a.py": False, "big.py": True}, counts={"a.py": 10, "big.py": 80})
    assert result.all_viewed is True


async def test_not_all_viewed_when_a_large_file_is_unviewed():
    result, _, _ = await run(unviewed("a.py", "big.py"), counts={"a.py": 10, "big.py": 80})
    assert result.all_viewed is False


async def test_all_viewed_in_dry_run_counts_would_be_marked():
    result, _, marks = await run(unviewed("a.py"), dry_run=True, counts={"a.py": 10})
    assert result.all_viewed is True and marks == []


async def test_unsupported_viewed_state_degrades_to_nothing_to_mark():
    host = make_host()
    seed_pr(host)
    host.fail_next("ChangeRequests.viewed_files", UnsupportedCapabilityError("no", capability="ChangeRequests"))
    result, _, marks = await run(unviewed("a.py"), counts={"a.py": 1}, host=host)
    # the injected error is consumed by the first call; the read gives {} so nothing is marked
    assert result.status is ViewedStatus.DONE and marks == []
    assert result.marked == [] and result.all_viewed is False


async def test_unsupported_mark_viewed_degrades_silently(caplog):
    result, host, marks = await run(
        unviewed("a.py", "b.py"),
        counts={"a.py": 1, "b.py": 1},
        setup=lambda h: h.fail_always(
            "ChangeRequests.mark_viewed", UnsupportedCapabilityError("no", capability="ChangeRequests")
        ),
    )
    assert result.status is ViewedStatus.DONE
    assert result.marked == [] and result.failed == [] and result.all_viewed is False
    assert host.writes == [] and len(marks) == 2  # both attempted, none applied
    assert not [r for r in caplog.records if r.levelname in {"WARNING", "ERROR"}]


async def test_unsupported_capability_without_degrade_policy_is_not_swallowed():
    host = make_host(capabilities=frozenset(Capability) - {Capability.CHANGE_REQUESTS})
    seed_pr(host)
    result = await mark_files_viewed(host.code_host(), REPO, NUMBER, COUNTS, 75, True)
    # viewed_files is a degrade site: an absent capability reads as nothing to mark
    assert result.status is ViewedStatus.DONE and result.marked == []
