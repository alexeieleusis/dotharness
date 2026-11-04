"""List the modules that ``docs/migration-ledger.md`` must have a row for.

A module is one ``*.py`` file under a source tree, read from a fixed git commit,
excluding test files (``test_*.py``, ``conftest.py`` and anything under a
``tests`` directory) and ``__init__.py`` files that are empty. The files are read
with ``git ls-tree`` and ``git show``, so the working tree does not matter.

Usage::

    python scripts/ledger_modules.py                 # one "tool<TAB>path" per line
    python scripts/ledger_modules.py --count         # the row count
    python scripts/ledger_modules.py --markdown      # ledger rows
    python scripts/ledger_modules.py --snapshot      # tests/data/ledger_modules.tsv content
    python scripts/ledger_modules.py --vibe-heal PATH  # vibe-heal checkout (required)

The frozen result is checked in as ``tests/data/ledger_modules.tsv``; regenerate it
only if the seed commits change.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

TOOLS_COMMIT = "cb0efc1"
VIBE_HEAL_COMMIT = "9363868"
REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class SourceTree:
    """One source tree at a fixed commit."""

    tool: str
    path: str
    package_root: str  # the directory above the top-level package
    commit: str
    in_vibe_heal: bool = False


TREES = (
    SourceTree("pr-review", "tools/pr-review/harness", "tools/pr-review", TOOLS_COMMIT),
    SourceTree(
        "spec-prism-flow",
        "tools/spec-prism-flow/spec_prism_flow",
        "tools/spec-prism-flow",
        TOOLS_COMMIT,
    ),
    SourceTree("sisyphus", "tools/sisyphus/sisyphus", "tools/sisyphus", TOOLS_COMMIT),
    SourceTree("vibe-heal", "src/vibe_heal", "src", VIBE_HEAL_COMMIT, in_vibe_heal=True),
)


@dataclass(frozen=True)
class Module:
    tool: str
    path: str
    name: str


def _git(repo: Path, *args: str) -> bytes:
    cmd = ["git", "-C", str(repo), *args]
    return subprocess.run(cmd, check=True, capture_output=True).stdout  # noqa: S603


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name
    return "tests" in p.parts or name.startswith("test_") or name == "conftest.py"


def list_modules(repo_root: Path = REPO_ROOT, *, vibe_heal: Path) -> list[Module]:
    modules: list[Module] = []
    for tree in TREES:
        repo = vibe_heal if tree.in_vibe_heal else repo_root
        listing = _git(repo, "ls-tree", "-r", "--name-only", tree.commit, tree.path)
        for line in sorted(listing.decode().splitlines()):
            if not line.endswith(".py") or is_test_file(line):
                continue
            if line.endswith("/__init__.py") and not _git(repo, "show", f"{tree.commit}:{line}").strip():
                continue
            rel = PurePosixPath(line).relative_to(tree.package_root).with_suffix("")
            parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
            modules.append(Module(tree.tool, line, ".".join(parts)))
    return modules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vibe-heal", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--count", action="store_true")
    mode.add_argument("--markdown", action="store_true")
    mode.add_argument("--snapshot", action="store_true")
    args = parser.parse_args(argv)
    modules = list_modules(vibe_heal=args.vibe_heal)
    if args.count:
        print(len(modules))
    elif args.markdown:
        for m in modules:
            print(f"| `{m.name}` | {m.tool} | `{m.path}` | not migrated | | |")
    elif args.snapshot:
        for m in modules:
            print(f"{m.name}\t{m.tool}\t{m.path}")
    else:
        for m in modules:
            print(f"{m.tool}\t{m.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
