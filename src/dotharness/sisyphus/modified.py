"""Shared definition of the ``modified`` items in ``refactor-input.json``: ``[{"path": str, "ratio": float}]``."""

from __future__ import annotations

import math


def parse_modified(value: object) -> list[tuple[str, float]]:
    """Well-formed ``(path, ratio)`` pairs of a ``modified`` value; ``[]`` if missing or malformed.

    An item is well-formed when ``path`` is a single-line string and ``ratio`` is a finite,
    non-bool number in ``[0, 1]``.
    """
    if not isinstance(value, list):
        return []
    pairs: list[tuple[str, float]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        path, ratio = item.get("path"), item.get("ratio")
        if not isinstance(path, str) or "\n" in path or "\r" in path:
            continue
        if isinstance(ratio, bool) or not isinstance(ratio, int | float):
            continue
        if math.isfinite(ratio) and 0 <= ratio <= 1:
            pairs.append((path, float(ratio)))
    return pairs


def format_percent(ratio: float) -> str:
    """``NN%`` for a ratio in ``[0, 1]``."""
    return f"{round(ratio * 100)}%"
