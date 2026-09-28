from __future__ import annotations

import json
from pathlib import Path

import click

from spec_prism_flow import generator, graph, handoff, linearize, sizing
from spec_prism_flow.chunk import Chunk, ChunkNode, load_tree, write_tree
from spec_prism_flow.config import SpecPrismFlowConfig
from spec_prism_flow.decompose_journal import JOURNAL_FILENAME, DecomposeJournal
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


def _sizing_over_ceiling(chunk: Chunk, doc: str) -> tuple[bool, str]:
    file_result = sizing.check_file_scope(chunk.file_scope_estimate)
    word_result = sizing.check_word_count(doc)
    notes = [n for n in (file_result.note, word_result.note) if n]
    # Only oversize forces a split. Undersize is advisory: a small chunk the generator judged a single
    # feature (including the root, whose file scope is unknown) is a valid early stop.
    return (file_result.over_ceiling or word_result.over_ceiling), "; ".join(notes)


def _escalate(log_path: Path, chunk: Chunk, log_reason: str, escalation_reason: str) -> ChunkNode:
    _append_log(log_path, chunk.path, "escalate", log_reason)
    return ChunkNode(chunk=chunk, escalation_reason=escalation_reason)


def _generate(
    chunk: Chunk, cfg: SpecPrismFlowConfig, journal: DecomposeJournal | None, *, forced_split: bool = False
) -> generator.GeneratorResult:
    """Replays a journaled result when one exists for this chunk; otherwise runs the generator and records it."""
    if journal is not None:
        replayed = journal.lookup(chunk, forced_split=forced_split)
        if replayed is not None:
            return replayed
    result = generator.run_generator(chunk, cfg, forced_split=forced_split)
    if journal is not None:
        journal.record(chunk, forced_split=forced_split, result=result)
    return result


def resolve_chunk(
    chunk: Chunk,
    cfg: SpecPrismFlowConfig,
    depth_cap: int,
    log_path: Path,
    journal: DecomposeJournal | None = None,
) -> ChunkNode:
    result = _generate(chunk, cfg, journal)

    if isinstance(result, generator.Leaf):
        force_split, reason = _sizing_over_ceiling(chunk, result.doc)
        if not force_split:
            return ChunkNode(chunk=chunk, leaf_doc=result.doc)

        if chunk.depth >= depth_cap:
            return _escalate(
                log_path,
                chunk,
                f"depth cap reached; skipping forced-split retry ({reason})",
                f"Depth cap ({depth_cap}) reached; skipping forced-split retry ({reason})",
            )

        _append_log(log_path, chunk.path, "retry", f"forced-split retry after natural Leaf verdict: {reason}")
        retry_result = _generate(chunk, cfg, journal, forced_split=True)

        if isinstance(retry_result, generator.Leaf):
            return _escalate(
                log_path,
                chunk,
                f"forced-split retry still returned Leaf ({reason})",
                f"Trivial-breakdown deadlock: forced-split retry still returned Leaf ({reason})",
            )

        children = [resolve_chunk(child, cfg, depth_cap, log_path, journal) for child in retry_result.children]
        return ChunkNode(chunk=chunk, children=children)

    if chunk.depth >= depth_cap:
        return _escalate(
            log_path, chunk, "depth cap reached on Split verdict", f"Depth cap ({depth_cap}) reached; Split discarded"
        )

    children = [resolve_chunk(child, cfg, depth_cap, log_path, journal) for child in result.children]
    return ChunkNode(chunk=chunk, children=children)


def _pause_for_tree_review(tree_path: Path) -> None:
    click.echo(f"Decomposition tree written to: {tree_path}")
    click.echo(
        "Review it now: reorder children, force a merge/split, or resolve any 'escalation_reason' "
        "entries by editing the file directly."
    )
    handoff.wait_for_confirmation("Tree approved and ready to continue?")


def run_decompose(
    cfg: SpecPrismFlowConfig, *, depth_cap: int = DEFAULT_DEPTH_CAP, resume: bool = False
) -> tuple[Path, Path]:
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
    tree_path = cfg.plan.workspace_dir / TREE_FILENAME
    journal = DecomposeJournal(cfg.plan.workspace_dir / JOURNAL_FILENAME)
    if not resume:
        journal.discard()

    if resume and journal.tree_matches(root_chunk, depth_cap) and tree_path.exists():
        # The tree is already on disk, possibly hand-edited during the review pause; rebuilding
        # it from the journal would overwrite those edits.
        click.echo(f"Resuming at tree review; keeping existing {tree_path}")
    else:
        if resume:
            click.echo(f"Resuming: replaying {journal.result_count} recorded generator result(s)")
            # Replay re-derives every retry/escalation, so drop the entries the interrupted run appended.
            log_path.unlink(missing_ok=True)
        tree = resolve_chunk(root_chunk, cfg, depth_cap, log_path, journal)
        write_tree(tree, tree_path)
        journal.mark_tree_written(root_chunk, depth_cap)

    _pause_for_tree_review(tree_path)

    approved_tree = load_tree(tree_path)
    result = linearize.linearize(approved_tree)

    cfg.plan.phase_dir.mkdir(parents=True, exist_ok=True)
    graph_path = cfg.plan.phase_dir / GRAPH_FILENAME
    graph.write_graph(result.graph, graph_path)

    journal.discard()
    return tree_path, graph_path
