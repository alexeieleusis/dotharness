from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from spec_prism_flow import handoff
from spec_prism_flow.chunk import Chunk
from spec_prism_flow.config import SpecPrismFlowConfig
from spec_prism_flow.errors import DecomposeError
from spec_prism_flow.requirements_stage import TEMPLATE_FILENAME

_KNOWLEDGE_SUBDIR = "spec-prism-flow"
_LEAF_MARKER = "LEAF"
_SPLIT_MARKER = "SPLIT"
_MIN_SPLIT_CHILDREN = 2
_MAX_SPLIT_CHILDREN = 4
_SPLIT_CHILD_KEYS = ("name", "file_scope_estimate", "requirements_slice")


@dataclass(frozen=True)
class Leaf:
    doc: str


@dataclass(frozen=True)
class Split:
    children: list[Chunk]


GeneratorResult = Leaf | Split


def build_generator_prompt(chunk: Chunk, template_text: str, *, forced_split: bool = False) -> str:
    lines = [
        template_text,
        "\n\n## Chunk under decomposition\n\n",
        f"- Path: `{chunk.path}`\n",
        f"- Name: {chunk.name}\n",
        f"- Depth: {chunk.depth}\n",
        f"- Rough file-scope estimate so far: {chunk.file_scope_estimate}\n",
        "\n### Rough brief (this chunk's own requirements slice)\n\n",
        "This is a self-contained sub-scope of the parent requirements document — deliberately not "
        "padded with sibling or parent context. Run this prompt's full elicitation process against "
        "it exactly as you would against a whole brief.\n\n",
        chunk.requirements_slice,
        "\n\n## Required output format\n\n",
        "Write your output file starting with exactly one marker line, either `LEAF` or `SPLIT`, "
        "with nothing else on that line:\n\n",
        "- `LEAF` — this chunk's Feature/component breakdown is trivial (one feature, nothing left "
        "to split) and it fits the sizing bands. Follow the marker line with the full requirements "
        "mini-doc this elicitation process produced for the chunk.\n",
        "- `SPLIT` — the breakdown is non-trivial. Follow the marker line with a JSON array of "
        f"{_MIN_SPLIT_CHILDREN}-{_MAX_SPLIT_CHILDREN} objects and nothing else, each with keys "
        '`"name"` (string), `"file_scope_estimate"` (list of file path strings), and '
        '`"requirements_slice"` (this child\'s own self-contained sub-scope text, quoted from the '
        "chunk's requirements slice above).\n",
        "\nAny output that does not open with one of these two exact marker lines will be rejected as malformed.\n",
    ]
    if forced_split:
        lines.append(
            "\n**This chunk was already judged trivial (`LEAF`) once, but the result failed the "
            "sizing bands. A split is required regardless of your own trivial-breakdown judgment "
            "this time — return `SPLIT` with genuine sub-chunks, not another `LEAF` verdict.**\n"
        )
    return "".join(lines)


def _parse_split_children(rest: str, chunk: Chunk) -> list[Chunk]:
    try:
        raw_children = json.loads(rest)
    except json.JSONDecodeError as e:
        raise DecomposeError(  # noqa: TRY003
            f"Generator SPLIT output for chunk {chunk.path!r} is not valid JSON: {e}"
        ) from e

    if not isinstance(raw_children, list) or not (_MIN_SPLIT_CHILDREN <= len(raw_children) <= _MAX_SPLIT_CHILDREN):
        raise DecomposeError(  # noqa: TRY003
            f"Generator SPLIT output for chunk {chunk.path!r} must be a JSON array of "
            f"{_MIN_SPLIT_CHILDREN}-{_MAX_SPLIT_CHILDREN} child objects, got: {raw_children!r}"
        )

    children = []
    for i, raw in enumerate(raw_children, start=1):
        if not isinstance(raw, dict) or not all(k in raw for k in _SPLIT_CHILD_KEYS):
            raise DecomposeError(  # noqa: TRY003
                f"Generator SPLIT child #{i} for chunk {chunk.path!r} missing required keys "
                f"{_SPLIT_CHILD_KEYS}: {raw!r}"
            )
        if (
            not isinstance(raw["name"], str)
            or not isinstance(raw["file_scope_estimate"], list)
            or not all(isinstance(p, str) for p in raw["file_scope_estimate"])
            or not isinstance(raw["requirements_slice"], str)
        ):
            raise DecomposeError(  # noqa: TRY003
                f"Generator SPLIT child #{i} for chunk {chunk.path!r} has the wrong types for "
                f"{_SPLIT_CHILD_KEYS}: {raw!r}"
            )
        children.append(
            Chunk(
                path=f"{chunk.path}-{i}",
                name=raw["name"],
                file_scope_estimate=list(raw["file_scope_estimate"]),
                requirements_slice=raw["requirements_slice"],
                depth=chunk.depth + 1,
            )
        )
    return children


def _parse_generator_output(output_text: str, chunk: Chunk) -> GeneratorResult:
    lines = output_text.splitlines()
    if not lines:
        raise DecomposeError(f"Generator output for chunk {chunk.path!r} is empty")  # noqa: TRY003

    marker = lines[0].strip()
    rest = "\n".join(lines[1:]).lstrip("\n")

    if marker == _LEAF_MARKER:
        if not rest.strip():
            raise DecomposeError(  # noqa: TRY003
                f"Generator LEAF output for chunk {chunk.path!r} has an empty requirements doc body"
            )
        return Leaf(doc=rest)
    if marker == _SPLIT_MARKER:
        return Split(children=_parse_split_children(rest, chunk))
    raise DecomposeError(  # noqa: TRY003
        f"Generator output for chunk {chunk.path!r} must open with a 'LEAF' or 'SPLIT' marker line, got {marker!r}"
    )


@lru_cache(maxsize=1)
def _read_template(template_path: Path) -> str:
    if not template_path.exists():
        raise DecomposeError(f"Requirements drafting template not found: {template_path}")  # noqa: TRY003
    return template_path.read_text()


def run_generator(chunk: Chunk, cfg: SpecPrismFlowConfig, *, forced_split: bool = False) -> GeneratorResult:
    template_path = cfg.harness.knowledge_dir / _KNOWLEDGE_SUBDIR / TEMPLATE_FILENAME
    template_text = _read_template(template_path)

    prompt_text = build_generator_prompt(chunk, template_text, forced_split=forced_split)

    stage_name = f"decompose_{chunk.path}_forced" if forced_split else f"decompose_{chunk.path}"
    try:
        output_text = handoff.run_handoff(prompt_text, cfg.plan.workspace_dir, stage_name=stage_name)
    except handoff.HandoffError as e:
        raise DecomposeError(str(e)) from e

    return _parse_generator_output(output_text, chunk)
