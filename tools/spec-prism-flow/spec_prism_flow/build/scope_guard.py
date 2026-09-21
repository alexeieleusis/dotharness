from __future__ import annotations

from fnmatch import fnmatch

from spec_prism_flow.build.errors import ScopeViolation


def matches_any(path: str, globs: list[str]) -> bool:
    """Return True if `path` matches any glob via `fnmatch`.

    Unlike shell-glob exclusion tools (.gitignore, most CI path filters), `fnmatch`'s
    `*` crosses path separators -- e.g. `fnmatch("src/sub/x.py", "src/*.py")` is True.
    A pattern meant to mean "only directly under src/" must be written per-level, e.g.
    `src/*/*.py`, rather than assuming `src/*.py` stops at the first `/`.
    """
    return any(fnmatch(path, glob) for glob in globs)


def check(changed_paths: list[str], scope_globs: list[str], base_ref: str) -> None:
    offending = [p for p in changed_paths if not matches_any(p, scope_globs)]
    if offending:
        raise ScopeViolation(offending, scope_globs, base_ref)
