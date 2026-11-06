"""Guard: the migrated plan package must not grow the build stage, backends or process-group killing."""

import ast
from pathlib import Path

import dotharness.spec_prism_flow as spec_prism_flow_pkg

FORBIDDEN_NAMES = frozenset({"Popen", "killpg", "ClaudeBackend", "OpencodeBackend"})
FORBIDDEN_TEXT = ("git reset --hard", "git clean")
BUILD_MODULE = "spec_prism_flow.build"


def _package_sources() -> list[Path]:
    return sorted(Path(next(iter(spec_prism_flow_pkg.__path__))).rglob("*.py"))


def _is_build_module(name: str) -> bool:
    return ("." + BUILD_MODULE + ".") in ("." + name + ".")


def _imports_build(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(_is_build_module(a.name) for a in node.names)
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if _is_build_module(module) or (node.level > 0 and (module == "build" or module.startswith("build."))):
            return True
        return any(a.name == "build" for a in node.names)
    return False


def find_violations(source: str) -> list[str]:
    """Return a description of each forbidden construct found in ``source``."""
    violations: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import | ast.ImportFrom) and _imports_build(node):
            violations.append(f"line {node.lineno}: imports build")
        elif isinstance(node, ast.ImportFrom):
            violations.extend(f"line {node.lineno}: imports {a.name}" for a in node.names if a.name in FORBIDDEN_NAMES)
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            violations.append(f"line {node.lineno}: uses {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            violations.append(f"line {node.lineno}: uses {node.attr}")
    violations.extend(f"text: {t}" for t in FORBIDDEN_TEXT if t in source)
    return violations


def test_package_sources_are_found():
    assert any(p.name == "decompose.py" for p in _package_sources())


def test_no_module_contains_forbidden_names():
    offenders = [
        f"{path.name}: {v}" for path in _package_sources() for v in find_violations(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_checker_flags_forbidden_constructs():
    cases = [
        "from .build import x",
        "from . import build",
        "from dotharness.spec_prism_flow import build",
        "import dotharness.spec_prism_flow.build",
        "from subprocess import Popen",
        "import subprocess as sp\nsp.Popen(['x'])",
        "from os import killpg",
        "import os\nos.killpg(1, 9)",
        "x = ClaudeBackend()",
        "x = OpencodeBackend()",
        "cmd = 'git reset --hard'",
        "cmd = 'git clean -fd'",
    ]
    for source in cases:
        assert find_violations(source), source


def test_checker_accepts_clean_source():
    assert find_violations("import json\nfrom pathlib import Path\n") == []
