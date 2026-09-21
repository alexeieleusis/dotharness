from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from spec_prism_flow.phase_file import PhaseFile, phase_file_stem

_UNVISITED, _IN_PROGRESS, _DONE = 0, 1, 2


class GraphError(ValueError):
    pass


@dataclass(frozen=True)
class Graph:
    nodes: list[str]
    edges: list[tuple[str, str]]


def load_graph(path: Path) -> Graph:
    data = json.loads(path.read_text())

    nodes = data["nodes"]
    if not isinstance(nodes, list) or not all(isinstance(n, str) for n in nodes):
        raise GraphError("graph 'nodes' must be a list of strings")  # noqa: TRY003

    edges = data["edges"]
    if not isinstance(edges, list) or not all(
        isinstance(e, list) and len(e) == 2 and all(isinstance(n, str) for n in e) for e in edges
    ):
        raise GraphError("graph 'edges' must be a list of [dependent, dependency] string pairs")  # noqa: TRY003

    return Graph(
        nodes=nodes,
        edges=[(dependent, dependency) for dependent, dependency in edges],
    )


def write_graph(graph: Graph, path: Path) -> None:
    data = {"nodes": graph.nodes, "edges": [[dependent, dependency] for dependent, dependency in graph.edges]}
    path.write_text(json.dumps(data, indent=2) + "\n")


def _stem(phase_file: PhaseFile) -> str:
    return phase_file_stem(phase_file.number, phase_file.name)


def _reachable_from_all(edges: list[tuple[str, str]], nodes: list[str], *, forward: bool) -> dict[str, set[str]]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for dependent, dependency in edges:
        if forward:
            adjacency[dependent].append(dependency)
        else:
            adjacency[dependency].append(dependent)

    reachable: dict[str, set[str]] = {}
    for start in nodes:
        seen: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            for neighbor in adjacency.get(node, []):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        reachable[start] = seen
    return reachable


def _find_cycle(edges: list[tuple[str, str]], nodes: list[str]) -> list[str] | None:
    adjacency: dict[str, list[str]] = defaultdict(list)
    for dependent, dependency in edges:
        adjacency[dependent].append(dependency)

    state: dict[str, int] = dict.fromkeys(nodes, _UNVISITED)
    for start in nodes:
        if state.get(start, _UNVISITED) != _UNVISITED:
            continue
        cycle = _walk_for_cycle(start, adjacency, state)
        if cycle is not None:
            return cycle
    return None


def _walk_for_cycle(start: str, adjacency: dict[str, list[str]], state: dict[str, int]) -> list[str] | None:
    path: list[str] = [start]
    frontiers = [iter(adjacency.get(start, []))]
    state[start] = _IN_PROGRESS

    while path:
        neighbor = next(frontiers[-1], None)
        if neighbor is None:
            state[path.pop()] = _DONE
            frontiers.pop()
            continue
        neighbor_state = state.get(neighbor, _UNVISITED)
        if neighbor_state == _IN_PROGRESS:
            cycle_start = path.index(neighbor)
            return [*path[cycle_start:], neighbor]
        if neighbor_state == _UNVISITED:
            state[neighbor] = _IN_PROGRESS
            path.append(neighbor)
            frontiers.append(iter(adjacency.get(neighbor, [])))
    return None


def _check_orphans(graph: Graph, phase_files: list[PhaseFile]) -> list[str]:
    stems_from_files = {_stem(pf) for pf in phase_files}
    node_set = set(graph.nodes)

    violations = [
        f"Phase file '{stem}' has no corresponding graph node" for stem in sorted(stems_from_files - node_set)
    ]
    violations += [
        f"Graph node '{node}' has no corresponding phase file" for node in sorted(node_set - stems_from_files)
    ]
    return violations


def check_acyclic(graph: Graph) -> list[str]:
    cycle = _find_cycle(graph.edges, graph.nodes)
    if cycle is None:
        return []
    return [f"Graph contains a cycle: {' -> '.join(cycle)}"]


def check_disjoint_scope(graph: Graph, phase_files: list[PhaseFile]) -> list[str]:
    node_set = set(graph.nodes)
    scope_by_stem = {_stem(pf): set(pf.scope) for pf in phase_files}
    stems = sorted(stem for stem in scope_by_stem if stem in node_set)

    forward_reachable = _reachable_from_all(graph.edges, graph.nodes, forward=True)
    backward_reachable = _reachable_from_all(graph.edges, graph.nodes, forward=False)

    violations = []
    for i, stem_a in enumerate(stems):
        for stem_b in stems[i + 1 :]:
            if stem_b in forward_reachable.get(stem_a, set()) or stem_b in backward_reachable.get(stem_a, set()):
                continue
            for entry in sorted(scope_by_stem[stem_a] & scope_by_stem[stem_b]):
                violations.append(
                    f"Leaves '{stem_a}' and '{stem_b}' have no dependency path between them "
                    f"but both claim scope entry '{entry}'"
                )
    return violations


def _check_linear_chain_coverage(graph: Graph, phase_files: list[PhaseFile]) -> list[str]:
    phase_by_number = {pf.number: pf for pf in phase_files}
    edge_set = set(graph.edges)

    violations = []
    for pf in phase_files:
        if pf.number <= 1:
            continue
        referenced_numbers = set(pf.depends_on_phase_numbers)
        predecessor_number = pf.number - 1
        if predecessor_number not in referenced_numbers:
            continue
        predecessor = phase_by_number.get(predecessor_number)
        if predecessor is None:
            continue
        edge = (_stem(pf), _stem(predecessor))
        if edge not in edge_set:
            violations.append(
                f"Phase {pf.number} names phase {predecessor_number} in its 'Depends on' text, "
                f"but graph is missing edge {edge!r}"
            )
    return violations


def validate_graph(graph: Graph, phase_files: list[PhaseFile]) -> list[str]:
    return [
        *_check_orphans(graph, phase_files),
        *check_acyclic(graph),
        *check_disjoint_scope(graph, phase_files),
        *_check_linear_chain_coverage(graph, phase_files),
    ]
