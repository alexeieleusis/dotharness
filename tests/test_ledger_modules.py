import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VIBE_HEAL = Path.home() / "development" / "vibe-heal"

_spec = importlib.util.spec_from_file_location("ledger_modules", ROOT / "scripts" / "ledger_modules.py")
assert _spec is not None
assert _spec.loader is not None
ledger_modules = importlib.util.module_from_spec(_spec)
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


@pytest.mark.skipif(not _have_commits(), reason="source commits not available")
def test_ledger_rows_match_script() -> None:
    modules = ledger_modules.list_modules(ROOT, VIBE_HEAL)
    ledger = (ROOT / "docs" / "migration-ledger.md").read_text()
    rows = re.findall(r"^\| `[^`]+` \| .*$", ledger, flags=re.MULTILINE)
    assert len(rows) == len(modules)
    assert all("| not migrated |" in row for row in rows)
    for module in modules:
        assert f"| `{module.path}` |" in ledger
