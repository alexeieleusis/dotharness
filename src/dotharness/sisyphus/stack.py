"""Classify paths added in a Sisyphus run as source or test files.

This docstring is the single statement of the test/source rule; the other
docstrings in this module refer to it.

Paths are repo-relative POSIX strings. A path is classified as follows:

- Source extensions are ``.py``, ``.ts``, ``.tsx``, ``.js`` and ``.jsx``. The
  extension is lower-cased before the check (the one case-insensitive step).
- A path with no source extension is dropped: docs, config and assets appear
  in neither output.
- A source-extension path is a test if any directory component is ``tests``,
  ``test`` or ``__tests__``, or if its file name matches ``test_*.py``,
  ``*_test.py``, ``*.test.*`` or ``*.spec.*``. Matching is case-sensitive, and
  only paths with a source extension are eligible to be tests.
- The test check runs first, so ``foo.test.ts`` is a test, not a source.
- Every other source-extension path is a source.

The manifest readers below (``nearest_manifest_dir``, ``python_declaration``,
``package_declaration``) read only manifest files and use the standard library
only. A manifest is a regular file named ``pyproject.toml``, ``package.json`` or
matching ``requirements*.txt``.

The framework detection rules live in ``FRAMEWORK_RULES``; ``match_file`` applies them.

``group_added_files`` assigns each added test to one group or to none (steps a to d,
non-override case only). ``M(P)`` is ``nearest_manifest_dir(root, P)``:

a. For a test T, take ``M(T)``; each added source S has ``M(S)``.
b. Candidate groups hold at least one source S with ``M(S) == M(T)``, and only when
   ``M(T)`` is not ``None`` (``''``, the repo root, is valid). With exactly one
   candidate, T goes to it.
c. With several candidates, T goes to the group of the source S (among those with
   ``M(S) == M(T)``) whose directory shares the longest common directory prefix with
   T's directory, measured in whole path components. On a tie the alphabetically
   earlier set wins, ``generic`` last.
d. With no candidate, T goes to the ``generic`` group if one exists, else to no group;
   no source-less ``generic`` group is created.

Only added sources count: existing code that was not added is invisible to this rule.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Literal

from dotharness import paths

_SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".js", ".jsx"})
_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__"})
_TEST_NAME_PATTERNS = ("test_*.py", "*_test.py", "*.test.*", "*.spec.*")


def _validate_path(path: str) -> None:
    """Raise ``ValueError`` if ``path`` is absolute or has a ``..`` component.

    Part of the module rule above. Backslashes are not translated and the empty string is valid.
    """
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise ValueError(f"path must be repo-relative, got absolute path: {path}")  # noqa: TRY003
    if ".." in pure.parts:
        raise ValueError(f"path must not contain a '..' component: {path}")  # noqa: TRY003


def _kind(path: str) -> Literal["source", "test"] | None:
    """Return ``"source"``, ``"test"`` or ``None`` (dropped) per the module docstring."""
    pure = PurePosixPath(path)
    if pure.suffix.lower() not in _SOURCE_SUFFIXES:
        return None
    if any(part in _TEST_DIR_NAMES for part in pure.parts[:-1]):
        return "test"
    if any(fnmatchcase(pure.name, pattern) for pattern in _TEST_NAME_PATTERNS):
        return "test"
    return "source"


@dataclass(frozen=True)
class Classified:
    """Sorted, deduplicated source and test paths; see the module docstring."""

    sources: tuple[str, ...]
    tests: tuple[str, ...]


def classify_paths(paths: Sequence[str]) -> Classified:
    """Split ``paths`` into sources and tests per the module docstring.

    Every path is validated first (the first invalid one raises ``ValueError``),
    then classified. Each output tuple is sorted and deduplicated.
    """
    for path in paths:
        _validate_path(path)
    sources: set[str] = set()
    tests: set[str] = set()
    for path in paths:
        kind = _kind(path)
        if kind == "source":
            sources.add(path)
        elif kind == "test":
            tests.add(path)
    return Classified(sources=tuple(sorted(sources)), tests=tuple(sorted(tests)))


_MANIFEST_NAMES = frozenset({"pyproject.toml", "package.json"})
_REQUIREMENTS_PATTERN = "requirements*.txt"
_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _is_manifest_name(name: str) -> bool:
    return name in _MANIFEST_NAMES or fnmatchcase(name, _REQUIREMENTS_PATTERN)


def _dir_str(directory: PurePosixPath) -> str:
    return "" if directory == PurePosixPath("") else directory.as_posix()


def nearest_manifest_dir(root: Path, rel_path: str) -> str | None:
    """Return the closest ancestor directory of ``rel_path`` holding a manifest.

    Starts at the file's own directory and walks up to and including the repo
    root. Returns a repo-relative POSIX string (``""`` for the root) or ``None``.
    An ``OSError`` on a directory means it holds no manifest. Never opens a file.
    """
    directory = PurePosixPath(rel_path).parent
    while True:
        try:
            for entry in (root / directory).iterdir():
                if _is_manifest_name(entry.name) and entry.is_file():
                    return _dir_str(directory)
        except OSError:
            pass
        if directory == PurePosixPath(""):
            return None
        directory = directory.parent


@dataclass(frozen=True)
class Declaration:
    """Result of a dependency reader: evidence if declared, plus read/parse errors."""

    evidence: str | None
    errors: tuple[str, ...]


def _normalize(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _matches(dependency: str, name: str) -> bool:
    found = _NAME_RE.match(dependency)
    return found is not None and _normalize(found.group(1)) == _normalize(name)


def _prefix(manifest_dir: str) -> str:
    return manifest_dir + "/" if manifest_dir != "" else ""


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _error(manifest: str, exc: Exception) -> str:
    return f"{manifest}: {type(exc).__name__}: {exc}"


def _first_match(items: object, name: str) -> str | None:
    if not isinstance(items, list):
        return None
    for dep in items:
        if isinstance(dep, str) and _matches(dep, name):
            return dep
    return None


def _project_evidence(project: object, name: str, manifest: str) -> str | None:
    if not isinstance(project, dict):
        return None
    dep = _first_match(project.get("dependencies"), name)
    if dep is not None:
        return f"{manifest}: project.dependencies: {dep}"
    extras = project.get("optional-dependencies")
    if isinstance(extras, dict):
        for extra in sorted(extras):
            dep = _first_match(extras[extra], name)
            if dep is not None:
                return f"{manifest}: project.optional-dependencies.{extra}: {dep}"
    return None


def _poetry_evidence(tool: object, name: str, manifest: str) -> str | None:
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    deps = poetry.get("dependencies") if isinstance(poetry, dict) else None
    if isinstance(deps, dict):
        for key in deps:
            if isinstance(key, str) and _matches(key, name):
                return f"{manifest}: tool.poetry.dependencies: {key}"
    return None


def _pyproject_evidence(data: object, name: str, manifest: str) -> str | None:
    if not isinstance(data, dict):
        return None
    return _project_evidence(data.get("project"), name, manifest) or _poetry_evidence(data.get("tool"), name, manifest)


def _requirements_evidence(text: str, name: str, manifest: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "" or stripped.startswith(("#", "-")):
            continue
        if _matches(stripped, name):
            return f"{manifest}: {stripped}"
    return None


def python_declaration(root: Path, manifest_dir: str, name: str = "fastapi") -> Declaration:
    """Report whether ``manifest_dir`` declares ``name`` in a Python manifest.

    Consults ``pyproject.toml`` then ``requirements*.txt`` sorted by file name;
    the first manifest declaring ``name`` supplies the evidence and later ones
    are not read. Read and parse failures become ``errors`` entries.
    """
    directory = root / manifest_dir
    prefix = _prefix(manifest_dir)
    errors: list[str] = []
    candidates: list[str] = []
    if (directory / "pyproject.toml").is_file():
        candidates.append("pyproject.toml")
    try:
        names = sorted(
            entry.name
            for entry in directory.iterdir()
            if fnmatchcase(entry.name, _REQUIREMENTS_PATTERN) and entry.is_file()
        )
    except OSError:
        names = []
    candidates.extend(names)
    for file_name in candidates:
        manifest = prefix + file_name
        try:
            text = _read_text(directory / file_name)
            if file_name == "pyproject.toml":
                evidence = _pyproject_evidence(tomllib.loads(text), name, manifest)
            else:
                evidence = _requirements_evidence(text, name, manifest)
        except (tomllib.TOMLDecodeError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            errors.append(_error(manifest, exc))
            continue
        if evidence is not None:
            return Declaration(evidence, tuple(errors))
    return Declaration(None, tuple(errors))


def package_declaration(root: Path, manifest_dir: str, name: str = "react") -> Declaration:
    """Report whether ``package.json`` in ``manifest_dir`` lists ``name`` under ``dependencies``."""
    path = root / manifest_dir / "package.json"
    if not path.is_file():
        return Declaration(None, ())
    manifest = _prefix(manifest_dir) + "package.json"
    try:
        data = json.loads(_read_text(path))
    except (tomllib.TOMLDecodeError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return Declaration(None, (_error(manifest, exc),))
    deps = data.get("dependencies") if isinstance(data, dict) else None
    if isinstance(deps, dict):
        for key in deps:
            if _normalize(key) == _normalize(name):
                return Declaration(f"{manifest}: dependencies: {key}", ())
    return Declaration(None, ())


_FASTAPI_IMPORT = re.compile(r"^\s*(from|import)\s+fastapi\b")
_REACT_MODULE = r"['\"]react(?:/jsx-runtime)?['\"]"
_REACT_IMPORT = re.compile(
    rf"\bfrom\s+{_REACT_MODULE}|^\s*import\s+{_REACT_MODULE}|\brequire\(\s*{_REACT_MODULE}\s*\)", re.M
)
_REACT_SUFFIXES = frozenset({".tsx", ".jsx"})
_REACT_HINT_SUFFIXES = frozenset({".ts", ".js"})
_REACT_HINT_DIRS = frozenset({"components", "hooks"})


def read_source_lines(root: Path, rel_path: str) -> tuple[tuple[str, ...], str | None]:
    """Return the lines of ``root / rel_path`` and ``None``, or ``()`` and an error entry.

    The error entry is ``<rel_path>: <ExceptionClass>: <error text>``. Only ``OSError`` is caught;
    invalid UTF-8 bytes become U+FFFD. Never raises.
    """
    lines: tuple[str, ...] = ()
    error: str | None = None
    try:
        text = (root / rel_path).read_bytes().decode("utf-8", errors="replace")
        lines = tuple(text.splitlines())
    except OSError as exc:
        error = f"{rel_path}: {type(exc).__name__}: {exc}"
    return lines, error


def fastapi_layout(rel_path: str, lines: Sequence[str]) -> str | None:
    """Return ``<rel_path>: <stripped line>`` for the first FastAPI-looking line, else ``None``."""
    for line in lines:
        if _FASTAPI_IMPORT.match(line) or "APIRouter" in line or "FastAPI(" in line:
            return f"{rel_path}: {line.strip()}"
    return None


def _react_path_qualifies(rel_path: str) -> bool:
    path = PurePosixPath(rel_path)
    suffix = path.suffix.lower()
    if suffix in _REACT_SUFFIXES:
        return True
    return suffix in _REACT_HINT_SUFFIXES and any(part in _REACT_HINT_DIRS for part in path.parts[:-1])


def react_layout(rel_path: str, lines: Sequence[str]) -> str | None:
    """Return ``<rel_path>: <stripped line>`` for the first React import or require, else ``None``.

    Matching runs on the whole text, so a multi-line ``import {...} from 'react'`` is found; the
    evidence is the line holding the match. ``react/jsx-runtime`` also counts.

    The path must first qualify: ``.tsx``/``.jsx``, or ``.ts``/``.js`` under a ``components`` or
    ``hooks`` directory.
    """
    if not _react_path_qualifies(rel_path):
        return None
    text = "\n".join(lines)
    found = _REACT_IMPORT.search(text)
    if found is None:
        return None
    start = text.rfind("\n", 0, found.start()) + 1
    end = text.find("\n", found.end())
    return f"{rel_path}: {text[start : end if end != -1 else len(text)].strip()}"


@dataclass(frozen=True)
class FileMatch:
    """The pattern set chosen for one file, with reviewer-readable evidence."""

    set: str
    evidence: tuple[str, ...]
    fallback: bool


@dataclass(frozen=True)
class FrameworkRule:
    """One framework: its set name, file extensions, manifest reader and layout predicate."""

    set_name: str
    extensions: tuple[str, ...]
    read_manifest: Callable[[Path, str], Declaration]
    layout: Callable[[str, Sequence[str]], str | None]


FRAMEWORK_RULES: tuple[FrameworkRule, ...] = (
    FrameworkRule("fastapi", (".py",), python_declaration, fastapi_layout),
    FrameworkRule("react", (".tsx", ".jsx", ".ts", ".js"), package_declaration, react_layout),
)


def match_file(root: Path, rel_path: str, set_names: Collection[str]) -> FileMatch:
    """Return the framework set that fits ``rel_path``, or ``generic`` with any read errors.

    A set is chosen only if its name is in ``set_names``, the extension fits the row, the nearest
    manifest directory declares the dependency and the layout matches. Rows come from
    ``FRAMEWORK_RULES`` in order. Errors appear as evidence only on a ``generic`` result.
    """
    manifest_dir = nearest_manifest_dir(root, rel_path)
    if manifest_dir is None:
        return FileMatch("generic", (), True)
    suffix = PurePosixPath(rel_path).suffix.lower()
    errors: list[str] = []
    source: tuple[tuple[str, ...], str | None] | None = None
    for row in FRAMEWORK_RULES:
        if row.set_name not in set_names or suffix not in row.extensions:
            continue
        declaration = row.read_manifest(root, manifest_dir)
        errors.extend(declaration.errors)
        if declaration.evidence is None:
            continue
        first_read = source is None
        if source is None:
            source = read_source_lines(root, rel_path)
        lines, error = source
        if error is not None:
            if first_read:
                errors.append(error)
            continue
        layout_evidence = row.layout(rel_path, lines)
        if layout_evidence is not None:
            return FileMatch(row.set_name, (declaration.evidence, layout_evidence), False)
    return FileMatch("generic", tuple(dict.fromkeys(errors)), True)


class StackError(ValueError):
    """Configuration-like problem raised by this module (for example a missing ``generic`` set)."""


def pattern_set_names() -> tuple[str, ...]:
    """Return the sorted names of the pattern sets under ``knowledge/patterns/``.

    A set is a directory holding ``QUICK_START_MIGRATION.md``. Raises ``StackError`` when ``generic`` is not a set.
    """
    patterns = paths.default_knowledge_dir() / "patterns"
    names: list[str] = []
    if patterns.is_dir():
        names = [
            child.name
            for child in patterns.iterdir()
            if child.is_dir() and (child / "QUICK_START_MIGRATION.md").is_file()
        ]
    if "generic" not in names:
        raise StackError("knowledge/patterns/generic is missing")  # noqa: TRY003
    return tuple(sorted(names))


@dataclass(frozen=True)
class StackGroup:
    """One group of added files sharing a matched pattern set (no validation; the grouping code sorts)."""

    set: str
    files: tuple[str, ...]
    tests: tuple[str, ...]
    evidence: tuple[str, ...]
    fallback: bool


def merge_evidence(per_file: Sequence[Sequence[str]]) -> tuple[str, ...]:
    """Concatenate per-file evidence in order, dropping exact duplicates (first occurrence kept)."""
    merged: list[str] = []
    for entries in per_file:
        for entry in entries:
            if entry not in merged:
                merged.append(entry)
    return tuple(merged)


def group_added_files(
    root: Path,
    added: Sequence[str],
    set_names: Collection[str] | None = None,
    pattern_set: str | None = None,
) -> tuple[StackGroup, ...]:
    """Group the added source files by pattern set.

    With ``pattern_set`` given, all sources form one group and no manifest is read.
    Otherwise each source file lands in exactly one group: framework groups in
    ascending set-name order, then ``generic`` last. ``tests`` follows steps a to d of
    the module docstring (with an override, all added tests go to the single group).
    """
    if set_names is None:
        set_names = pattern_set_names()
    classified = classify_paths(added)
    sources = tuple(sorted(set(classified.sources)))
    if not sources:
        return ()
    if pattern_set is not None:
        if pattern_set not in set_names:
            valid = ", ".join(sorted(set_names))
            raise StackError(f"unknown pattern set {pattern_set!r}; valid sets: {valid}")  # noqa: TRY003
        return (
            StackGroup(
                set=pattern_set,
                files=sources,
                tests=classified.tests,
                evidence=(),
                fallback=False,
            ),
        )
    files: dict[str, list[str]] = {}
    evidence: dict[str, list[tuple[str, ...]]] = {}
    for path in sources:
        match = match_file(root, path, set_names)
        files.setdefault(match.set, []).append(path)
        evidence.setdefault(match.set, []).append(tuple(match.evidence))
    names = sorted(name for name in files if name != "generic")
    if "generic" in files:
        names.append("generic")
    assigned = _assign_tests(root, names, files, sorted(set(classified.tests)))
    return tuple(
        StackGroup(
            set=name,
            files=tuple(files[name]),
            tests=tuple(sorted(assigned[name])),
            evidence=merge_evidence(evidence[name]),
            fallback=name == "generic",
        )
        for name in names
    )


def _dir_parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(path).parent.parts


def _shared_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    count = 0
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        count += 1
    return count


def _assign_tests(
    root: Path,
    names: Sequence[str],
    files: dict[str, list[str]],
    tests: Sequence[str],
) -> dict[str, list[str]]:
    """Assign each test to one group (``names`` is in output order) or to none; see the module docstring."""
    assigned: dict[str, list[str]] = {name: [] for name in names}
    source_dirs = {
        name: [(nearest_manifest_dir(root, path), _dir_parts(path)) for path in files[name]] for name in names
    }
    for test in tests:
        manifest = nearest_manifest_dir(root, test)
        best: str | None = None
        if manifest is not None:
            test_dir = _dir_parts(test)
            best_score = -1
            for name in names:
                scores = [_shared_prefix(parts, test_dir) for found, parts in source_dirs[name] if found == manifest]
                if scores and max(scores) > best_score:
                    best, best_score = name, max(scores)
        if best is None and "generic" in assigned:
            best = "generic"
        if best is not None:
            assigned[best].append(test)
    return assigned
