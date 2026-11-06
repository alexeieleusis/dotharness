"""Deterministic resolution of spec-prism-flow tool and knowledge locations.

The installed location under the home directory wins; the dev checkout (found
by walking up from this file) is the fallback. Home and checkout root are
injectable. No candidate existing raises :class:`PathResolutionError` naming
every path tried; a nonexistent path is never returned.
"""

# ruff: noqa: TRY003
from __future__ import annotations

from pathlib import Path

INSTALLED_DIRNAME = ".harness"
TOOL_NAME = "spec-prism-flow"


class PathResolutionError(Exception):
    """Raised when no candidate location exists."""


def find_checkout_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default: this file) to the dotharness checkout root.

    The root is the first ancestor containing both ``knowledge`` and ``tools`` directories.
    """
    here = (start if start is not None else Path(__file__)).resolve()
    for parent in (here, *here.parents):
        if (parent / "knowledge").is_dir() and (parent / "tools").is_dir():
            return parent
    return None


def _resolve(
    installed: Path,
    dev_rel: tuple[str, ...],
    checkout_root: Path | None,
) -> Path:
    tried: list[Path] = [installed]
    if installed.exists():
        return installed.resolve()
    root = checkout_root if checkout_root is not None else find_checkout_root()
    if root is not None:
        dev = Path(root).joinpath(*dev_rel)
        tried.append(dev)
        if dev.exists():
            return dev.resolve()
    raise PathResolutionError("no existing location found; tried: " + ", ".join(str(p) for p in tried))


def _home(home: Path | None) -> Path:
    return Path(home) if home is not None else Path.home()


def tool_dir(home: Path | None = None, checkout_root: Path | None = None) -> Path:
    """Absolute spec-prism-flow tool directory (``~/.harness/tools/spec-prism-flow``)."""
    return _resolve(
        _home(home) / INSTALLED_DIRNAME / "tools" / TOOL_NAME,
        ("tools", TOOL_NAME),
        checkout_root,
    )


def knowledge_dir(home: Path | None = None, checkout_root: Path | None = None) -> Path:
    """Absolute knowledge directory (``~/.harness/knowledge``)."""
    return _resolve(_home(home) / INSTALLED_DIRNAME / "knowledge", ("knowledge",), checkout_root)


def knowledge_file(name: str, home: Path | None = None, checkout_root: Path | None = None) -> Path:
    """Resolve a named knowledge file, e.g. ``sisyphus/intake-prompt.md``.

    A leading ``knowledge/`` is accepted. The installed knowledge dir is tried first, then the
    checkout. The file is never modified. Absolute names and ``..`` segments are rejected.
    """
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise PathResolutionError(f"invalid knowledge file name: {name!r}")
    if rel.parts[0] == "knowledge":
        rel = Path(*rel.parts[1:]) if len(rel.parts) > 1 else rel
    return _resolve(
        _home(home) / INSTALLED_DIRNAME / "knowledge" / rel,
        ("knowledge", *rel.parts),
        checkout_root,
    )
