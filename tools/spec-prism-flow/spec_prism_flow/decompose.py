from __future__ import annotations

import json
from pathlib import Path

import click

from spec_prism_flow import generator, graph, handoff, linearize, sizing
from spec_prism_flow.chunk import Chunk, ChunkNode, load_tree, write_tree
from spec_prism_flow.config import SpecPrismFlowConfig
from spec_prism_flow.errors import DecomposeError
from spec_prism_flow.requirements_stage import REQUIREMENTS_FILENAME
from spec_prism_flow.workspace import ManifestError, load_manifest

DEFAULT_DEPTH_CAP = 4

TREE_FILENAME = "decompose_tree.json"
DECOMPOSE_LOG_FILENAME = "decompose_log.jsonl"
GRAPH_FILENAME = "graph.json"

_ROOT_CHUNK_PATH = "A"
_ROOT_CHUNK_NAME = "root"


def _append_log(log_path: Path, chunk_path: str, event: str, reason: str) -> None:
    entry = {"chunk_path": chunk_path, "event": event, "reason": reason}
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _sizing_out_of_band(chunk: Chunk, doc: str) -> tuple[bool, str]:
    file_result = sizing.check_file_scope(chunk.file_scope_estimate)
    word_result = sizing.check_word_count(doc)
    notes = [n for n in (file_result.note, word_result.note) if n]
    return (not file_result.in_band or not word_result.in_band), "; ".join(notes)


def _escalate(log_path: Path, chunk: Chunk, log_reason: str, escalation_reason: str) -> ChunkNode:
    _append_log(log_path, chunk.path, "escalate", log_reason)
    return ChunkNode(chunk=chunk, escalation_reason=escalation_reason)


def resolve_chunk(chunk: Chunk, cfg: SpecPrismFlowConfig, depth_cap: int, log_path: Path) -> ChunkNode:
    result = generator.run_generator(chunk, cfg)

    if isinstance(result, generator.Leaf):
        out_of_band, reason = _sizing_out_of_band(chunk, result.doc)
        if not out_of_band:
            return ChunkNode(chunk=chunk, leaf_doc=result.doc)

        _append_log(log_path, chunk.path, "retry", f"forced-split retry after natural Leaf verdict: {reason}")
        retry_result = generator.run_generator(chunk, cfg, forced_split=True)

        if isinstance(retry_result, generator.Leaf):
            return _escalate(
                log_path,
                chunk,
                f"forced-split retry still returned Leaf ({reason})",
                f"Trivial-breakdown deadlock: forced-split retry still returned Leaf ({reason})",
            )

        if chunk.depth >= depth_cap:
            return _escalate(
                log_path,
                chunk,
                "forced-split retry returned Split but depth cap reached",
                f"Depth cap ({depth_cap}) reached after forced-split retry; Split discarded",
            )

        children = [resolve_chunk(child, cfg, depth_cap, log_path) for child in retry_result.children]
        return ChunkNode(chunk=chunk, children=children)

    if chunk.depth >= depth_cap:
        return _escalate(
            log_path, chunk, "depth cap reached on Split verdict", f"Depth cap ({depth_cap}) reached; Split discarded"
        )

    children = [resolve_chunk(child, cfg, depth_cap, log_path) for child in result.children]
    return ChunkNode(chunk=chunk, children=children)


def _pause_for_tree_review(tree_path: Path) -> None:
    click.echo(f"Decomposition tree written to: {tree_path}")
    click.echo(
        "Review it now: reorder children, force a merge/split, or resolve any 'escalation_reason' "
        "entries by editing the file directly."
    )
    handoff.wait_for_confirmation("Tree approved and ready to continue?")


def run_decompose(cfg: SpecPrismFlowConfig, *, depth_cap: int = DEFAULT_DEPTH_CAP) -> tuple[Path, Path]:
    try:
        load_manifest(cfg.plan.workspace_dir)
    except ManifestError as e:
        raise DecomposeError(str(e)) from e

    requirements_path = cfg.plan.workspace_dir / REQUIREMENTS_FILENAME
    if not requirements_path.exists():
        raise DecomposeError(  # noqa: TRY003
            f"Requirements doc not found: {requirements_path}; run 'plan draft-requirements' first"
        )
    requirements_text = requirements_path.read_text()

    root_chunk = Chunk(
        path=_ROOT_CHUNK_PATH,
        name=_ROOT_CHUNK_NAME,
        file_scope_estimate=[],
        requirements_slice=requirements_text,
        depth=0,
    )

    log_path = cfg.plan.workspace_dir / DECOMPOSE_LOG_FILENAME
    tree = resolve_chunk(root_chunk, cfg, depth_cap, log_path)

    tree_path = cfg.plan.workspace_dir / TREE_FILENAME
    write_tree(tree, tree_path)

    _pause_for_tree_review(tree_path)

    approved_tree = load_tree(tree_path)
    result = linearize.linearize(approved_tree)

    cfg.plan.phase_dir.mkdir(parents=True, exist_ok=True)
    graph_path = cfg.plan.phase_dir / GRAPH_FILENAME
    graph.write_graph(result.graph, graph_path)

    return tree_path, graph_path
