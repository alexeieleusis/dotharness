"""Resolution of packaged knowledge files (prompts and the lifecycle cheat sheet).

Files resolve through :func:`dotharness.paths.default_knowledge_dir` (``~/.harness/knowledge`` when it exists,
else the knowledge shipped in the package). It takes no parameters; tests monkeypatch ``HOME``. A file missing from a
user knowledge dir falls back to the packaged copy. No candidate existing raises :class:`PathResolutionError` naming
every path tried; a nonexistent path is never returned.
"""

# ruff: noqa: TRY003
from __future__ import annotations

from importlib import resources
from pathlib import Path

from dotharness.paths import default_knowledge_dir


class PathResolutionError(Exception):
    """Raised when no candidate location exists."""


def knowledge_file(name: str) -> Path:
    """Resolve a named knowledge file, e.g. ``sisyphus/intake-prompt.md``.

    A leading ``knowledge/`` is accepted. The file is never modified. Absolute names and ``..`` segments are rejected.
    """
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise PathResolutionError(f"invalid knowledge file name: {name!r}")
    if rel.parts[0] == "knowledge" and len(rel.parts) > 1:
        rel = Path(*rel.parts[1:])
    tried = [default_knowledge_dir() / rel]
    packaged = Path(str(resources.files("dotharness") / "knowledge")) / rel
    if packaged not in tried:
        tried.append(packaged)
    for candidate in tried:
        if candidate.exists():
            return candidate.resolve()
    raise PathResolutionError("no existing location found; tried: " + ", ".join(str(p) for p in tried))
