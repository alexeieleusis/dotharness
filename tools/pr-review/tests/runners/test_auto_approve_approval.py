import json
from unittest.mock import MagicMock, patch

from harness.runners.auto_approve_github import ApprovalStatus, approve_pr

ME = "reviewer"
HEAD = "abc123"


def proc(code=0, out=b""):
    return MagicMock(returncode=code, stdout=out, stderr=b"")


def pr_view(head=HEAD):
    data = {"mergeable": "MERGEABLE", "autoMergeRequest": None, "isDraft": False, "state": "OPEN", "headRefOid": head}
    return proc(out=json.dumps(data).encode())


def review(sha=HEAD, login=ME, state="APPROVED"):
    return {"state": state, "commit_id": sha, "user": {"login": login}}


def run(reviews=(), view=None, post=None, total=100, gate=True, safe=True, sha=HEAD, user=ME, dry_run=False):
    calls = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["pr", "view"]:
            return view or pr_view()
        return post or proc(out=b"{}")

    pages = None if reviews is None else list(reviews)
    with (
        patch("harness.runners.auto_approve_github.run_cmd", side_effect=fake),
        patch("harness.runners.auto_approve_github.fetch_all_pages", return_value=pages),
    ):
        result = approve_pr("o/r", 5, total, 500, gate, safe, sha, user, {}, dry_run)
    posts = [c for c in calls if "POST" in c]
    return result, calls, posts


def test_approves_with_commit_id_and_empty_body():
    result, _, posts = run()
    assert result.status is ApprovalStatus.APPROVED
    assert len(posts) == 1
    cmd = posts[0]
    assert "repos/o/r/pulls/5/reviews" in cmd
    assert "event=APPROVE" in cmd and f"commit_id={HEAD}" in cmd and "body=" in cmd


def test_gate_not_passed_no_call():
    result, calls, _ = run(gate=False)
    assert result.status is ApprovalStatus.SKIPPED and calls == []


def test_safety_not_passed_no_call():
    result, calls, _ = run(safe=False)
    assert result.status is ApprovalStatus.SKIPPED and calls == []


def test_missing_head_sha_no_call():
    result, calls, _ = run(sha="")
    assert result.status is ApprovalStatus.SKIPPED and calls == []


def test_size_threshold_is_strict():
    result, calls, _ = run(total=500)
    assert result.status is ApprovalStatus.SKIPPED and calls == []
    assert run(total=499)[0].status is ApprovalStatus.APPROVED


def test_already_approved_on_head_skips():
    result, _, posts = run(reviews=[review()])
    assert result.status is ApprovalStatus.SKIPPED and posts == []


def test_approval_on_older_commit_skips():
    # FR-6.4 / D-14: any APPROVED review by the acting account counts; re-approval after a push is manual.
    result, _, posts = run(reviews=[review(sha="old")])
    assert result.status is ApprovalStatus.SKIPPED and result.reason == "already-approved" and posts == []


def test_other_user_or_dismissed_approval_does_not_skip():
    result, _, posts = run(reviews=[review(login="someone"), review(state="DISMISSED")])
    assert result.status is ApprovalStatus.APPROVED and len(posts) == 1


def test_reviews_read_failure_makes_no_write():
    # _has_user_approved would return False here; the tri-state helper must not approve.
    result, _, posts = run(reviews=None)
    assert result.status is ApprovalStatus.READ_FAILED and posts == []


def test_pr_state_read_failure_makes_no_write():
    result, _, posts = run(view=proc(code=1))
    assert result.status is ApprovalStatus.READ_FAILED and posts == []


def test_head_moved_makes_no_write():
    result, _, posts = run(view=pr_view(head="newer"))
    assert result.status is ApprovalStatus.SKIPPED and posts == []


def test_unknown_acting_account_makes_no_write():
    result, _, posts = run(user="")
    assert result.status is ApprovalStatus.READ_FAILED and posts == []


def test_post_failure_reported():
    result, _, posts = run(post=proc(code=1))
    assert result.status is ApprovalStatus.WRITE_FAILED and len(posts) == 1


def test_dry_run_reads_but_never_posts():
    result, calls, posts = run(dry_run=True)
    assert result.status is ApprovalStatus.DRY_RUN
    assert posts == [] and len(calls) == 1  # only the head re-read
