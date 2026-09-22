from __future__ import annotations

from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import TYPE_CHECKING

from spec_prism_flow.build import phase_runner
from spec_prism_flow.build.completion_log import CompletionRecord, load_all
from spec_prism_flow.build.errors import OrchestrationError
from spec_prism_flow.build.phase_runner import PhaseRunResult
from spec_prism_flow.build.track_runner import (
    COMPLETION_LOG_JSON_RELPATH,
    discover_phase_files,
    merged_phase_numbers,
)
from spec_prism_flow.decompose import GRAPH_FILENAME
from spec_prism_flow.graph import Graph, load_graph, reachable_from_all
from spec_prism_flow.phase_file import PhaseFile, parse_phase_file, phase_file_stem, phase_number_from_stem

if TYPE_CHECKING:
    from spec_prism_flow.config import SpecPrismFlowConfig


def eligible_leaves(
    graph: Graph, completion_log: list[CompletionRecord], phase_files: list[PhaseFile]
) -> list[PhaseFile]:
    """A leaf is eligible iff it hasn't merged yet and every dependency edge from its
    graph node points to a node whose phase has merged (a leaf with no dependency
    edges is always eligible)."""
    merged_numbers = merged_phase_numbers(completion_log)

    deps_by_node: dict[str, list[str]] = defaultdict(list)
    for dependent, dependency in graph.edges:
        deps_by_node[dependent].append(dependency)

    eligible = []
    for phase in phase_files:
        if phase.number in merged_numbers:
            continue
        node = phase_file_stem(phase.number, phase.name)
        deps = deps_by_node.get(node, [])
        if all(phase_number_from_stem(dep) in merged_numbers for dep in deps):
            eligible.append(phase)
    return eligible


def run_parallel(
    config: SpecPrismFlowConfig,
    clone: Path,
    *,
    workers: int,
    dry_run: bool = False,
    strict: bool = False,
) -> list[PhaseRunResult]:
    """Drives `discover_phase_files(config.plan.phase_dir)` to completion against
    `clone` using a bounded `ThreadPoolExecutor`, submitting up to `workers` leaves
    at a time from whatever `eligible_leaves` reports, re-polling eligibility (from
    `clone`'s completion log, re-read fresh each time -- concurrent writers coordinate
    via `completion_log.append_and_commit`'s own retry loop, see toolchain.py) as
    each leaf completes. An escalated leaf's `OrchestrationError` is not re-raised:
    it, and every leaf that transitively depends on it, is excluded from all further
    submission, while unrelated branches keep running. The run ends once nothing is
    left running and no further leaf is eligible -- i.e. every leaf has either merged
    or is blocked (transitively) by an escalation."""
    graph = load_graph(config.plan.phase_dir / GRAPH_FILENAME)
    phase_files = [parse_phase_file(path) for path in discover_phase_files(config.plan.phase_dir)]
    completion_log_path = clone / COMPLETION_LOG_JSON_RELPATH

    results: list[PhaseRunResult] = []
    blocked_nodes: set[str] = set()
    running: dict[Future[PhaseRunResult], PhaseFile] = {}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while True:
            completion_log = load_all(completion_log_path)
            running_numbers = {phase.number for phase in running.values()}

            candidates = [
                phase
                for phase in eligible_leaves(graph, completion_log, phase_files)
                if phase.number not in running_numbers
                and phase_file_stem(phase.number, phase.name) not in blocked_nodes
            ]

            free_slots = workers - len(running)
            for phase in candidates[:free_slots]:
                future = executor.submit(phase_runner.run_phase, clone, config, phase, dry_run=dry_run, strict=strict)
                running[future] = phase

            if not running:
                break

            done, _ = wait(running.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                phase = running.pop(future)
                try:
                    result = future.result()
                except OrchestrationError as exc:
                    exc.with_context(phase_number=phase.number, phase_name=phase.name)
                    node = phase_file_stem(phase.number, phase.name)
                    dependents = reachable_from_all(graph.edges, graph.nodes, forward=False).get(node, set())
                    blocked_nodes |= {node, *dependents}
                else:
                    results.append(result)

    return results
