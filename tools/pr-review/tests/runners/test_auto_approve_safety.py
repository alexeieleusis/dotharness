import json
import logging
import subprocess
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
    _check_ci,
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


def repo_json(name="main"):
    return json.dumps({"defaultBranchRef": {"name": name}}).encode()


def run(view=None, required=None, allc=None, reviews=(), user=ME, repo=None, heads=None, roots=None):
    """view/required/allc/repo: CompletedProcess mocks (or an exception for repo). reviews: list or None (read failed).

    heads: {branch: mock} for ``pr list --head``; roots: {pr number: mock} for ``pr checks <n> --required`` of
    a stack root. Calls for PR 5 (the PR under test) use required/allc; a missing heads key means no open PR.
    """
    heads = heads or {}
    roots = roots or {}
    repo = repo or proc(out=repo_json())
    view = view or proc(out=pr_json())
    required = required or proc(out=buckets("pass"))
    allc = allc or proc(out=buckets())
    calls = []

    def fake_cmd(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["pr", "view"]:
            return view
        if cmd[1:3] == ["repo", "view"]:
            if isinstance(repo, Exception):
                raise repo
            return repo
        if cmd[1:3] == ["pr", "list"]:
            return heads.get(cmd[cmd.index("--head") + 1], proc(out=b"[]"))
        if cmd[1:3] == ["pr", "checks"] and cmd[3] != "5":
            return roots[int(cmd[3])]
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


def test_pr_view_requests_base_branch_and_checks_sequence_unchanged():
    result, calls = run(view=proc(out=pr_json(baseRefName="stack-a")))
    assert result.status is SafetyStatus.SAFE
    assert "baseRefName" in calls[0][calls[0].index("--json") + 1]
    assert sum("checks" in c for c in calls) == 1
    assert "--required" in calls[-1]


def test_default_branch_lookup_success_keeps_result_and_checks_calls():
    result, calls = run(repo=proc(out=repo_json("trunk")))
    assert result.status is SafetyStatus.SAFE
    (lookup,) = [c for c in calls if c[1:3] == ["repo", "view"]]
    assert lookup[3:] == ["o/r", "--json", "defaultBranchRef"]
    assert sum("checks" in c for c in calls) == 1


def test_default_branch_reaches_check_ci(caplog):
    with caplog.at_level(logging.DEBUG):
        run(view=proc(out=pr_json(baseRefName="stack-a")), repo=proc(out=repo_json("trunk")))
    assert any("base stack-a, default branch trunk" in r.getMessage() for r in caplog.records)


def test_default_branch_lookup_failure_is_read_failed_without_approval():
    bad = [
        proc(code=1, err=b"boom"),
        proc(out=b"not json"),
        proc(out=b"{}"),
        proc(out=repo_json("")),
        proc(out=json.dumps({"defaultBranchRef": None}).encode()),
        proc(out=json.dumps({"defaultBranchRef": {"name": 7}}).encode()),
        subprocess.TimeoutExpired(["gh"], 30),
    ]
    for repo in bad:
        result, calls = run(repo=repo)
        assert result.status is SafetyStatus.READ_FAILED
        assert not any("checks" in c for c in calls)
        assert not any(c[1:3] == ["pr", "review"] for c in calls)


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


# --- stack-root walk (Phase 3b) ---

STACK_VIEW = proc(out=pr_json(baseRefName="stack-a"))


def pr_list(*pairs):
    """pairs: (number, baseRefName)."""
    return proc(out=json.dumps([{"number": n, "baseRefName": b} for n, b in pairs]).encode())


def walk(heads=None, roots=None, **kw):
    kw.setdefault("view", STACK_VIEW)
    kw.setdefault("required", proc(1, b"", NO_REQ))
    return run(
        heads=heads or {"stack-a": pr_list((4, "main"))}, roots=roots or {4: proc(out=named(("build", "x")))}, **kw
    )


def no_post(calls):
    return not any(c[1:3] == ["pr", "review"] for c in calls)


def test_stacked_pr_with_only_non_required_failures_is_approved():
    allc = proc(out=named(("build", "pass"), ("lint", "fail")))
    res, calls = walk(allc=allc)
    assert res.status is SafetyStatus.SAFE
    assert res.head_sha == HEAD
    assert any(c[1:3] == ["pr", "list"] for c in calls)
    (root_read,) = [c for c in calls if c[1:3] == ["pr", "checks"] and c[3] == "4"]
    assert root_read[4:] == ["--repo", "o/r", "--required", "--json", "name"]


def test_walk_follows_chain_to_the_root():
    heads = {"stack-a": pr_list((3, "stack-b")), "stack-b": pr_list((4, "main"))}
    res, calls = walk(heads=heads, allc=proc(out=named(("build", "pass"), ("lint", "fail"))))
    assert res.status is SafetyStatus.SAFE
    assert [c[c.index("--head") + 1] for c in calls if c[1:3] == ["pr", "list"]] == ["stack-a", "stack-b"]
    assert not any(c[1:3] == ["pr", "checks"] and c[3] == "3" for c in calls)


def test_required_at_root_failing_on_pr_is_skipped_and_named(caplog):
    with caplog.at_level(logging.INFO):
        res, calls = walk(allc=proc(out=named(("build", "fail"), ("lint", "pass"))))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)
    assert any("failing checks: build(fail)" in r.getMessage() for r in caplog.records)
    assert no_post(calls)


def test_required_check_pending_on_pr_blocks():
    res, _ = walk(allc=proc(8, named(("build", "pending"))))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)


def test_own_base_with_required_checks_makes_no_walk_calls():
    res, calls = run(view=STACK_VIEW, required=proc(out=buckets("pass")))
    assert res.status is SafetyStatus.SAFE
    assert sum("checks" in c for c in calls) == 1
    assert not any(c[1:3] == ["pr", "list"] for c in calls)
    assert len(calls) == 3  # pr view, repo view, required checks


def test_base_is_default_branch_falls_back_to_all_without_walk():
    res, calls = run(
        view=proc(out=pr_json(baseRefName="main")),
        required=proc(1, b"", NO_REQ),
        allc=proc(out=buckets("pass", "fail")),
    )
    assert res.status is SafetyStatus.SKIP
    assert not any(c[1:3] == ["pr", "list"] for c in calls)


def test_no_open_pr_for_base_falls_back_to_all_checks():
    res, _ = walk(heads={"x": pr_list()}, allc=proc(out=buckets("pass", "fail")))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)
    res, _ = walk(heads={"x": pr_list()}, allc=proc(out=buckets("pass", "skipping")))
    assert res.status is SafetyStatus.SAFE


def test_root_without_required_checks_falls_back_to_all():
    roots = {4: proc(1, b"", NO_REQ)}
    assert walk(roots=roots, allc=proc(out=buckets("pass", "fail")))[0].reason == REASON_CI_BLOCKED
    assert walk(roots=roots, allc=proc(out=buckets("pass")))[0].status is SafetyStatus.SAFE


def test_walk_read_failures_are_read_failed_without_post():
    bad_heads = [
        proc(1, b"", b"boom"),
        proc(out=b"not json"),
        proc(out=b'{"a": 1}'),
        proc(out=b'[{"number": 4}]'),
        proc(out=b'[{"number": "4", "baseRefName": "main"}]'),
    ]
    for bad in bad_heads:
        res, calls = walk(heads={"stack-a": bad})
        assert res.status is SafetyStatus.READ_FAILED
        assert no_post(calls)


def test_walk_timeout_is_read_failed():
    def boom(cmd, **kwargs):
        if cmd[1:3] == ["pr", "list"]:
            raise subprocess.TimeoutExpired(cmd, 30)
        return proc(out=repo_json()) if cmd[1:3] == ["repo", "view"] else proc(1, b"", NO_REQ)

    with patch("harness.runners.auto_approve_github.run_cmd", side_effect=boom):
        res = _check_ci("o/r", 5, "stack-a", {})
    assert res is not None
    assert res.status is SafetyStatus.READ_FAILED


def test_ambiguous_head_match_fails_closed():
    res, calls = walk(heads={"stack-a": pr_list((4, "main"), (6, "main"))})
    assert res.status is SafetyStatus.READ_FAILED
    assert no_post(calls)


def test_root_read_failures_are_read_failed_without_post():
    bad_roots = [
        proc(1, b"", b"HTTP 502"),
        proc(2, b"", b"x"),
        proc(8, named(("build", "pending"))),
        proc(8, b""),
        proc(out=b"not json"),
        proc(out=b'{"a": 1}'),
        proc(out=b'[{"nom": "x"}]'),
    ]
    for bad in bad_roots:
        res, calls = walk(roots={4: bad})
        assert res.status is SafetyStatus.READ_FAILED
        assert no_post(calls)


def test_default_branch_lookup_failure_skips_before_walk():
    res, calls = walk(repo=proc(1, b"", b"boom"))
    assert res.status is SafetyStatus.READ_FAILED
    assert not any(c[1:3] == ["pr", "list"] for c in calls)
    assert no_post(calls)


def test_depth_cap_fails_closed_and_ten_is_allowed():
    def chain(n):
        heads = {f"b{i}": pr_list((100 + i, f"b{i + 1}")) for i in range(n)}
        heads[f"b{n - 1}"] = pr_list((100 + n - 1, "main"))
        return heads, {100 + n - 1: proc(out=named(("build", "x")))}

    ok_heads, ok_roots = chain(10)
    res, calls = walk(
        view=proc(out=pr_json(baseRefName="b0")),
        heads=ok_heads,
        roots=ok_roots,
        allc=proc(out=named(("build", "pass"))),
    )
    assert res.status is SafetyStatus.SAFE
    assert sum(c[1:3] == ["pr", "list"] for c in calls) == 10

    heads, roots = chain(11)
    res, calls = walk(view=proc(out=pr_json(baseRefName="b0")), heads=heads, roots=roots)
    assert res.status is SafetyStatus.READ_FAILED
    assert sum(c[1:3] == ["pr", "list"] for c in calls) == 10
    assert no_post(calls)

    cyc = {"stack-a": pr_list((4, "stack-b")), "stack-b": pr_list((3, "stack-a"))}
    res, calls = walk(heads=cyc)
    assert res.status is SafetyStatus.READ_FAILED


def test_required_name_with_no_run_on_pr_does_not_pass(caplog):
    roots = {4: proc(out=named(("build", "x"), ("test", "x")))}
    with caplog.at_level(logging.INFO):
        res, _ = walk(roots=roots, allc=proc(out=named(("build", "pass"), ("lint", "pass"))))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)
    assert any("no run: test" in r.getMessage() for r in caplog.records)
    res, _ = walk(allc=proc(1, b"", NO_CHECKS))
    assert (res.status, res.reason) == (SafetyStatus.SKIP, REASON_CI_BLOCKED)


def test_superseded_failed_run_of_required_name_does_not_block():
    allc = proc(out=runs(("build", "fail", "W", T1, T1), ("build", "pass", "W", T2, T2)))
    assert walk(allc=allc)[0].status is SafetyStatus.SAFE


def test_empty_required_set_and_no_pr_checks_passes():
    for roots in ({4: proc(1, b"", NO_REQ)}, {4: proc(out=b"[]")}):
        for allc in (proc(1, b"", NO_CHECKS), proc(out=buckets())):
            res, _ = walk(roots=roots, allc=allc)
            assert res.status is SafetyStatus.SAFE
