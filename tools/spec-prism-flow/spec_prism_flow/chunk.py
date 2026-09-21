from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from spec_prism_flow.errors import ChunkError, DecomposeError


@dataclass(frozen=True)
class Chunk:
    path: str
    name: str
    file_scope_estimate: list[str]
    requirements_slice: str
    depth: int


@dataclass(frozen=True)
class ChunkNode:
    chunk: Chunk
    leaf_doc: str | None = None
    children: list[ChunkNode] | None = None
    escalation_reason: str | None = None

    def __post_init__(self) -> None:
        set_count = sum(x is not None for x in (self.leaf_doc, self.children, self.escalation_reason))
        if set_count != 1:
            raise ChunkError(  # noqa: TRY003
                f"ChunkNode for {self.chunk.path!r} must set exactly one of leaf_doc, children, "
                f"or escalation_reason, got {set_count}"
            )
        if self.children is not None and not self.children:
            raise ChunkError(f"ChunkNode for {self.chunk.path!r} has an empty children list")  # noqa: TRY003

    @property
    def is_leaf(self) -> bool:
        return self.leaf_doc is not None

    @property
    def is_escalated(self) -> bool:
        return self.escalation_reason is not None


def _chunk_to_dict(chunk: Chunk) -> dict:
    return {
        "path": chunk.path,
        "name": chunk.name,
        "file_scope_estimate": list(chunk.file_scope_estimate),
        "requirements_slice": chunk.requirements_slice,
        "depth": chunk.depth,
    }


_CHUNK_REQUIRED_FIELDS = ("path", "name", "file_scope_estimate", "requirements_slice", "depth")


def _chunk_from_dict(data: dict) -> Chunk:
    missing = [field for field in _CHUNK_REQUIRED_FIELDS if field not in data]
    if missing:
        raise ChunkError(  # noqa: TRY003
            f"Chunk {data.get('path')!r} is missing required field(s): {', '.join(missing)}"
        )
    file_scope_estimate = data["file_scope_estimate"]
    if not isinstance(file_scope_estimate, list):
        raise ChunkError(  # noqa: TRY003
            f"Chunk {data.get('path')!r} has file_scope_estimate of type "
            f"{type(file_scope_estimate).__name__}, expected a list"
        )
    return Chunk(
        path=data["path"],
        name=data["name"],
        file_scope_estimate=list(file_scope_estimate),
        requirements_slice=data["requirements_slice"],
        depth=data["depth"],
    )


def node_to_dict(node: ChunkNode) -> dict:
    data: dict = {"chunk": _chunk_to_dict(node.chunk)}
    if node.leaf_doc is not None:
        data["leaf_doc"] = node.leaf_doc
    elif node.children is not None:
        data["children"] = [node_to_dict(child) for child in node.children]
    else:
        data["escalation_reason"] = node.escalation_reason
    return data


def node_from_dict(data: dict) -> ChunkNode:
    if "chunk" not in data:
        raise ChunkError("Tree node is missing required field: chunk")  # noqa: TRY003
    chunk = _chunk_from_dict(data["chunk"])
    variant_keys = [key for key in ("leaf_doc", "children", "escalation_reason") if key in data]
    if len(variant_keys) != 1:
        raise ChunkError(  # noqa: TRY003
            f"Tree node for chunk {chunk.path!r} must have exactly one of leaf_doc/children/"
            f"escalation_reason, got {variant_keys}"
        )
    if variant_keys[0] == "leaf_doc":
        return ChunkNode(chunk=chunk, leaf_doc=data["leaf_doc"])
    if variant_keys[0] == "children":
        return ChunkNode(chunk=chunk, children=[node_from_dict(c) for c in data["children"]])
    return ChunkNode(chunk=chunk, escalation_reason=data["escalation_reason"])


def write_tree(node: ChunkNode, path: Path) -> None:
    path.write_text(json.dumps(node_to_dict(node), indent=2) + "\n")


def load_tree(path: Path) -> ChunkNode:
    if not path.exists():
        raise DecomposeError(f"Tree file not found: {path}")  # noqa: TRY003
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise DecomposeError(f"Tree file is not valid JSON: {path}") from e  # noqa: TRY003
    return node_from_dict(data)
