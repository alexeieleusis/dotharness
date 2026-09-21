from __future__ import annotations

import re
from dataclasses import dataclass

from spec_prism_flow.chunk import Chunk, ChunkNode
from spec_prism_flow.errors import DecomposeError
from spec_prism_flow.graph import Graph, validate_graph
from spec_prism_flow.phase_file import PhaseFile, phase_file_stem

_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9-]+")

_PLACEHOLDER_NOTE = "(drafted by `plan draft-phases`)"
_DISJOINT_SCOPE_MARKER = "have no dependency path between them but both claim scope entry"


@dataclass(frozen=True)
class LeafVisit:
    number: int
    chunk: Chunk
    leaf_doc: str
    depends_on: str


@dataclass(frozen=True)
class LinearizationResult:
    leaves: list[LeafVisit]
    graph: Graph


def _slugify(name: str) -> str:
    slug = _SLUG_INVALID_CHARS.sub("-", name.strip().lower()).strip("-")
    return slug or "leaf"


def _dfs_collect_leaves(node: ChunkNode) -> list[ChunkNode]:
    if node.is_escalated:
        raise DecomposeError(  # noqa: TRY003
            f"Chunk {node.chunk.path!r} is still flagged for human review "
            f"({node.escalation_reason}); resolve it in the tree file before linearizing"
        )
    if node.is_leaf:
        return [node]

    assert node.children is not None  # noqa: S101 (ChunkNode's own invariant already guarantees this)
    leaves: list[ChunkNode] = []
    for child in node.children:
        leaves.extend(_dfs_collect_leaves(child))
    return leaves


def _mentions(doc: str, chunk: Chunk) -> bool:
    return (bool(chunk.path) and chunk.path in doc) or (bool(chunk.name) and chunk.name in doc)


def _shares_scope(a: LeafVisit, b: LeafVisit) -> bool:
    return not set(a.chunk.file_scope_estimate).isdisjoint(b.chunk.file_scope_estimate)


def _linked(a: LeafVisit, b: LeafVisit) -> bool:
    return _mentions(a.leaf_doc, b.chunk) or _mentions(b.leaf_doc, a.chunk) or _shares_scope(a, b)


def _derive_edges(leaves: list[LeafVisit], stems: list[str]) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    for i in range(len(leaves)):
        for j in range(i):
            if _linked(leaves[i], leaves[j]):
                edges.append((stems[i], stems[j]))
    return edges


def _synthetic_phase_file(leaf: LeafVisit) -> PhaseFile:
    return PhaseFile(
        number=leaf.number,
        name=_slugify(leaf.chunk.name),
        scope=list(leaf.chunk.file_scope_estimate),
        requirements=leaf.leaf_doc,
        acceptance_criteria=[_PLACEHOLDER_NOTE],
        manual_test_checklist=[_PLACEHOLDER_NOTE],
        depends_on=leaf.depends_on,
    )


def linearize(root: ChunkNode) -> LinearizationResult:
    leaf_nodes = _dfs_collect_leaves(root)
    if not leaf_nodes:
        raise DecomposeError("Decomposition tree has no leaves to linearize")  # noqa: TRY003

    leaves: list[LeafVisit] = []
    for idx, node in enumerate(leaf_nodes):
        number = idx + 1
        depends_on = "None (first phase)" if idx == 0 else f"Phase {number - 1} merged"
        assert node.leaf_doc is not None  # noqa: S101 (node.is_leaf already guarantees this)
        leaves.append(LeafVisit(number=number, chunk=node.chunk, leaf_doc=node.leaf_doc, depends_on=depends_on))

    stems = [phase_file_stem(leaf.number, _slugify(leaf.chunk.name)) for leaf in leaves]
    edges = _derive_edges(leaves, stems)
    derived_graph = Graph(nodes=stems, edges=edges)

    phase_files = [_synthetic_phase_file(leaf) for leaf in leaves]
    violations = validate_graph(derived_graph, phase_files)
    disjoint_scope_violations = [v for v in violations if _DISJOINT_SCOPE_MARKER in v]
    if disjoint_scope_violations:
        raise DecomposeError(
            "Disjoint-scope violation(s) between unlinked leaves:\n" + "\n".join(disjoint_scope_violations)
        )

    return LinearizationResult(leaves=leaves, graph=derived_graph)
