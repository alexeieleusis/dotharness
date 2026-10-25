"""The stage and retry-key constants are defined once; RESET_KEYS only names counters that are really written."""

from __future__ import annotations

import ast
from pathlib import Path

import sisyphus
from sisyphus import stages
from sisyphus.resume import RESET_KEYS

PKG = Path(sisyphus.__file__).parent


def _written_retry_keys() -> set[str]:
    """Values of every key passed to ``increment_retry`` / ``reset_retries`` / ``retries[...]=`` in the package."""
    consts = {n: v for n, v in vars(stages).items() if n.endswith("_KEY") and isinstance(v, str)}
    found: set[str] = set()

    def resolve(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        return None

    for path in PKG.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "increment_retry"
            ):
                key = resolve(node.args[0])
                if key:
                    found.add(key)
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "retries"
            ):
                key = resolve(node.slice)
                if key:
                    found.add(key)
    return found


def test_every_reset_key_is_written_by_sisyphus() -> None:
    written = _written_retry_keys()
    assert {"build:retry-cycles", "auto-retry"} <= written
    unused = {k for keys in RESET_KEYS.values() for k in keys} - written
    assert not unused


def test_reset_keys_cover_every_stage() -> None:
    assert tuple(RESET_KEYS) == stages.STAGE_ORDER
