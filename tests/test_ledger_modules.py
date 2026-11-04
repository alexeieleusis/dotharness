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


# Only ``test_*.py``, ``conftest.py`` and anything under a directory named exactly ``tests`` are
# excluded. ``*_test.py`` (also collected by pytest's default ``python_files``) is deliberately NOT
# excluded: ``spec_prism_flow/build/manual_test.py`` is a real source module that needs a ledger
# row, and the source trees are not collected by pytest. ``test/`` and ``testing/`` directories
# are likewise treated as source, because no source tree uses them for tests.
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("a/tests/x.py", True),
        ("a/test_x.py", True),
        ("a/conftest.py", True),
        ("a/cli.py", False),
        # documented: *_test.py is a ledger module (see comment above)
        ("a/manual_test.py", False),
        # a ``tests`` directory at the top or middle of the path
        ("tests/x.py", True),
        ("tools/x/pkg/tests/sub/helper.py", True),
        ("tools/x/tests/pkg/helper.py", True),
        # nested conftest.py
        ("tools/x/pkg/sub/conftest.py", True),
        # ``test_*.py`` is excluded even when it is really a helper
        ("tools/x/pkg/test_utils.py", True),
        # only the exact name ``tests`` counts
        ("tools/x/pkg/test/x.py", False),
        ("tools/x/pkg/testing/x.py", False),
        ("tools/x/pkg/tests_helpers/x.py", False),
        ("tools/x/pkg/contest.py", False),
        ("tools/x/pkg/testimony.py", False),
    ],
)
def test_is_test_file(path: str, expected: bool) -> None:
    assert ledger_modules.is_test_file(path) is expected


def _git(repo: Path, *args: str) -> None:
    cmd = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args]
    subprocess.run(cmd, check=True, capture_output=True)  # noqa: S603


def test_list_modules_init_empty_and_non_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    files = {
        "src/pkg/__init__.py": "",  # empty: dropped
        "src/pkg/mod.py": "x = 1\n",
        "src/pkg/sub/__init__.py": "VALUE = 1\n",  # non-empty: kept, named after the package
        "src/pkg/sub/inner.py": "y = 2\n",
        "src/pkg/empty_sub/__init__.py": "\n",  # whitespace only: dropped
        "src/pkg/tests/__init__.py": "z = 3\n",  # under tests: dropped
        "src/pkg/test_helper.py": "",
        "src/pkg/conftest.py": "",
        "src/pkg/notes.txt": "not python",
    }
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    tree = ledger_modules.SourceTree("demo", "src/pkg", "src", commit)
    monkeypatch.setattr(ledger_modules, "TREES", (tree,))
    modules = ledger_modules.list_modules(repo, tmp_path / "unused")

    assert [(m.path, m.name) for m in modules] == [
        ("src/pkg/mod.py", "pkg.mod"),
        ("src/pkg/sub/__init__.py", "pkg.sub"),
        ("src/pkg/sub/inner.py", "pkg.sub.inner"),
    ]


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
