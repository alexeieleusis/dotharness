"""Shared readers for the spec-prism-flow plan: the TOML config, ``plan.*`` paths and the leaf list.

Used by both the orchestrator (leaf counting) and ``prbody`` (PR description) so the two cannot drift.
No imports from either of them.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

LEAF_GLOB = "*-leaf.md"


def read_spf_config(path: Path) -> dict[str, Any] | None:
    """Parse ``.spec-prism-flow.toml``; ``None`` when absent. Raises ``OSError``/``tomllib.TOMLDecodeError``."""
    if not path.is_file():
        return None
    return tomllib.loads(path.read_text(encoding="utf-8"))


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
