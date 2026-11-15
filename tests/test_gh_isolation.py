"""R-GH-4: GitHub CLI knowledge stays inside the reference code host and cannot regress into the rest of ``src``."""

import importlib
import re
from pathlib import Path

import pytest

import dotharness

SRC = Path(dotharness.__file__).parent
# The reference implementation (``code_host/reference*``), its bundled comments script, and the fake host's
# stand-in credential variable names are the only places allowed to name the GitHub CLI or its variables.
EXEMPT_PREFIXES = ("code_host/reference", "code_host/scripts/", "code_host/testing.py")
FORBIDDEN = {
    "the old module": re.compile(r"dotharness\.gh\b|from dotharness import gh\b"),
    "gh_argv": re.compile(r"\bgh_argv\b"),
    "a credential variable": re.compile(r"\b(?:GITHUB_TOKEN|GH_TOKEN)\b"),
    "a gh argument list": re.compile(r"""\[\s*["']gh["']"""),
    "a gh command line": re.compile(r"\bgh (?:pr|api|repo|issue|run|auth|release|workflow)\b"),
}


def _scanned_files() -> list[Path]:
    files = []
    for path in sorted(SRC.rglob("*")):
        relative = path.relative_to(SRC).as_posix()
        if not path.is_file() or "__pycache__" in path.parts or relative.startswith(EXEMPT_PREFIXES):
            continue
        files.append(path)
    return files


def test_scan_covers_code_and_knowledge_prompts() -> None:
    suffixes = {path.suffix for path in _scanned_files()}
    assert {".py", ".md"} <= suffixes


@pytest.mark.parametrize("label", list(FORBIDDEN))
def test_gh_knowledge_stays_in_the_reference_host(label: str) -> None:
    pattern = FORBIDDEN[label]
    hits = []
    for path in _scanned_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits.extend(
            f"{path.relative_to(SRC).as_posix()}:{number}"
            for number, line in enumerate(text.splitlines(), start=1)
            if pattern.search(line)
        )
    assert hits == [], f"{label} found outside the reference code host: {hits}"


def test_gh_module_lives_only_in_the_reference_package() -> None:
    assert not (SRC / "gh.py").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("dotharness.gh")
    from dotharness.code_host.reference_gh import gh_argv

    assert gh_argv("pr", "view") == ["gh", "pr", "view"]
    assert gh_argv() == ["gh"]
