"""The change-request body limit has one source, and ``code_host`` does not import ``sisyphus``."""

import ast
from pathlib import Path

import dotharness.code_host
from dotharness.code_host.limits import DEFAULT_MAX_CHANGE_REQUEST_BODY_CHARS
from dotharness.code_host.reference import DECLARATION
from dotharness.sisyphus import prbody


def test_body_limit_has_a_single_source() -> None:
    assert DEFAULT_MAX_CHANGE_REQUEST_BODY_CHARS == 60000
    assert prbody.MAX_BODY_CHARS == DEFAULT_MAX_CHANGE_REQUEST_BODY_CHARS
    assert DECLARATION.limits.max_change_request_body_chars == DEFAULT_MAX_CHANGE_REQUEST_BODY_CHARS


def test_code_host_does_not_import_sisyphus() -> None:
    package_dir = Path(dotharness.code_host.__file__).parent
    offenders: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            offenders += [f"{path.name}: {n}" for n in names if n.split(".")[:2] == ["dotharness", "sisyphus"]]
    assert offenders == []
