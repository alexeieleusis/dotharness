import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_VIBE_HEAL_ENV = os.environ.get("VIBE_HEAL_PATH")
VIBE_HEAL = Path(_VIBE_HEAL_ENV) if _VIBE_HEAL_ENV else Path.home() / "development" / "vibe-heal"

_spec = importlib.util.spec_from_file_location("ledger_modules", ROOT / "scripts" / "ledger_modules.py")
assert _spec is not None
assert _spec.loader is not None
ledger_modules = importlib.util.module_from_spec(_spec)
sys.modules["ledger_modules"] = ledger_modules
_spec.loader.exec_module(ledger_modules)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("a/tests/x.py", True),
        ("a/test_x.py", True),
        ("a/conftest.py", True),
        ("a/manual_test.py", False),
        ("a/cli.py", False),
    ],
)
def test_is_test_file(path: str, expected: bool) -> None:
    assert ledger_modules.is_test_file(path) is expected


def _have_commits() -> bool:
    if not VIBE_HEAL.exists():
        return False
    try:
        for repo, commit in ((ROOT, "cb0efc1"), (VIBE_HEAL, "9363868")):
            cmd = ["git", "-C", str(repo), "cat-file", "-e", commit]
            subprocess.run(cmd, check=True, capture_output=True)  # noqa: S603
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return True


def test_ledger_rows_match_script() -> None:
    if not _have_commits():
        message = "source commits not available (set VIBE_HEAL_PATH to a vibe-heal clone with 9363868)"
        # When CI explicitly configures VIBE_HEAL_PATH, a missing source must fail, not skip.
        if os.environ.get("CI") and _VIBE_HEAL_ENV:
            pytest.fail(message)
        pytest.skip(message)
    modules = ledger_modules.list_modules(ROOT, VIBE_HEAL)
    ledger = (ROOT / "docs" / "migration-ledger.md").read_text()
    ledger = ledger.split("\n## Rows\n", 1)[1]
    # Row format: | module | origin tool | origin path | status | shared module | issue |
    row_re = re.compile(r"^\| `([^`]+)` \|(.*)\|\s*$", flags=re.MULTILINE)
    allowed = {"not migrated", "migrated", "dropped"}
    ledger_rows: dict[str, tuple[str, str, str]] = {}
    duplicates: list[str] = []
    for match in row_re.finditer(ledger):
        cells = [c.strip() for c in match.group(2).split("|")]
        assert len(cells) == 5, match.group(0)
        tool, origin_path, status = cells[0], cells[1].strip("`"), cells[2]
        assert status in allowed, (origin_path, status)
        if origin_path in ledger_rows:
            duplicates.append(origin_path)
        ledger_rows[origin_path] = (match.group(1), tool, status)
    assert not duplicates, duplicates
    expected = {m.path: (m.name, m.tool) for m in modules}
    assert ledger_rows.keys() == expected.keys(), (
        sorted(expected.keys() - ledger_rows.keys()),
        sorted(ledger_rows.keys() - expected.keys()),
    )
    for origin_path, (name, tool) in expected.items():
        assert ledger_rows[origin_path][:2] == (name, tool), origin_path
