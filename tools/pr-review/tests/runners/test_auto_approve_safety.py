import json
from unittest.mock import MagicMock, patch

from harness.runners.auto_approve_github import (
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
    return json.dumps([{"bucket": n} for n in names]).encode()


def proc(code=0, out=b"", err=b""):
    return MagicMock(returncode=code, stdout=out, stderr=err)


def run(view=None, required=None, allc=None, reviews=(), user=ME, workflows=None):
    """view/required/allc: CompletedProcess mocks. reviews: list or None (read failed)."""
    view = view or proc(out=pr_json())
    required = required or proc(out=buckets("pass"))
    allc = allc or proc(out=buckets())
    workflows = workflows or proc(out=json.dumps({"workflows": []}).encode())
    calls = []

    def fake_cmd(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["pr", "view"]:
            return view
        if cmd[1] == "api":
            return workflows
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


def test_no_checks_but_repo_has_workflows_skips():
    wf = proc(out=json.dumps({"workflows": [{"state": "active"}]}).encode())
    for allc in (proc(1, b"", NO_CHECKS), proc(out=buckets())):
        res, _ = run(required=proc(1, b"", NO_REQ), allc=allc, workflows=wf)
        assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)


def test_no_checks_inactive_workflows_only_is_safe():
    wf = proc(out=json.dumps({"workflows": [{"state": "disabled_manually"}]}).encode())
    res, _ = run(required=proc(1, b"", NO_REQ), allc=proc(out=buckets()), workflows=wf)
    assert res.status is SafetyStatus.SAFE


def test_no_checks_workflows_read_failed():
    res, _ = run(required=proc(1, b"", NO_REQ), allc=proc(out=buckets()), workflows=proc(1, b"", b"HTTP 502"))
    assert res.status is SafetyStatus.READ_FAILED


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
