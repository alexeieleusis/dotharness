from __future__ import annotations

from fnmatch import fnmatch

from spec_prism_flow.build.errors import ScopeViolation


def matches_any(path: str, globs: list[str]) -> bool:
    return any(fnmatch(path, glob) for glob in globs)


def check(changed_paths: list[str], scope_globs: list[str], base_ref: str) -> None:
    offending = [p for p in changed_paths if not matches_any(p, scope_globs)]
    if offending:
        raise ScopeViolation(offending, scope_globs, base_ref)
