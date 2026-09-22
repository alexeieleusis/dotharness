"""Tests for `eligible_leaves`/`run_parallel`."""

import threading
import time
from datetime import UTC, datetime

from conftest import git_commit as _commit
from conftest import init_git_repo as _init_repo
from conftest import make_completion_record as _record
from conftest import make_config as _config
from conftest import make_phase as _phase
from conftest import write_completion_log
from conftest import write_phase_file as _write_phase_file

from spec_prism_flow.build import phase_runner
from spec_prism_flow.build.completion_log import load_all
from spec_prism_flow.build.errors import EmptyImplementationError
from spec_prism_flow.build.parallel_runner import eligible_leaves, run_parallel
from spec_prism_flow.build.phase_runner import PhaseRunResult
from spec_prism_flow.build.track_runner import COMPLETION_LOG_JSON_RELPATH
from spec_prism_flow.decompose import GRAPH_FILENAME
from spec_prism_flow.graph import Graph, write_graph
from spec_prism_flow.phase_file import phase_file_stem

# --- eligible_leaves --------------------------------------------------------------


def test_eligible_leaves_no_dependencies_is_always_eligible_when_unmerged():
    graph = Graph(nodes=["01-a-leaf"], edges=[])
    phases = [_phase(number=1, name="a")]

    assert eligible_leaves(graph, [], phases) == phases


def test_eligible_leaves_excludes_already_merged():
    graph = Graph(nodes=["01-a-leaf"], edges=[])
    phases = [_phase(number=1, name="a")]
    completion_log = [_record(phase_number=1)]

    assert eligible_leaves(graph, completion_log, phases) == []


def test_eligible_leaves_all_dependencies_merged_is_eligible():
    graph = Graph(nodes=["01-a-leaf", "02-b-leaf"], edges=[("02-b-leaf", "01-a-leaf")])
    phases = [_phase(number=1, name="a"), _phase(number=2, name="b")]
    completion_log = [_record(phase_number=1)]

    result = eligible_leaves(graph, completion_log, phases)

    assert [p.number for p in result] == [2]


def test_eligible_leaves_partial_dependency_merge_is_not_eligible():
    graph = Graph(
        nodes=["01-a-leaf", "02-b-leaf", "03-c-leaf"],
        edges=[("03-c-leaf", "01-a-leaf"), ("03-c-leaf", "02-b-leaf")],
    )
    phases = [_phase(number=1, name="a"), _phase(number=2, name="b"), _phase(number=3, name="c")]
    completion_log = [_record(phase_number=1)]  # only one of leaf 3's two dependencies merged

    result = eligible_leaves(graph, completion_log, phases)

    assert [p.number for p in result] == [2]  # leaf 3 not eligible; leaf 2 (no deps) is


# --- run_parallel ------------------------------------------------------------------


def _write_graph(phase_dir, nodes, edges) -> None:
    write_graph(Graph(nodes=nodes, edges=edges), phase_dir / GRAPH_FILENAME)


def _init_clone(tmp_path):
    """A real, committed git repo -- `run_parallel` now shells out to `git worktree
    add`/`git fetch` against `clone` itself (not just the mocked-out `run_phase`), so
    `clone` needs to be a real repo with at least one commit for those calls to
    succeed (`git worktree add` refuses an unborn HEAD)."""
    clone = _init_repo(tmp_path, dirname="clone")
    (clone / "README.md").write_text("init\n")
    _commit(clone, "initial")
    return clone


def _merging_run_phase(log_path, lock, *, delay=0.0, escalate_numbers=frozenset(), calls=None):
    """A `phase_runner.run_phase` stand-in that, on success, appends a merged
    completion record to `log_path` under `lock` -- mirroring what the real
    `run_phase` does via `completion_log.append_and_commit`, since `run_parallel`'s
    eligibility re-polling depends entirely on that on-disk log, not on any
    in-process bookkeeping."""

    def run_phase(clone, config, phase, *, dry_run=False, strict=False):
        if calls is not None:
            with lock:
                calls.append((phase.number, time.monotonic(), "start"))
        time.sleep(delay)
        if calls is not None:
            with lock:
                calls.append((phase.number, time.monotonic(), "end"))

        if phase.number in escalate_numbers:
            raise EmptyImplementationError()

        record = _record(
            phase_number=phase.number,
            phase_name=phase.name,
            pr_merged_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        with lock:
            existing = load_all(log_path)
            write_completion_log(log_path, [*existing, record])
        return PhaseRunResult(phase_number=phase.number, merged=True, completion_record=record)

    return run_phase


def test_run_parallel_workers_1_linear_chain_runs_in_dependency_order(tmp_path, monkeypatch):
    config = _config(tmp_path)
    phases = [_phase(number=n, name=letter) for n, letter in ((1, "a"), (2, "b"), (3, "c"))]
    for phase in phases:
        _write_phase_file(config.plan.phase_dir, phase)
    _write_graph(
        config.plan.phase_dir,
        nodes=[phase_file_stem(p.number, p.name) for p in phases],
        edges=[("02-b-leaf", "01-a-leaf"), ("03-c-leaf", "02-b-leaf")],
    )
    clone = _init_clone(tmp_path)
    log_path = clone / COMPLETION_LOG_JSON_RELPATH
    lock = threading.Lock()
    calls: list[tuple[int, float, str]] = []
    monkeypatch.setattr(phase_runner, "run_phase", _merging_run_phase(log_path, lock, calls=calls))

    results = run_parallel(config, clone, workers=1)

    call_order = [number for number, _, event in calls if event == "start"]
    assert call_order == [1, 2, 3]
    assert sorted(r.phase_number for r in results) == [1, 2, 3]


def test_run_parallel_runs_independent_leaves_concurrently_up_to_workers(tmp_path, monkeypatch):
    config = _config(tmp_path)
    phases = [_phase(number=1, name="a"), _phase(number=2, name="b")]
    for phase in phases:
        _write_phase_file(config.plan.phase_dir, phase)
    _write_graph(config.plan.phase_dir, nodes=["01-a-leaf", "02-b-leaf"], edges=[])
    clone = _init_clone(tmp_path)
    log_path = clone / COMPLETION_LOG_JSON_RELPATH
    lock = threading.Lock()
    calls: list[tuple[int, float, str]] = []
    monkeypatch.setattr(phase_runner, "run_phase", _merging_run_phase(log_path, lock, delay=0.05, calls=calls))

    results = run_parallel(config, clone, workers=2)

    assert sorted(r.phase_number for r in results) == [1, 2]
    starts = {number: t for number, t, event in calls if event == "start"}
    ends = {number: t for number, t, event in calls if event == "end"}
    # Both leaves' [start, end] windows overlap -- proof they ran concurrently.
    assert starts[1] < ends[2]
    assert starts[2] < ends[1]


def test_run_parallel_with_workers_1_runs_independent_leaves_serially(tmp_path, monkeypatch):
    config = _config(tmp_path)
    phases = [_phase(number=1, name="a"), _phase(number=2, name="b")]
    for phase in phases:
        _write_phase_file(config.plan.phase_dir, phase)
    _write_graph(config.plan.phase_dir, nodes=["01-a-leaf", "02-b-leaf"], edges=[])
    clone = _init_clone(tmp_path)
    log_path = clone / COMPLETION_LOG_JSON_RELPATH
    lock = threading.Lock()
    calls: list[tuple[int, float, str]] = []
    monkeypatch.setattr(phase_runner, "run_phase", _merging_run_phase(log_path, lock, delay=0.05, calls=calls))

    run_parallel(config, clone, workers=1)

    starts = {number: t for number, t, event in calls if event == "start"}
    ends = {number: t for number, t, event in calls if event == "end"}
    # No overlap: the second leaf only starts once the first has already ended.
    assert starts[2] >= ends[1] or starts[1] >= ends[2]


def test_run_parallel_continues_unrelated_branch_after_escalation(tmp_path, monkeypatch):
    config = _config(tmp_path)
    # Branch A: leaf 1 -> leaf 2 (leaf 2 depends on leaf 1). Branch B: leaf 3, unrelated.
    phases = [_phase(number=1, name="a"), _phase(number=2, name="b"), _phase(number=3, name="c")]
    for phase in phases:
        _write_phase_file(config.plan.phase_dir, phase)
    _write_graph(
        config.plan.phase_dir,
        nodes=["01-a-leaf", "02-b-leaf", "03-c-leaf"],
        edges=[("02-b-leaf", "01-a-leaf")],
    )
    clone = _init_clone(tmp_path)
    log_path = clone / COMPLETION_LOG_JSON_RELPATH
    lock = threading.Lock()
    calls: list[tuple[int, float, str]] = []
    monkeypatch.setattr(
        phase_runner, "run_phase", _merging_run_phase(log_path, lock, escalate_numbers={1}, calls=calls)
    )

    results = run_parallel(config, clone, workers=2)

    called_numbers = {number for number, _, event in calls if event == "start"}
    assert called_numbers == {1, 3}  # leaf 2 never submitted -- blocked by leaf 1's escalation
    assert [r.phase_number for r in results] == [3]
