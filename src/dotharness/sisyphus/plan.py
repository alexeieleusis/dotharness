"""Shared readers for the spec-prism-flow plan: the TOML config, ``plan.*`` paths and the leaf list.

Used by both the orchestrator (leaf counting) and ``prbody`` (PR description) so the two cannot drift.
No imports from either of them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotharness.fileio import load_toml

LEAF_GLOB = "*-leaf.md"


def read_spf_config(path: Path) -> dict[str, Any] | None:
    """Parse ``.spec-prism-flow.toml``; ``None`` when absent. Raises ``OSError``/``TOMLDecodeError``."""
    if not path.is_file():
        return None
    return load_toml(path)


def plan_path(data: Mapping[str, Any] | None, repo: Path, key: str) -> Path | None:
    """``plan.<key>`` from parsed config as a path (relative values resolve against ``repo``), or ``None``."""
    plan = data.get("plan") if data else None
    raw = plan.get(key) if isinstance(plan, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else repo / path


def leaf_stems(phase_dir: Path) -> list[str]:
    """Leaf stems from ``<phase_dir>/graph.json`` ``nodes``, else the sorted ``*-leaf.md`` stems (maybe empty)."""
    graph = phase_dir / "graph.json"
    try:
        nodes = json.loads(graph.read_text(encoding="utf-8")).get("nodes")
        if isinstance(nodes, list) and nodes and all(isinstance(n, str) and n for n in nodes):
            return list(nodes)
    except (OSError, ValueError, AttributeError):
        pass
    return sorted(p.stem for p in phase_dir.glob(LEAF_GLOB))


_SCOPE_HEADER = "## Scope"
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


@dataclass(frozen=True)
class ScopeRead:
    """Scope entries of the leaves in order, plus lines of a Scope section that are not ``- `` bullets (not guessed at)."""

    entries: tuple[str, ...]
    skipped: tuple[str, ...]


def _outside_fence(lines: list[str]) -> list[bool]:
    """Per line: True when outside fenced code (fence lines count as inside; same rule as the phase-file parser)."""
    flags: list[bool] = []
    open_marker: str | None = None
    for line in lines:
        match = _FENCE.match(line)
        if open_marker is None:
            flags.append(not match)
            if match:
                open_marker = match.group(1)
        else:
            flags.append(False)
            if (
                match
                and match.group(1)[0] == open_marker[0]
                and len(match.group(1)) >= len(open_marker)
                and not match.group(2).strip()
            ):
                open_marker = None
    return flags


def scope_section(text: str) -> list[str]:
    """Non-blank stripped lines of the ``## Scope`` body (up to the next ``## `` header), fences respected."""
    lines = text.splitlines()
    outside = _outside_fence(lines)
    body: list[str] = []
    inside = False
    for line, out in zip(lines, outside, strict=True):
        if out and line.startswith("## "):
            if inside:
                break
            inside = line.strip() == _SCOPE_HEADER
            continue
        if inside and line.strip():
            body.append(line.strip())
    return body


def read_scope(phase_dir: Path) -> ScopeRead:
    """Scope entries of every leaf in ``leaf_stems(phase_dir)``, in leaf order (duplicates kept).

    A bullet is ``- `` plus text, the item being ``line[2:].strip()`` as in the phase-file parser. Other lines are
    returned in ``skipped``. A leaf that is unreadable or has no Scope section contributes nothing.
    """
    entries: list[str] = []
    skipped: list[str] = []
    for stem in leaf_stems(phase_dir):
        try:
            text = (phase_dir / f"{stem}.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in scope_section(text):
            item = line[2:].strip() if line.startswith("- ") else ""
            if item:
                entries.append(item)
            else:
                skipped.append(line)
    return ScopeRead(tuple(entries), tuple(skipped))
