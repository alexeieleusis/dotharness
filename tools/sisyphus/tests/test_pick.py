"""Picker tests: trackers, PR lookups and ``gh``/Linear transports are all fakes. No network, no real gh."""

from __future__ import annotations

import ast
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from sisyphus import pick
from sisyphus.config import WorkItemSource
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


def test_draft_linked_pr_is_still_open_for_pick(tmp_path: Path) -> None:
    ex = FakeExec([
        ExecResult(0, json.dumps({"closedByPullRequestsReferences": [{"number": 8}]})),
        ExecResult(0, json.dumps({"number": 8, "state": "OPEN", "isDraft": True})),
    ])
    linked = GhPullRequestLookup(GhCli(None, None, ex)).linked_prs(1)
    assert linked == [LinkedPr(8, "OPEN")]
    assert refs(run(GH, [issue(1), issue(2)], tmp_path, lookup=FakeLookup({1: linked}))) == ["#2"]


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


def test_archived_github_item_is_not_repicked(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    RunRecord.open(archive, "#1")
    got = pick_items(GH, FakeTracker([issue(1), issue(2)]), FakeLookup(), workspace_dir=tmp_path / "ws", limit=5,
                     archive_dir=archive)  # fmt: skip
    assert refs(got) == ["#2"]


def test_archived_file_item_is_not_repicked(tmp_path: Path) -> None:
    write_ticket(tmp_path / "in", "A-1", mtime=1)
    write_ticket(tmp_path / "in", "A-2", mtime=2)
    archive = tmp_path / "archive"
    RunRecord.open(archive, "A-1")
    picked = pick_items(file_src(tmp_path / "in"), pick.FileTracker(), None, workspace_dir=tmp_path / "ws", limit=5,
                        archive_dir=archive)  # fmt: skip
    assert refs(picked) == ["A-2"]


def test_missing_or_none_archive_dir_picks_normally(tmp_path: Path) -> None:
    cands = [lin("ENG-1"), lin("ENG-2")]
    for archive in (tmp_path / "nope" / "archive", None):
        got = pick_items(LIN, FakeTracker(cands), FakeLookup(), workspace_dir=tmp_path / "ws", limit=5,
                         archive_dir=archive)  # fmt: skip
        assert refs(got) == ["ENG-1", "ENG-2"]
    assert not (tmp_path / "nope").exists()
    assert not pick.is_started(tmp_path / "ws", "ENG-1", tmp_path / "nope")


def test_is_started_checks_workspace_and_archive_only(tmp_path: Path) -> None:
    ws, archive = tmp_path / "ws", tmp_path / "archive"
    assert not pick.is_started(ws, "X-1", archive)
    RunRecord.open(archive, "X-1")
    assert pick.is_started(ws, "X-1", archive)
    assert not pick.is_started(ws, "X-1")
    RunRecord.open(ws, "X-2")
    assert pick.is_started(ws, "X-2", archive)
    assert not ws.joinpath("X-1.json").exists()


def test_github_without_lookup_and_bad_source(tmp_path: Path) -> None:
    with pytest.raises(PickError):
        pick_items(GH, FakeTracker([issue(1)]), None, workspace_dir=tmp_path, limit=1)
    for bad in (None, WorkItemSource(), WorkItemSource(kind="github"), WorkItemSource(kind="linear", assignee="a")):
        with pytest.raises(PickError):
            pick_items(bad, FakeTracker([]), FakeLookup(), workspace_dir=tmp_path, limit=1)


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


@pytest.mark.parametrize("raw", [b'{"data": null}', b"null", b"[]", b'{"data": {"issues": null}}'])
def test_linear_tracker_malformed_body_raises_pick_error(raw: bytes) -> None:
    with pytest.raises(PickError):
        LinearTracker("k", lambda _u, _p, _h: raw).list_candidates(LIN)


def test_linear_tracker_display_name_assignee_filter() -> None:
    seen: list = []
    src = WorkItemSource(kind="linear", assignee="Me", pick_state="Todo")
    LinearTracker("k", _linear_post([], seen)).list_candidates(src)
    assert seen[0][1]["variables"]["assignee"] == {"displayName": {"eq": "Me"}}


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


# ------------------------------------------------------------------ FileTracker


NO_LOOKUP = "no PR lookup for file"


def write_ticket(inbox: Path, ref: str, *, priority: str | None = None, title: str | None = "T", mtime: int = 1000,
                 front: str | None = None, body: str = "body\n") -> Path:  # fmt: skip
    inbox.mkdir(parents=True, exist_ok=True)
    if front is None:
        lines = [f"ref: {ref}"]
        if title is not None:
            lines.append(f"title: {title}")
        if priority is not None:
            lines.append(f"priority: {priority}")
        front = "---\n" + "\n".join(lines) + "\n---\n"
    path = inbox / f"{ref}.md"
    path.write_text(front + body)
    os.utime(path, (mtime, mtime))
    return path


def file_src(inbox: Path | str) -> WorkItemSource:
    return WorkItemSource(kind="file", dir=str(inbox))


def test_file_tracker_candidate_fields(tmp_path: Path) -> None:
    write_ticket(tmp_path, "ENG-1", title="Fix: the thing", mtime=1_700_000_000)
    (cand,) = pick.FileTracker().list_candidates(file_src(tmp_path))
    assert cand.ref == "ENG-1"
    assert cand.title == "Fix: the thing"
    assert cand.created_at == "2023-11-14T22:13:20.000000Z"


def test_file_tracker_order_priority_then_mtime_then_ref(tmp_path: Path) -> None:
    write_ticket(tmp_path, "A-none", mtime=1)
    write_ticket(tmp_path, "B-p4", priority="4", mtime=2)
    write_ticket(tmp_path, "C-p1-late", priority="1", mtime=30)
    write_ticket(tmp_path, "D-p1-early", priority="1", mtime=20)
    write_ticket(tmp_path, "F-p2-b", priority="2", mtime=5)
    write_ticket(tmp_path, "E-p2-a", priority="2", mtime=5)
    write_ticket(tmp_path, "G-p0", priority="0", mtime=0)
    refs = [c.ref for c in pick.FileTracker().list_candidates(file_src(tmp_path))]
    assert refs == ["D-p1-early", "C-p1-late", "E-p2-a", "F-p2-b", "B-p4", "G-p0", "A-none"]


def test_file_tracker_mtime_sorts_numerically(tmp_path: Path) -> None:
    write_ticket(tmp_path, "OLD", mtime=999)
    write_ticket(tmp_path, "NEW", mtime=1_000_000)
    cands = pick.FileTracker().list_candidates(file_src(tmp_path))
    assert [c.ref for c in cands] == ["OLD", "NEW"]
    assert cands[0].created_at < cands[1].created_at


@pytest.mark.parametrize("raw", ["x", "5", "-1", "2.5", ""])
def test_file_tracker_bad_priority_is_zero(tmp_path: Path, raw: str) -> None:
    write_ticket(tmp_path, "BAD", priority=raw, mtime=1)
    write_ticket(tmp_path, "P4", priority="4", mtime=2)
    assert [c.ref for c in pick.FileTracker().list_candidates(file_src(tmp_path))] == ["P4", "BAD"]


def test_file_tracker_title_falls_back_to_ref(tmp_path: Path) -> None:
    write_ticket(tmp_path, "NT", title=None)
    write_ticket(tmp_path, "ET", title="")
    assert {c.title for c in pick.FileTracker().list_candidates(file_src(tmp_path))} == {"NT", "ET"}


def test_file_tracker_title_keeps_colons_and_body_is_not_parsed(tmp_path: Path) -> None:
    write_ticket(tmp_path, "X-1", title="a: b: c", body="ref: EVIL\npriority: 1\n---\n")
    (cand,) = pick.FileTracker().list_candidates(file_src(tmp_path))
    assert (cand.ref, cand.title) == ("X-1", "a: b: c")


@pytest.mark.parametrize(
    ("name", "front", "reason"),
    [
        ("NOOPEN", "ref: NOOPEN\n---\n", "missing opening ---"),
        ("NOCLOSE", "---\nref: NOCLOSE\ntitle: t\n", "missing closing ---"),
        ("NOREF", "---\ntitle: t\n---\n", "missing ref"),
        ("EMPTY", "", "missing opening ---"),
        ("MISMATCH", "---\nref: OTHER\n---\n", "does not match the file name"),
        ("bad ref", "---\nref: bad ref\n---\n", "invalid ref"),
        ("..", "---\nref: ..\n---\n", "invalid ref"),
        (".", "---\nref: .\n---\n", "invalid ref"),
        ("a$b", "---\nref: a$b\n---\n", "invalid ref"),
    ],
)
def test_file_tracker_skips_invalid_files_and_notes_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, front: str, reason: str
) -> None:
    write_ticket(tmp_path, "GOOD", mtime=1)
    (tmp_path / f"{name}.md").write_text(front)
    assert [c.ref for c in pick.FileTracker().list_candidates(file_src(tmp_path))] == ["GOOD"]
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert f"{name}.md" in err
    assert reason in err


def test_file_tracker_ignores_non_markdown_and_directories(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_ticket(tmp_path, "OK")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "sub.md").mkdir()
    assert [c.ref for c in pick.FileTracker().list_candidates(file_src(tmp_path))] == ["OK"]
    assert capsys.readouterr().err == ""


def test_file_tracker_missing_or_empty_dir_returns_nothing(tmp_path: Path) -> None:
    tracker = pick.FileTracker()
    assert tracker.list_candidates(file_src(tmp_path / "nope")) == []
    assert tracker.list_candidates(file_src(tmp_path)) == []
    assert tracker.list_candidates(WorkItemSource(kind="file")) == []
    assert tracker.list_candidates(file_src("")) == []


def test_file_tracker_unreadable_dir_raises(tmp_path: Path) -> None:
    afile = tmp_path / "afile"
    afile.write_text("x")
    with pytest.raises(PickError, match="cannot read ticket directory"):
        pick.FileTracker().list_candidates(file_src(afile))


def test_file_tracker_expands_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    write_ticket(tmp_path / "inbox", "T-1")
    assert [c.ref for c in pick.FileTracker().list_candidates(file_src("~/inbox"))] == ["T-1"]


def test_pick_items_file_needs_only_dir(tmp_path: Path) -> None:
    write_ticket(tmp_path / "in", "A-1", mtime=1)
    write_ticket(tmp_path / "in", "A-2", mtime=2)
    picked = pick_items(file_src(tmp_path / "in"), pick.FileTracker(), None, workspace_dir=tmp_path / "ws", limit=5)
    assert [c.ref for c in picked] == ["A-1", "A-2"]


def test_pick_items_file_requires_dir() -> None:
    with pytest.raises(PickError, match="dir is required"):
        pick_items(WorkItemSource(kind="file"), FakeTracker([]), None, workspace_dir="ws", limit=1)


def test_pick_items_file_skips_started_and_never_looks_up_prs(tmp_path: Path) -> None:
    write_ticket(tmp_path / "in", "A-1", mtime=1)
    write_ticket(tmp_path / "in", "A-2", mtime=2)
    ws = tmp_path / "ws"
    RunRecord.open(ws, "A-1")

    class Boom:
        def linked_prs(self, issue_number: int) -> Sequence[LinkedPr]:
            raise AssertionError(NO_LOOKUP)

        def open_pr_bodies(self) -> Sequence[str]:
            raise AssertionError(NO_LOOKUP)

    picked = pick_items(file_src(tmp_path / "in"), pick.FileTracker(), Boom(), workspace_dir=ws, limit=5,
                        open_pr_check=lambda *_: True)  # fmt: skip
    assert [c.ref for c in picked] == ["A-2"]


def test_eligible_file_ignores_assignee_state_and_label() -> None:
    src = WorkItemSource(kind="file", dir="d", assignee="x", pick_state="Done", pick_label="l")
    assert pick._eligible(Candidate("A-1"), src)


def test_file_tracker_code_imports_no_harness() -> None:
    tree = ast.parse(Path(pick.__file__).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FileTracker")
    for node in ast.walk(cls):
        assert not isinstance(node, ast.Import | ast.ImportFrom)
