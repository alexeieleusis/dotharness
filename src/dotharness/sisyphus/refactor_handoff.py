"""Refactor handoff parsing for the scope check.

``edits_bullet_lines`` is the single scanner for the ``- Edits to existing files:`` bullet;
``parse_listed_edits`` derives the paths for the scope check from it.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence

_EDITS_BULLET = re.compile(r"^- Edits to existing files:[ \t]*(.*)$")
_EDIT_ITEM = re.compile(r"^\s*[-*]\s*(.*)$")


def edits_bullet_lines(text: str) -> list[str] | None:
    """Lines of the first edits bullet: the remainder of its line, then continuation lines.

    Continuation lines run up to the next top-level ``- `` bullet. ``None`` when the bullet is absent.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = _EDITS_BULLET.match(line)
        if match is None:
            continue
        body = [match.group(1)]
        for nxt in lines[i + 1 :]:
            if nxt.startswith("- "):
                break
            body.append(nxt)
        return body
    return None


def _edit_path(item: str) -> str:
    words = item.split(None, 1)
    if not words:
        return ""
    token = words[0].rstrip(":,;").strip("`")
    if token.lower().rstrip(".") == "none":
        return ""
    return posixpath.normpath(token)


def parse_listed_edits(text: str) -> set[str]:
    """Paths named under the first ``- Edits to existing files:`` bullet of a refactor handoff."""
    body = edits_bullet_lines(text)
    if body is None:
        return set()
    first = _EDIT_ITEM.match(body[0])
    candidates = [first.group(1) if first else body[0]]
    for line in body[1:]:
        item = _EDIT_ITEM.match(line)
        if item:
            candidates.append(item.group(1))
    paths: set[str] = set()
    for candidate in candidates:
        for piece in candidate.split("; "):
            path = _edit_path(piece)
            if path:
                paths.add(path)
    return paths


def scope_violations(
    name_status_output: str, files: Sequence[str], tests: Sequence[str], listed_edits: set[str]
) -> list[str]:
    """One ``"<status> <path>"`` string per non-``A`` entry outside files, tests and listed edits.

    ``name_status_output`` is ``git diff --name-status -z`` output (NUL-separated, paths unquoted).
    """
    allowed = set(files) | set(tests) | listed_edits
    violations: list[str] = []
    tokens = name_status_output.split("\0")
    i = 0
    while i + 1 < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        letter = status[0]
        n_paths = 2 if letter in ("R", "C") else 1
        paths = tokens[i + 1 : i + 1 + n_paths]
        i += 1 + n_paths
        if letter == "A":
            continue
        for path in paths:
            if path in allowed:
                continue
            entry = f"{letter} {path}"
            if entry not in violations:
                violations.append(entry)
    return violations
