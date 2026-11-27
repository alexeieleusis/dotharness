"""Pure, read-only inputs of the ``prework`` stage: the pre-work file set and the vibe_heal "configured" detector.

Package A: no import of ``spec_prism_flow``, ``pr_review``, ``vibe_heal``, ``harness``, the orchestrator or ``prbody``.
The only side effects are one injected git call and file reads.
"""

# ruff: noqa: TRY003

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotharness.fileio import load_toml
from dotharness.sisyphus.plan import read_scope
from dotharness.sisyphus.stack import classify_paths

GitFn = Callable[[Sequence[str]], Awaitable[Any]]

_GLOB_CHARS = frozenset("*?[")


class PreworkInputError(RuntimeError):
    """The tree listing at ``origin/<base>`` could not be read."""


def _bracket_class(pattern: str, i: int) -> tuple[str, int]:
    """Translate the ``[...]`` at ``pattern[i]``; an unterminated ``[`` is a literal. Returns (regex, next index)."""
    end = pattern.find("]", i + 2)
    if end == -1:
        return re.escape("["), i + 1
    body = pattern[i + 1 : end]
    neg = body[:1] in ("!", "^")
    body = (body[1:] if neg else body).replace("\\", "\\\\")
    return f"(?!/)[{'^' if neg else ''}{body}]", end + 1


def _glob_token(pattern: str, i: int) -> tuple[str, int]:
    """Translate the glob token starting at ``pattern[i]``. Returns (regex, next index)."""
    if pattern.startswith("**/", i):
        return "(?:.*/)?", i + 3
    if pattern.startswith("**", i):
        return ".*", i + 2
    ch = pattern[i]
    if ch == "*":
        return "[^/]*", i + 1
    if ch == "?":
        return "[^/]", i + 1
    if ch == "[":
        return _bracket_class(pattern, i)
    return re.escape(ch), i + 1


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a Scope glob. ``*`` and ``?`` stay within one path segment, ``[...]`` is a segment character class,
    and ``**`` matches any number of characters including ``/`` (``a/**/b`` also matches ``a/b``)."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        piece, i = _glob_token(pattern, i)
        out.append(piece)
    return re.compile("".join(out))


async def _tree_listing(git: GitFn, base: str) -> list[str]:
    ref = f"origin/{base}"
    res = await git(["ls-tree", "-r", "--name-only", "-z", ref])
    if res.returncode != 0:
        raise PreworkInputError(f"git ls-tree {ref} failed: {str(res.stderr or res.stdout).strip()[-200:]}")
    return [p for p in str(res.stdout).split("\0") if p]


async def prework_set(phase_dir: Path, base: str, git: GitFn) -> tuple[str, ...]:
    """Sorted, de-duplicated repo-relative source paths named by the Scope of any leaf in ``phase_dir`` that exist at
    ``origin/<base>`` (the working tree is never read). Empty tuple when none qualify.

    Entries with ``*``, ``?`` or ``[`` are globs matched against the one tree listing; others are literal paths kept
    only if listed. Tests are dropped by ``classify_paths``; its ``ValueError`` (absolute or ``..`` entry) propagates,
    as does a failing git call (``PreworkInputError``).
    """
    entries = read_scope(phase_dir).entries
    classify_paths(entries)  # validate every entry first; raises ValueError
    listing = await _tree_listing(git, base)
    present = set(listing)
    found: set[str] = set()
    for entry in entries:
        if _GLOB_CHARS & set(entry):
            rx = glob_to_regex(entry)
            found.update(p for p in listing if rx.fullmatch(p))
        elif entry in present:
            found.add(entry)
    return classify_paths(sorted(found)).sources


# --- vibe_heal configured detector -------------------------------------------------------------------------------

_VIBEHEAL_VARS = ("SONARQUBE_URL", "SONARQUBE_TOKEN", "SONARQUBE_PROJECT_KEY")
_ENV_EXACT = (".env.vibeheal", "env.vibeheal")
_ENV_VARIANT_GLOBS = (".env.vibeheal.*", "env.vibeheal.*")
_ENV_FALLBACK = ".env"
_EXAMPLE_SUFFIXES = (".example", ".sample", ".template")
_PR_REVIEW_CONFIGS = (".harness.toml", "harness.toml", ".pr_review.toml", "pr_review.toml")


@dataclass(frozen=True)
class VibeHealDetection:
    """``configured`` is the verdict of ``_is_configured``; ``env_file`` is the file found, or ``None``."""

    configured: bool
    env_file: Path | None


def _is_configured(found_file: Path | None, env: Mapping[str, str]) -> bool:
    """The single OD-1 predicate. Option A (current): configured once an env file is found.

    Option B would be: file defines all of ``_VIBEHEAL_VARS``, or ``env`` does.
    """
    return found_file is not None


def _search_dirs(repo: Path, config_dir: Path) -> list[Path]:
    dirs: list[Path] = []
    for d in (repo, config_dir):
        if d not in dirs:
            dirs.append(d)
    return dirs


def _env_files_in(directory: Path) -> list[Path]:
    found = [directory / n for n in _ENV_EXACT]
    for pattern in _ENV_VARIANT_GLOBS:
        found.extend(sorted(p for p in directory.glob(pattern) if not p.name.endswith(_EXAMPLE_SUFFIXES)))
    found.append(directory / _ENV_FALLBACK)
    return [p for p in found if p.is_file()]


def _referenced_env_files(config_file: Path) -> list[Path]:
    """``env_file`` values of ``[[repo.subdir]]`` entries, relative ones resolved against the config's directory."""
    try:
        data = load_toml(config_file)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    repos = data.get("repo")
    repos = [repos] if isinstance(repos, dict) else repos
    if not isinstance(repos, list):
        return []
    found: list[Path] = []
    for repo in repos:
        subdirs = repo.get("subdir") if isinstance(repo, dict) else None
        for sub in subdirs if isinstance(subdirs, list) else []:
            value = sub.get("env_file") if isinstance(sub, dict) else None
            if isinstance(value, str) and value.strip():
                path = Path(value.strip()).expanduser()
                found.append(path if path.is_absolute() else config_file.parent / path)
    return found


def vibeheal_configured(
    repo: Path, config_dir: Path | None = None, env: Mapping[str, str] | None = None
) -> VibeHealDetection:
    """Look for vibe_heal configuration by file reads only: env files in ``repo`` then ``config_dir`` (the directory of
    ``.sisyphus.toml``; defaults to ``repo``), then env files referenced from a ``pr_review`` TOML in those directories.
    A malformed TOML counts as no reference. ``env`` defaults to the process environment.
    """
    environ = os.environ if env is None else env
    dirs = _search_dirs(repo, config_dir or repo)
    candidates = [p for d in dirs for p in _env_files_in(d)]
    candidates += [
        ref
        for d in dirs
        for name in _PR_REVIEW_CONFIGS
        if (d / name).is_file()
        for ref in _referenced_env_files(d / name)
        if ref.is_file()
    ]
    found = candidates[0] if candidates else None
    return VibeHealDetection(_is_configured(found, environ), found)
