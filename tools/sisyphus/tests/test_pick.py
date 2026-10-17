"""Picker tests: trackers, PR lookups and ``gh``/Linear transports are all fakes. No network, no real gh."""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from sisyphus import pick
from sisyphus.config import WorkItemSource
from sisyphus.orchestrator import BRANCH_CREATED_KEY, CI_FIX_STAGE, MERGE_STAGE, OPEN_PR_STAGE, PullRequest, branch_name
from sisyphus.pick import (
    Candidate,
    ExecResult,
    GhCli,
    GhPullRequestLookup,
    GitHubIssueTracker,
    LinearTracker,
    LinkedPr,
    PickError,
    default_open_pr_check,
    find_awaiting_review,
    mentions_issue,
    pick_items,
)
from sisyphus.runrecord import RunRecord

GH = WorkItemSource(kind="github", assignee="me", pick_label="sisyphus")
LIN = WorkItemSource(kind="linear", assignee="me@x.io", pick_state="Todo")


class FakeTracker:
    def __init__(self, candidates: Sequence[Candidate]) -> None:
        self.candidates = list(candidates)

    def list_candidates(self, source: WorkItemSource) -> Sequence[Candidate]:
        return self.candidates


class FakeLookup:
    def __init__(self, linked: Mapping[int, Sequence[LinkedPr]] | None = None, bodies: Sequence[str] = ()) -> None:
        self.linked = dict(linked or {})
        self.bodies = list(bodies)

    def linked_prs(self, issue_number: int) -> Sequence[LinkedPr]:
        return self.linked.get(issue_number, [])

    def open_pr_bodies(self) -> Sequence[str]:
        return self.bodies


def issue(n: int, created: str = "2026-01-01", assignee: str = "me", label: str = "sisyphus") -> Candidate:
    return Candidate(f"#{n}", f"t{n}", n, created, (assignee,), (label,), "open")


def lin(ref: str, assignee: str = "me@x.io", state: str = "Todo") -> Candidate:
    return Candidate(ref, ref, None, "", (assignee,), (), state)


def refs(items: Sequence[Candidate]) -> list[str]:
    return [c.ref for c in items]


def run(src: WorkItemSource, cands: Sequence[Candidate], ws: Path, limit: int = 5, lookup: FakeLookup | None = None):
    return pick_items(src, FakeTracker(cands), lookup or FakeLookup(), workspace_dir=ws, limit=limit)


def test_linear_keeps_tracker_order(tmp_path: Path) -> None:
    assert refs(run(LIN, [lin("ENG-3"), lin("ENG-1"), lin("ENG-2")], tmp_path)) == ["ENG-3", "ENG-1", "ENG-2"]


def test_github_oldest_first_then_number(tmp_path: Path) -> None:
    cands = [issue(5, "2026-02-01"), issue(9, "2026-01-01"), issue(3, "2026-01-01")]
    assert refs(run(GH, cands, tmp_path)) == ["#3", "#9", "#5"]


def test_assignee_and_label_filters(tmp_path: Path) -> None:
    cands = [issue(1, assignee="other"), issue(2, label="bug"), issue(3, assignee="ME")]
    assert refs(run(GH, cands, tmp_path)) == ["#3"]


def test_linear_filters_assignee_and_state(tmp_path: Path) -> None:
    cands = [lin("A-1", assignee="x@y.z"), lin("A-2", state="Done"), lin("A-3")]
    assert refs(run(LIN, cands, tmp_path)) == ["A-3"]


def test_closed_github_issue_is_not_picked(tmp_path: Path) -> None:
    closed = Candidate("#1", "", 1, "2026-01-01", ("me",), ("sisyphus",), "closed")
    assert run(GH, [closed], tmp_path) == []


def test_open_linked_pr_excludes_but_merged_or_closed_do_not(tmp_path: Path) -> None:
    lookup = FakeLookup({1: [LinkedPr(10, "OPEN")], 2: [LinkedPr(11, "MERGED")], 3: [LinkedPr(12, "CLOSED")]})
    assert refs(run(GH, [issue(1), issue(2), issue(3)], tmp_path, lookup=lookup)) == ["#2", "#3"]


def test_hash_n_in_open_pr_body_only_excludes(tmp_path: Path) -> None:
    lookup = FakeLookup(bodies=["Fixes #7 and more", "see #12345", "owner/repo#8"])
    assert refs(run(GH, [issue(7), issue(8), issue(123), issue(12)], tmp_path, lookup=lookup)) == ["#8", "#12", "#123"]


def test_mentions_issue_boundaries() -> None:
    assert mentions_issue("closes #12.", 12)
    assert mentions_issue("#12", 12)
    assert not mentions_issue("#123", 12)
    assert not mentions_issue("a#12", 12)
    assert not mentions_issue("", 12)


def test_open_pr_check_is_swappable(tmp_path: Path) -> None:
    got = pick_items(
        GH, FakeTracker([issue(1), issue(2)]), FakeLookup(), workspace_dir=tmp_path, limit=5,
        open_pr_check=lambda c, _l: c.number == 1,
    )  # fmt: skip
    assert refs(got) == ["#2"]
    assert default_open_pr_check(Candidate("x"), FakeLookup()) is False


def test_started_items_skipped_and_limit_caps(tmp_path: Path) -> None:
    RunRecord.open(tmp_path, "ENG-1")
    assert refs(run(LIN, [lin("ENG-1"), lin("ENG-2"), lin("ENG-3")], tmp_path, limit=1)) == ["ENG-2"]
    assert refs(run(LIN, [lin("ENG-1"), lin("ENG-2"), lin("ENG-3")], tmp_path, limit=5)) == ["ENG-2", "ENG-3"]
    assert run(LIN, [lin("ENG-2")], tmp_path, limit=0) == []


def test_github_without_lookup_and_bad_source(tmp_path: Path) -> None:
    with pytest.raises(PickError):
        pick_items(GH, FakeTracker([issue(1)]), None, workspace_dir=tmp_path, limit=1)
    for bad in (None, WorkItemSource(), WorkItemSource(kind="github"), WorkItemSource(kind="linear", assignee="a")):
        with pytest.raises(PickError):
            pick_items(bad, FakeTracker([]), FakeLookup(), workspace_dir=tmp_path, limit=1)


# ------------------------------------------------------- awaiting review


def _record(ws: Path, item: str, stage: str, created: bool = True) -> None:
    rec = RunRecord.open(ws, item)
    rec.mark_stage(stage, "complete")
    if created:
        rec.increment_retry(BRANCH_CREATED_KEY)


def test_awaiting_review_found_for_open_pr(tmp_path: Path) -> None:
    _record(tmp_path, "ENG-1", OPEN_PR_STAGE)
    _record(tmp_path, "ENG-2", OPEN_PR_STAGE)  # PR merged: no open PR
    _record(tmp_path, "ENG-3", "build")  # never reached the PR stage
    _record(tmp_path, "ENG-4", OPEN_PR_STAGE, created=False)
    (tmp_path / "ENG-1.json.stop.json").write_text("{}")
    (tmp_path / "junk.json").write_text("not json")
    (tmp_path / "other.json").write_text(json.dumps({"item": "ENG-9"}))
    seen: list[str] = []

    def find(branch: str) -> PullRequest | None:
        seen.append(branch)
        return PullRequest(5, "u") if branch == branch_name("ENG-1") else None

    got = find_awaiting_review(tmp_path, find)
    assert [(a.item, a.pr.number) for a in got] == [("ENG-1", 5)]
    assert sorted(seen) == sorted([branch_name("ENG-1"), branch_name("ENG-2")])


def test_awaiting_review_excludes_checkpointed_record_without_lookup(tmp_path: Path) -> None:
    _record(tmp_path, "ENG-1", OPEN_PR_STAGE)
    rec = RunRecord.open(tmp_path, "ENG-1")
    rec.set_checkpoint("C7")
    seen: list[str] = []

    def find(branch: str) -> PullRequest | None:
        seen.append(branch)
        return PullRequest(5, "u")

    assert find_awaiting_review(tmp_path, find) == []
    assert seen == []


@pytest.mark.parametrize("stage", [CI_FIX_STAGE, MERGE_STAGE])
def test_awaiting_review_includes_later_pr_stages(tmp_path: Path, stage: str) -> None:
    _record(tmp_path, "ENG-1", stage)
    got = find_awaiting_review(tmp_path, lambda _b: PullRequest(5, "u"))
    assert [(a.item, a.pr.number) for a in got] == [("ENG-1", 5)]


def test_awaiting_review_missing_workspace(tmp_path: Path) -> None:
    assert find_awaiting_review(tmp_path / "nope", lambda _b: None) == []


# --------------------------------------------------------- real clients (fakes)


class FakeExec:
    def __init__(self, replies: list[ExecResult]) -> None:
        self.replies = replies
        self.calls: list[tuple[list[str], Path | None, Mapping[str, str] | None]] = []

    def __call__(self, argv: Sequence[str], cwd: Path | None, env: Mapping[str, str] | None) -> ExecResult:
        self.calls.append((list(argv), cwd, env))
        return self.replies.pop(0)


def test_github_issue_tracker_parses_gh_output(tmp_path: Path) -> None:
    rows = [
        {
            "number": 4,
            "title": "T",
            "createdAt": "2026-01-01",
            "assignees": [{"login": "me"}],
            "labels": [{"name": "sisyphus"}],
        }
    ]
    ex = FakeExec([ExecResult(0, json.dumps(rows))])
    got = GitHubIssueTracker(GhCli(tmp_path, "tok", ex)).list_candidates(GH)
    assert got == [Candidate("#4", "T", 4, "2026-01-01", ("me",), ("sisyphus",), "open")]
    argv, cwd, env = ex.calls[0]
    assert argv[:3] == ["gh", "issue", "list"]
    assert "--assignee" in argv and "me" in argv and "sisyphus" in argv
    assert cwd == tmp_path and env == {"GH_TOKEN": "tok"}


def test_gh_failure_and_bad_json_raise(tmp_path: Path) -> None:
    with pytest.raises(PickError, match="exit 1"):
        GhCli(None, None, FakeExec([ExecResult(1, "", "boom")])).json("issue", "list")
    with pytest.raises(PickError, match="invalid JSON"):
        GhCli(None, None, FakeExec([ExecResult(0, "{")])).json("issue", "list")


def test_gh_pull_request_lookup() -> None:
    ex = FakeExec([
        ExecResult(0, json.dumps({"closedByPullRequestsReferences": [{"number": 8}]})),
        ExecResult(0, json.dumps({"number": 8, "state": "MERGED"})),
        ExecResult(0, json.dumps([{"body": "Fixes #1"}, {"body": None}])),
    ])
    lookup = GhPullRequestLookup(GhCli(None, None, ex))
    assert lookup.linked_prs(3) == [LinkedPr(8, "MERGED")]
    assert lookup.open_pr_bodies() == ["Fixes #1", ""]


def _linear_post(nodes: list[dict], seen: list[tuple[str, dict, Mapping[str, str]]]):
    def post(url: str, payload: bytes, headers: Mapping[str, str]) -> bytes:
        seen.append((url, json.loads(payload), headers))
        return json.dumps({"data": {"issues": {"nodes": nodes}}}).encode()

    return post


def test_linear_tracker_orders_by_priority_and_builds_query() -> None:
    nodes = [
        {
            "identifier": "A-1",
            "priority": 0,
            "sortOrder": 1,
            "state": {"name": "Todo"},
            "assignee": {"email": "me@x.io"},
        },
        {
            "identifier": "A-2",
            "priority": 3,
            "sortOrder": 2,
            "state": {"name": "Todo"},
            "assignee": {"email": "me@x.io"},
        },
        {
            "identifier": "A-3",
            "priority": 1,
            "sortOrder": 3,
            "state": {"name": "Todo"},
            "assignee": {"email": "me@x.io"},
        },
    ]
    seen: list = []
    got = LinearTracker("key", _linear_post(nodes, seen)).list_candidates(LIN)
    assert refs(got) == ["A-3", "A-2", "A-1"]
    url, body, headers = seen[0]
    assert url == pick.LINEAR_API_URL and headers["Authorization"] == "key"
    assert body["variables"] == {"state": "Todo", "assignee": {"email": {"eq": "me@x.io"}}}
    assert got[0].state == "Todo" and "me@x.io" in got[0].assignees


def test_linear_tracker_errors() -> None:
    with pytest.raises(PickError, match="API key"):
        LinearTracker("")

    def bad(_u: str, _p: bytes, _h: Mapping[str, str]) -> bytes:
        return json.dumps({"errors": ["nope"]}).encode()

    with pytest.raises(PickError, match="errors"):
        LinearTracker("k", bad).list_candidates(LIN)

    def down(_u: str, _p: bytes, _h: Mapping[str, str]) -> bytes:
        raise OSError("offline")

    with pytest.raises(PickError, match="offline"):
        LinearTracker("k", down).list_candidates(WorkItemSource(kind="linear", assignee="Me", pick_state="Todo"))


def test_no_harness_or_spec_prism_flow_import() -> None:
    tree = ast.parse(Path(pick.__file__).read_text())
    names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(n.split(".")[0] in ("harness", "spec_prism_flow") for n in names)


def test_gh_cli_run_returns_the_raw_result_with_cwd_and_env(tmp_path: Path) -> None:
    ex = FakeExec([ExecResult(1, "out", "err")])
    res = GhCli(tmp_path, "tok", ex).run("pr", "create")
    assert res == ExecResult(1, "out", "err")
    assert ex.calls == [(["gh", "pr", "create"], tmp_path, {"GH_TOKEN": "tok"})]
