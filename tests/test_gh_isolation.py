"""R-GH-4: GitHub CLI knowledge stays inside the reference code host and cannot regress into the rest of ``src``."""

import importlib
import re
from pathlib import Path

import pytest

import dotharness

SRC = Path(dotharness.__file__).parent
# The reference implementation and its bundled comments script are the only files allowed to name the GitHub CLI.
# Exact paths, not prefixes, so a new sibling file or script can never slip into the exemption.
EXEMPT_PATHS = frozenset({"code_host/reference.py", "code_host/reference_gh.py", "code_host/scripts/pr-comments.py"})
# The fake host may name the credential variables it stands in for, but nothing else: every other pattern still
# applies to it.
CREDENTIAL_ONLY_EXEMPT = {"code_host/testing.py": "a credential variable"}
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
        if not path.is_file() or "__pycache__" in path.parts or relative in EXEMPT_PATHS:
            continue
        files.append(path)
    return files


def test_exemptions_name_existing_files() -> None:
    for relative in EXEMPT_PATHS | CREDENTIAL_ONLY_EXEMPT.keys():
        assert (SRC / relative).is_file(), f"stale exemption: {relative}"


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
        relative = path.relative_to(SRC).as_posix()
        if CREDENTIAL_ONLY_EXEMPT.get(relative) == label:
            continue
        hits.extend(
            f"{relative}:{number}" for number, line in enumerate(text.splitlines(), start=1) if pattern.search(line)
        )
    assert hits == [], f"{label} found outside the reference code host: {hits}"


def test_gh_module_lives_only_in_the_reference_package() -> None:
    assert not (SRC / "gh.py").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("dotharness.gh")
    from dotharness.code_host.reference_gh import gh_argv

    assert gh_argv("pr", "view") == ["gh", "pr", "view"]
    assert gh_argv() == ["gh"]
