from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from spec_prism_flow import linearize, sizing
from spec_prism_flow.chunk import ChunkNode, load_tree
from spec_prism_flow.config import SpecPrismFlowConfig
from spec_prism_flow.decompose import TREE_FILENAME
from spec_prism_flow.errors import DecomposeError
from spec_prism_flow.phase_file import (
    PHASE_FILE_NAME_PATTERN,
    PhaseFile,
    extract_list_items,
    phase_file_name,
    render_phase_file,
)

_HEADING_PATTERN = re.compile(r"^#{1,6}\s")
_GOALS_HEADING_PATTERN = re.compile(r"^#{1,6}\s+.*\bgoals?\b", re.IGNORECASE)
_LIST_ITEM_PATTERN = re.compile(r"^-\s+(.*)$")
_GOAL_MARKER_PATTERN = re.compile(r"^\*{0,2}G\d+\.\*{0,2}\s*")

_NO_TESTABLE_REQUIREMENTS_NOTE = (
    "No structured, testable requirements could be derived from this leaf's mini-doc "
    "(no 'Goals' section and no 'G<N>.' markers found) — replace this placeholder with "
    "real criteria after a human review of the Requirements section above."
)


class DraftPhasesError(Exception):
    pass


@dataclass(frozen=True)
class DraftPhasesResult:
    written: list[Path]
    leaves_drafted: int
    internal_nodes_skipped: int
    outliers: list[str]


def _count_total_nodes(node: ChunkNode) -> int:
    if node.children is None:
        return 1
    return 1 + sum(_count_total_nodes(child) for child in node.children)


def _collect_section_body(lines: list[str], heading_index: int) -> list[str]:
    body = []
    for line in lines[heading_index + 1 :]:
        if _HEADING_PATTERN.match(line):
            break
        body.append(line)
    return body


def _derive_acceptance_criteria(leaf_doc: str) -> list[str]:
    """Heuristic: a mini-doc follows the same drafting template as the root requirements.md,

    so a 'Goals' heading's bullets (e.g. '- **G1.** ...') are its most reliable testable
    requirements (the template's own "Testable, not aspirational" property is written
    specifically against Goals items). If no such heading exists, fall back to scanning the
    whole doc for bare 'G<N>.'-marked bullets. If neither is found, emit a single flagged
    placeholder rather than an empty (and un-render-able) criteria list.
    """
    lines = leaf_doc.splitlines()
    goals_heading_index = next((i for i, line in enumerate(lines) if _GOALS_HEADING_PATTERN.match(line.strip())), None)
    if goals_heading_index is not None:
        items = extract_list_items(_collect_section_body(lines, goals_heading_index), _LIST_ITEM_PATTERN)
    else:
        items = [item for item in extract_list_items(lines, _LIST_ITEM_PATTERN) if _GOAL_MARKER_PATTERN.match(item)]
    criteria = [c for c in (_GOAL_MARKER_PATTERN.sub("", item).strip() for item in items) if c]
    return criteria or [_NO_TESTABLE_REQUIREMENTS_NOTE]


def _derive_manual_test_checklist(acceptance_criteria: list[str]) -> list[str]:
    if acceptance_criteria == [_NO_TESTABLE_REQUIREMENTS_NOTE]:
        return [_NO_TESTABLE_REQUIREMENTS_NOTE]
    return [f"Manually verify: {criterion}" for criterion in acceptance_criteria]


def _depends_on_text(number: int) -> str:
    return "None (first phase)." if number == 1 else f"Phase {number - 1} merged."


def run_draft_phases(cfg: SpecPrismFlowConfig) -> DraftPhasesResult:
    tree_path = cfg.plan.workspace_dir / TREE_FILENAME
    if not tree_path.exists():
        raise DraftPhasesError(  # noqa: TRY003
            f"Decomposition tree not found: {tree_path}; run 'plan decompose' first"
        )
    try:
        tree = load_tree(tree_path)
    except DecomposeError as e:
        raise DraftPhasesError(str(e)) from e

    try:
        result = linearize.linearize(tree)
    except DecomposeError as e:
        raise DraftPhasesError(  # noqa: TRY003
            f"{e}\nResolve any remaining 'escalation_reason' node directly in {tree_path} "
            "(the same manual-edit checkpoint 'plan decompose' pauses for) and re-run 'plan draft-phases'."
        ) from e

    internal_nodes_skipped = _count_total_nodes(tree) - len(result.leaves)

    cfg.plan.phase_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    outliers: list[str] = []
    # result.graph.nodes is built by linearize() from the same `leaves` list, in the same
    # order (one stem per leaf) — see linearize.py's `stems` computation. Reusing that stem
    # (rather than re-deriving a slug independently here) guarantees the phase file this
    # writes always matches the graph.json node `plan decompose` already wrote for it.
    for leaf, stem in zip(result.leaves, result.graph.nodes, strict=True):
        name_match = PHASE_FILE_NAME_PATTERN.match(stem + ".md")
        if not name_match:
            raise DraftPhasesError(  # noqa: TRY003
                f"Derived graph node {stem!r} does not match the '<NN>-<slug>-leaf' naming pattern"
            )
        name = name_match.group(2)

        acceptance_criteria = _derive_acceptance_criteria(leaf.leaf_doc)
        phase = PhaseFile(
            number=leaf.number,
            name=name,
            scope=list(leaf.chunk.file_scope_estimate),
            requirements=leaf.leaf_doc,
            acceptance_criteria=acceptance_criteria,
            manual_test_checklist=_derive_manual_test_checklist(acceptance_criteria),
            depends_on=_depends_on_text(leaf.number),
        )

        label = phase_file_name(phase.number, phase.name)
        file_scope_result = sizing.check_file_scope(phase.scope)
        if not file_scope_result.in_band:
            outliers.append(f"{label}: {file_scope_result.note}")
        word_count_result = sizing.check_word_count(phase.requirements)
        if not word_count_result.in_band:
            outliers.append(f"{label}: {word_count_result.note}")

        out_path = cfg.plan.phase_dir / label
        out_path.write_text(render_phase_file(phase))
        written.append(out_path)

    return DraftPhasesResult(
        written=written,
        leaves_drafted=len(result.leaves),
        internal_nodes_skipped=internal_nodes_skipped,
        outliers=outliers,
    )
