from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sisyphus.config import LimitsConfig
from sisyphus.rails import (
    Checkpoint,
    LimitTracker,
    MergeGateFailureResult,
    PushRequest,
    RetryBound,
    ScopeViolationResult,
    parse_push_argv,
    push_guard,
)

CREATED = {"sisyphus/eng-1"}


def guard(req: PushRequest):
    return push_guard(req, "main", CREATED)


def test_allows_created_branch() -> None:
    d = guard(PushRequest("origin", "sisyphus/eng-1"))
    assert d.allowed
    assert guard(PushRequest("origin", "HEAD:refs/heads/sisyphus/eng-1")).allowed


@pytest.mark.parametrize("flag", ["--force", "-f", "--force-with-lease", "--force-with-lease=x:y", "-fu"])
def test_rejects_force_flags(flag: str) -> None:
    d = guard(PushRequest("origin", "sisyphus/eng-1", (flag,)))
    assert not d.allowed
    assert "force" in d.reason


@pytest.mark.parametrize(
    "flag",
    [
        "--force-if-includes",
        "--mirror",
        "--prune",
        "--del",
        "--dele",
        "--force-with",
        "--force-if",
        "--mir",
        "--pru",
    ],
)
def test_rejects_full_and_abbreviated_long_flags(flag: str) -> None:
    # git accepts unambiguous option prefixes; the allowlist must reject them all.
    assert not guard(PushRequest("origin", "sisyphus/eng-1", (flag,))).allowed


def test_rejects_plus_refspec() -> None:
    assert not guard(PushRequest("origin", "+sisyphus/eng-1")).allowed


def test_rejects_history_rewrite() -> None:
    assert not guard(PushRequest("origin", "sisyphus/eng-1", history_rewrite=True)).allowed
    assert not guard(PushRequest("origin", ":sisyphus/eng-1")).allowed
    assert not guard(PushRequest("origin", "sisyphus/eng-1", ("--delete",))).allowed


def test_rejects_default_and_uncreated() -> None:
    assert not guard(PushRequest("origin", "main")).allowed
    assert not guard(PushRequest("origin", "HEAD:refs/heads/main")).allowed
    d = guard(PushRequest("origin", "feature/other"))
    assert not d.allowed
    assert "not created" in d.reason


def test_rebase_needed_maps_to_c7() -> None:
    d = guard(PushRequest("origin", "sisyphus/eng-1", needs_rebase=True))
    assert not d.allowed
    assert d.checkpoint is Checkpoint.C7


def test_parse_push_argv() -> None:
    r = parse_push_argv(
        ["git", "push", "--force", "origin", "sisyphus/eng-1"], history_rewrite=False, needs_rebase=False
    )
    assert r == PushRequest("origin", "sisyphus/eng-1", ("--force",))
    assert not guard(r).allowed
    assert parse_push_argv(["origin", "x"], history_rewrite=False, needs_rebase=False).refspec == "x"
    with pytest.raises(ValueError, match="expected"):
        parse_push_argv(["push", "origin"], history_rewrite=False, needs_rebase=False)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_time_limit_boundary() -> None:
    clock = Clock()
    tr = LimitTracker(LimitsConfig(), clock)
    clock.t = 179 * 60 + 59
    assert not tr.status().tripped
    clock.t = 180 * 60
    s = tr.status()
    assert s.reached == ("max_run_minutes",)
    assert s.checkpoint is Checkpoint.C6


def test_ticket_limit_boundary() -> None:
    tr = LimitTracker(LimitsConfig(max_tickets_per_run=2), Clock())
    tr.record_ticket()
    assert not tr.status().tripped
    tr.record_ticket()
    assert tr.status().reached == ("max_tickets_per_run",)


def test_leaf_limit_boundary() -> None:
    tr = LimitTracker(clock=Clock())
    for _ in range(3):
        tr.record_leaf()
    assert not tr.status().tripped
    tr.record_leaf()
    s = tr.status()
    assert s.reached == ("max_leaves_per_run",)
    assert s.checkpoint is Checkpoint.C6


def test_fix_attempt_limit_boundary_per_pr() -> None:
    tr = LimitTracker(clock=Clock())
    tr.record_fix_attempt("1")
    tr.record_fix_attempt("1")
    tr.record_fix_attempt("2")
    assert not tr.status().tripped
    assert tr.record_fix_attempt("1") == 3
    assert tr.status().reached == ("max_fix_attempts_per_pr",)


def test_retry_bound_escalates() -> None:
    rb = RetryBound()
    assert not rb.result().escalate
    assert not rb.record_failure().escalate
    assert not rb.record_failure().escalate
    r = rb.record_failure()
    assert r.escalate
    assert r.attempts == 3
    assert r.checkpoint is Checkpoint.C5
    assert RetryBound(1).record_failure().escalate


def test_result_types_map_to_c5() -> None:
    assert ScopeViolationResult(("a.py",), "x").checkpoint is Checkpoint.C5
    assert MergeGateFailureResult("ci").checkpoint is Checkpoint.C5


def test_checkpoints_c1_to_c8() -> None:
    assert [c.value for c in Checkpoint] == [f"C{i}" for i in range(1, 9)]


def test_no_harness_imports() -> None:
    for name in ("paths", "lock", "rails"):
        src = (Path(__file__).parent.parent / "sisyphus" / f"{name}.py").read_text()
        for node in ast.walk(ast.parse(src)):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for m in mods:
                assert m.split(".")[0] not in ("harness", "spec_prism_flow")


@pytest.mark.parametrize("flag", ["--force-with", "--force-i", "--dele", "--prun", "--mirr", "--unknown", "-x"])
def test_rejects_abbreviated_and_unknown_flags(flag: str) -> None:
    req = parse_push_argv(["git", "push", flag, "origin", "sisyphus/eng-1"], history_rewrite=False, needs_rebase=False)
    assert not guard(req).allowed


@pytest.mark.parametrize("flag", ["-u", "--set-upstream", "--no-verify"])
def test_allows_known_flags(flag: str) -> None:
    assert guard(PushRequest("origin", "sisyphus/eng-1", (flag,))).allowed
