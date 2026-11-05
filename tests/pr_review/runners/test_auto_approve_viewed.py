import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from dotharness.pr_review.runners.auto_approve_github import _MAX_FILE_PAGES, ViewedStatus, mark_files_viewed

COUNTS = {"a.py": 10, "b.py": 10, "big.py": 80, "c.py": 74}


def proc(code=0, out=b""):
    return MagicMock(returncode=code, stdout=out, stderr=b"")


def page(nodes, has_next=False, cursor=None, node_id="PR_1"):
    body = {
        "data": {
            "repository": {
                "pullRequest": {
                    "id": node_id,
                    "files": {"nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}},
                }
            }
        }
    }
    return proc(out=json.dumps(body).encode())


def node(path, state="UNVIEWED"):
    return {"path": path, "viewerViewedState": state, "additions": 1, "deletions": 0, "changeType": "MODIFIED"}


def raw(pr):
    return proc(out=json.dumps({"data": {"repository": {"pullRequest": pr}}}).encode())


def run(reads, mark=None, gate=True, dry_run=False, counts=COUNTS, repo="o/r"):
    """reads: list of read responses in order; mark: mark response (or callable on path)."""
    reads = list(reads)
    calls = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        if any("markFileAsViewed" in a for a in cmd):
            path = next(a[5:] for a in cmd if a.startswith("path="))
            return mark(path) if callable(mark) else (mark or proc(out=b'{"data":{}}'))
        item = reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    with patch("dotharness.pr_review.runners.ci_checks.run_cmd", side_effect=fake):
        result = mark_files_viewed(repo, 5, counts, 75, gate, {}, dry_run)
    writes = [c for c in calls if any("markFileAsViewed" in a for a in c)]
    return result, calls, writes


def test_gate_not_passed_makes_no_call():
    result, calls, _ = run([], gate=False)
    assert result.status is ViewedStatus.GATE_NOT_PASSED
    assert calls == []


def test_marks_small_unviewed_files_only():
    reads = [page([node("a.py"), node("b.py"), node("big.py"), node("c.py"), node("other.py")])]
    result, _, writes = run(reads)
    assert result.status is ViewedStatus.DONE
    assert result.marked == ["a.py", "b.py", "c.py"]  # big.py (80 >= 75) and uncounted other.py untouched
    assert len(writes) == 3
    assert "pullRequestId=PR_1" in writes[0]


def test_threshold_is_strict():
    result, _, writes = run([page([node("a.py")])], counts={"a.py": 75})
    assert result.marked == [] and writes == []


def test_already_viewed_skipped_and_rerun_no_write():
    result, _, writes = run([page([node("a.py", "VIEWED"), node("b.py", "VIEWED")])], counts={"a.py": 1, "b.py": 2})
    assert result.skipped == ["a.py", "b.py"]
    assert writes == []


def test_dismissed_is_remarked():
    result, _, writes = run([page([node("a.py", "DISMISSED")])], counts={"a.py": 1})
    assert result.marked == ["a.py"] and len(writes) == 1


def test_pagination_collects_all_pages():
    reads = [page([node("a.py")], True, "CUR1"), page([node("b.py")])]
    result, calls, _ = run(reads, counts={"a.py": 1, "b.py": 1})
    assert result.marked == ["a.py", "b.py"]
    assert "cursor=CUR1" in calls[1]
    assert not any(a.startswith("cursor=") for a in calls[0])


def test_read_failure_makes_no_write():
    result, _, writes = run([proc(code=1)])
    assert result.status is ViewedStatus.READ_FAILED and writes == []


def test_read_failure_on_second_page_makes_no_write():
    result, _, writes = run([page([node("a.py")], True, "C"), proc(code=1)], counts={"a.py": 1})
    assert result.status is ViewedStatus.READ_FAILED and writes == []


def test_malformed_read_makes_no_write():
    result, _, writes = run([proc(out=b"not json")])
    assert result.status is ViewedStatus.READ_FAILED and writes == []


_GOOD_FILES = {"nodes": [], "pageInfo": {"hasNextPage": False}}
MALFORMED_PAGES = {
    "next-page-without-cursor": page([node("a.py")], True, None),
    "next-page-empty-cursor": page([node("a.py")], True, ""),
    "missing-pr-id": raw({"files": _GOOD_FILES}),
    "empty-pr-id": raw({"id": "", "files": _GOOD_FILES}),
    "pageinfo-without-hasnextpage": raw({"id": "PR_1", "files": {"nodes": [], "pageInfo": {}}}),
    "non-list-nodes": raw({"id": "PR_1", "files": {"nodes": {"path": "a.py"}, "pageInfo": {"hasNextPage": False}}}),
    "node-with-null-path": page([{"path": None}]),
    "node-not-a-dict": page(["a.py"]),
}


@pytest.mark.parametrize("response", MALFORMED_PAGES.values(), ids=MALFORMED_PAGES.keys())
def test_malformed_page_is_read_failure_without_writes(response):
    result, _, writes = run([response], counts={"a.py": 1})
    assert result.status is ViewedStatus.READ_FAILED and writes == []
    assert result.all_viewed is False


def test_page_cap_reached_is_read_failure_not_truncated_done():
    reads = [page([node("a.py")], True, f"C{i}") for i in range(_MAX_FILE_PAGES)]
    result, calls, writes = run(reads, counts={"a.py": 1})
    assert result.status is ViewedStatus.READ_FAILED and writes == []
    assert result.all_viewed is False
    assert len(calls) == _MAX_FILE_PAGES


@pytest.mark.parametrize("repo", ["norepo", "", "/name", "owner/"])
def test_invalid_repo_is_read_failure_without_any_call(repo):
    result, calls, writes = run([], repo=repo)
    assert result.status is ViewedStatus.READ_FAILED and writes == [] and calls == []


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired("gh", 1), OSError("boom"), ValueError("bad")])
def test_run_cmd_raising_is_caught_as_read_failure(exc):
    result, _, writes = run([exc])
    assert result.status is ViewedStatus.READ_FAILED and writes == []


def test_pr_with_zero_files_is_not_all_viewed():
    result, _, writes = run([page([])], counts={})
    assert result.status is ViewedStatus.DONE and writes == []
    assert result.all_viewed is False


def test_failed_mark_does_not_stop_others():
    def mark(path):
        return proc(code=1) if path == "a.py" else proc(out=b'{"data":{}}')

    result, _, writes = run([page([node("a.py"), node("b.py")])], mark=mark, counts={"a.py": 1, "b.py": 1})
    assert result.failed == ["a.py"] and result.marked == ["b.py"] and len(writes) == 2


def test_graphql_errors_in_mutation_count_as_failure():
    result, _, _ = run([page([node("a.py")])], mark=proc(out=b'{"errors":[{"message":"x"}]}'), counts={"a.py": 1})
    assert result.failed == ["a.py"]


@pytest.mark.parametrize("out", [b"not json", b"[]", b'["errors"]'])
def test_non_object_mutation_output_counts_as_failure(out):
    result, _, _ = run([page([node("a.py")])], mark=proc(out=out), counts={"a.py": 1})
    assert result.failed == ["a.py"] and result.marked == []
    assert result.all_viewed is False


def test_dry_run_reads_but_never_writes():
    result, calls, writes = run([page([node("a.py"), node("b.py")])], dry_run=True, counts={"a.py": 1, "b.py": 1})
    assert result.marked == ["a.py", "b.py"]
    assert writes == [] and len(calls) == 1


def test_all_viewed_when_every_file_viewed_or_marked():
    reads = [page([node("a.py"), node("big.py", "VIEWED")])]
    result, _, _ = run(reads, counts={"a.py": 10, "big.py": 80})
    assert result.all_viewed is True


def test_not_all_viewed_when_a_large_file_is_unviewed():
    reads = [page([node("a.py"), node("big.py")])]
    result, _, _ = run(reads, counts={"a.py": 10, "big.py": 80})
    assert result.all_viewed is False


def test_not_all_viewed_when_a_mark_fails():
    reads = [page([node("a.py")])]
    result, _, _ = run(reads, mark=proc(code=1), counts={"a.py": 10})
    assert result.all_viewed is False


def test_all_viewed_in_dry_run_counts_would_be_marked():
    result, _, writes = run([page([node("a.py")])], dry_run=True, counts={"a.py": 10})
    assert result.all_viewed is True and writes == []
