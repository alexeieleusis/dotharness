from __future__ import annotations

import re
from collections.abc import Iterable


def extract_list_items(lines: Iterable[str], pattern: re.Pattern[str]) -> list[str]:
    """Parse markdown list items matching `pattern`, joining indented continuation lines."""
    items: list[str] = []
    current: list[str] | None = None
    for line in lines:
        match = pattern.match(line)
        if match:
            if current is not None:
                items.append(" ".join(current))
            current = [match.group(1).strip()]
        elif current is not None and line.strip() and line[:1].isspace():
            current.append(line.strip())
        elif current is not None and not line.strip():
            items.append(" ".join(current))
            current = None
    if current is not None:
        items.append(" ".join(current))
    return items
