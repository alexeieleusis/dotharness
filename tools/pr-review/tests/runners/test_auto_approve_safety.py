import json
import logging
from unittest.mock import MagicMock, patch

from harness.runners.auto_approve_github import (
    _MAX_NAMED_CHECKS,
    REASON_ALREADY_APPROVED,
    REASON_AUTO_MERGE,
    REASON_CI_BLOCKED,
    REASON_CONFLICTING,
    REASON_DRAFT,
    REASON_MERGEABLE_UNKNOWN,
    REASON_NOT_OPEN,
    SafetyStatus,
    check_approval_safety,
)

ME = "reviewer"
HEAD = "abc123"
OLD = "old999"
NO_REQ = b"no required checks reported on the 'x' branch"
NO_CHECKS = b"no checks reported on the 'x' branch"


def pr_json(**over):
    data = {"mergeable": "MERGEABLE", "autoMergeRequest": None, "isDraft": False, "state": "OPEN", "headRefOid": HEAD}
    data.update(over)
    return json.dumps(data).encode()


def buckets(*names):
    return json.dumps([{"name": f"check-{i}", "bucket": n} for i, n in enumerate(names)]).encode()


def named(*pairs):
    return json.dumps([{"name": n, "bucket": b} for n, b in pairs]).encode()


def runs(*rows):
    """rows: (name, bucket, workflow, startedAt, completedAt); None fields are omitted."""
    keys = ("name", "bucket", "workflow", "startedAt", "completedAt")
    return json.dumps([{k: v for k, v in zip(keys, row, strict=True) if v is not None} for row in rows]).encode()


T1, T2, T3 = "2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z", "2026-01-01T12:00:00Z"
ZERO = "0001-01-01T00:00:00Z"


def proc(code=0, out=b"", err=b""):
    return MagicMock(returncode=code, stdout=out, stderr=err)


def run(view=None, required=None, allc=None, reviews=(), user=ME):
    """view/required/allc: CompletedProcess mocks. reviews: list or None (read failed)."""
    view = view or proc(out=pr_json())
    required = required or proc(out=buckets("pass"))
    allc = allc or proc(out=buckets())
    calls = []

    def fake_cmd(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["pr", "view"]:
            return view
        return required if "--required" in cmd else allc

    page = None if reviews is None else list(reviews)
    with (
        patch("harness.runners.auto_approve_github.run_cmd", side_effect=fake_cmd),
        patch("harness.runners.auto_approve_github.fetch_all_pages", return_value=page),
    ):
        result = check_approval_safety("o/r", 5, user, {})
    return result, calls


def approval(sha=HEAD, login=ME, state="APPROVED"):
    return {"state": state, "commit_id": sha, "user": {"login": login}}


def test_required_pass_and_skipping_safe():
    res, calls = run(required=proc(out=buckets("pass", "skipping")))
    assert res.status is SafetyStatus.SAFE
    assert res.head_sha == HEAD
    assert sum("checks" in c for c in calls) == 1


def test_required_blockers():
    for bad in ("fail", "pending", "cancel"):
        res, _ = run(required=proc(out=buckets("pass", bad)))
        assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)
        assert res.head_sha == HEAD


def test_no_required_checks_falls_back_to_all():
    res, calls = run(required=proc(1, b"", NO_REQ), allc=proc(out=buckets("pass", "skipping")))
    assert res.status is SafetyStatus.SAFE
    assert sum("checks" in c for c in calls) == 2


def test_fallback_fail_bucket_with_exit_zero_blocks():
    res, _ = run(required=proc(1, b"", NO_REQ), allc=proc(0, buckets("pass", "fail")))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)


def test_required_pending_exit_8_with_json_blocks():
    res, _ = run(required=proc(8, buckets("pending")))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)


def test_required_pending_exit_8_without_json_is_read_failed():
    res, _ = run(required=proc(8, b""))
    assert res.status is SafetyStatus.READ_FAILED


def test_required_empty_list_exit_zero_falls_back_to_all():
    res, calls = run(required=proc(out=buckets()), allc=proc(out=buckets("pass", "fail")))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)
    assert sum("checks" in c for c in calls) == 2


def test_unrecognised_bucket_blocks():
    res, _ = run(required=proc(out=buckets("pass", "unknown")))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)


def test_other_exit_1_is_read_failed():
    res, _ = run(required=proc(1, b"", b"HTTP 502"))
    assert res.status is SafetyStatus.READ_FAILED


def test_other_exit_1_without_required_flag_is_read_failed():
    res, _ = run(required=proc(1, b"", NO_REQ), allc=proc(1, b"", b"HTTP 502"))
    assert res.status is SafetyStatus.READ_FAILED


def test_no_checks_at_all_is_safe():
    for allc in (proc(1, b"", NO_CHECKS), proc(out=buckets())):
        res, _ = run(required=proc(1, b"", NO_REQ), allc=allc)
        assert res.status is SafetyStatus.SAFE


def test_unparseable_checks_read_failed():
    assert run(required=proc(out=b"not json"))[0].status is SafetyStatus.READ_FAILED
    assert run(required=proc(out=b'{"a": 1}'))[0].status is SafetyStatus.READ_FAILED
    assert run(required=proc(out=b'[{"name": "x"}]'))[0].status is SafetyStatus.READ_FAILED


def test_conflicting_skips():
    res, _ = run(view=proc(out=pr_json(mergeable="CONFLICTING")))
    assert (res.status, res.reason, res.head_sha) == (SafetyStatus.SKIP, REASON_CONFLICTING, HEAD)


def test_mergeable_unknown_skips():
    res, _ = run(view=proc(out=pr_json(mergeable="UNKNOWN")))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_MERGEABLE_UNKNOWN)


def test_auto_merge_skips():
    res, _ = run(view=proc(out=pr_json(autoMergeRequest={"enabledBy": {"login": "x"}})))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_AUTO_MERGE)


def test_draft_skips():
    res, _ = run(view=proc(out=pr_json(isDraft=True)))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_DRAFT)


def test_not_open_skips():
    for state in ("MERGED", "CLOSED"):
        res, _ = run(view=proc(out=pr_json(state=state)))
        assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_NOT_OPEN)


def test_already_approved_on_head_skips_without_checks():
    res, calls = run(reviews=[approval()])
    assert (res.status, res.reason, res.head_sha) == (SafetyStatus.SKIP, REASON_ALREADY_APPROVED, HEAD)
    assert not any("checks" in c for c in calls)


def test_approval_on_old_commit_skips():
    res, _ = run(reviews=[approval(sha=OLD)])
    assert (res.status, res.reason, res.head_sha) == (SafetyStatus.SKIP, REASON_ALREADY_APPROVED, HEAD)


def test_other_user_or_dismissed_approval_does_not_skip():
    for review in (approval(login="other"), approval(state="DISMISSED")):
        assert run(reviews=[review])[0].status is SafetyStatus.SAFE


def test_approval_login_case_insensitive():
    assert run(reviews=[approval(login="Reviewer")], user="REVIEWER")[0].status is SafetyStatus.SKIP


def test_head_sha_returned_when_safe():
    assert run()[0].head_sha == HEAD


def test_view_failure_read_failed():
    for view in (proc(1, b"", b"err"), proc(out=b"nope"), proc(out=b"[]"), proc(out=pr_json(headRefOid=""))):
        res, _ = run(view=view)
        assert res.status is SafetyStatus.READ_FAILED
        assert res.head_sha == ""


def test_reviews_failure_read_failed():
    res, _ = run(reviews=None)
    assert (res.status, res.head_sha) == (SafetyStatus.READ_FAILED, HEAD)


def test_unknown_acting_account_read_failed():
    assert run(user="")[0].status is SafetyStatus.READ_FAILED


def test_exception_read_failed():
    with patch("harness.runners.auto_approve_github.run_cmd", side_effect=OSError("boom")):
        assert check_approval_safety("o/r", 5, ME, {}).status is SafetyStatus.READ_FAILED


def test_skip_logs_failing_check_names_with_exact_reason(caplog):
    checks = named(("lint", "pass"), ("unit", "fail"), ("e2e", "pending"))
    with caplog.at_level(logging.INFO):
        res, _ = run(required=proc(out=checks))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)
    assert res.reason == "ci-blocked"
    assert "failing checks: unit(fail)" in caplog.text
    assert "lint" not in caplog.text and "e2e" not in caplog.text  # passing and pending are not named


def test_failing_check_list_is_capped_with_omitted_count(caplog):
    checks = named(*[(f"c{i:02d}", "fail") for i in range(_MAX_NAMED_CHECKS + 3)])
    with caplog.at_level(logging.INFO):
        res, _ = run(required=proc(out=checks))
    assert res.reason == REASON_CI_BLOCKED
    assert f"c{_MAX_NAMED_CHECKS - 1:02d}(fail)" in caplog.text
    assert f"c{_MAX_NAMED_CHECKS:02d}(fail)" not in caplog.text
    assert "(+3 more omitted)" in caplog.text


def test_missing_name_is_tolerated_but_missing_bucket_is_not(caplog):
    with caplog.at_level(logging.INFO):
        res, _ = run(required=proc(out=b'[{"bucket": "fail"}]'))
    assert res.reason == REASON_CI_BLOCKED
    assert "<unnamed>(fail)" in caplog.text
    res, _ = run(required=proc(out=named(("x", "pass")).replace(b"bucket", b"state")))
    assert res.status is SafetyStatus.READ_FAILED


def test_passing_checks_log_no_failing_names(caplog):
    with caplog.at_level(logging.INFO):
        res, _ = run(required=proc(out=named(("lint", "pass"))))
    assert res.status is SafetyStatus.SAFE
    assert "failing checks" not in caplog.text


def _ci(*rows):
    res, calls = run(required=proc(out=runs(*rows)))
    assert not any(c[1:3] == ["pr", "review"] for c in calls)
    return res


def test_failed_then_passing_rerun_does_not_block():
    res = _ci(("t", "fail", "W", T1, T1), ("t", "pass", "W", T2, T3))
    assert res.status is SafetyStatus.SAFE


def test_latest_order_independent_of_list_order():
    res = _ci(("t", "pass", "W", T2, T3), ("t", "fail", "W", T1, T1))
    assert res.status is SafetyStatus.SAFE


def test_passing_then_failed_rerun_blocks():
    res = _ci(("t", "pass", "W", T1, T1), ("t", "fail", "W", T2, T3))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)


def test_pending_rerun_blocks():
    res = _ci(("t", "fail", "W", T1, T1), ("t", "pending", "W", T2, ZERO))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)


def test_latest_started_without_completed_blocks_even_with_pass_bucket():
    res = _ci(("t", "pass", "W", T1, T1), ("t", "pass", "W", T2, None))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)


def test_incomplete_run_compared_by_started_at_against_completed_at():
    # Incomplete run started before the other run completed: the completed one is later on the timeline.
    res = _ci(("t", "pending", "W", T1, ZERO), ("t", "pass", "W", T1, T2))
    assert res.status is SafetyStatus.SAFE


def test_duplicate_without_any_timestamp_is_read_failed():
    for rows in (
        (("t", "fail", "W", None, None), ("t", "pass", "W", T2, T3)),
        (("t", "pass", "W", T1, T1), ("t", "pass", "W", None, ZERO)),
    ):
        res = _ci(*rows)
        assert res.status is SafetyStatus.READ_FAILED


def test_tie_for_latest_blocks_when_any_blocks():
    res = _ci(("t", "fail", "W", T1, T2), ("t", "pass", "W", T1, T2))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)


def test_same_name_in_different_workflows_are_separate_checks():
    res = _ci(("t", "fail", "A", T1, T1), ("t", "pass", "B", T2, T3))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)


def test_single_runs_need_no_timestamps():
    res = _ci(("a", "pass", None, None, None), ("b", "skipping", "W", None, None))
    assert res.status is SafetyStatus.SAFE


def test_latest_run_applies_to_fallback_read():
    allc = proc(out=runs(("t", "fail", "W", T1, T1), ("t", "pass", "W", T2, T3)))
    res, _ = run(required=proc(1, b"", NO_REQ), allc=allc)
    assert res.status is SafetyStatus.SAFE


def test_malformed_check_output_is_read_failed():
    for out in (b"not json", b'{"a": 1}', b'["x"]', b'[{"name": "x"}]', b'[{"bucket": 3}]'):
        res, _ = run(required=proc(out=out))
        assert res.status is SafetyStatus.READ_FAILED


def test_checks_requested_with_run_fields():
    _, calls = run()
    (cmd,) = [c for c in calls if "--required" in c]
    assert cmd[cmd.index("--json") + 1] == "name,bucket,workflow,startedAt,completedAt"
