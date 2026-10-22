"""Focused tests for `local_review.findings`: parsing, id stamping and import isolation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from harness.runners.common import run_cmd
from harness.runners.local_review import findings
from harness.runners.local_review.findings import (
    AddressFinding,
    finding_id,
    parse_findings,
    sha8,
    stamp_findings,
    stamp_findings_file,
)

PKG = "harness.runners.local_review"

GOOD = """## Finding: Off by one
- severity: P0
- file: src/a.py
- line: 12
- status: open

Body text."""

NO_LINE = """## Finding: No line
- severity: P1
- file: src/b.py
- status: open

Body."""


def test_isolation_findings_imports_without_backend_lock_or_orchestrators() -> None:
    # The package `__init__` is eager, so load the submodule under a stand-in package (same
    # `__path__`, no `__init__` code) to observe the module's own import graph.
    pkg_dir = str(Path(findings.__file__).parent)
    code = (
        f"import importlib, sys, types\n"
        f"pkg = types.ModuleType('{PKG}'); pkg.__path__ = [{pkg_dir!r}]; sys.modules['{PKG}'] = pkg\n"
        f"importlib.import_module('{PKG}.findings')\n"
        f"bad = [m for m in ('harness.backend', 'harness.lock', '{PKG}.review', '{PKG}.address') if m in sys.modules]\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)"
    )
    proc = run_cmd([sys.executable, "-c", code], cwd=os.getcwd(), env=dict(os.environ), timeout=60, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_parse_valid_block_fields() -> None:
    (blk,) = parse_findings(GOOD + "\n")
    assert (blk.title, blk.severity, blk.file, blk.line, blk.status) == ("Off by one", "P0", "src/a.py", 12, "open")
    assert blk.valid
    assert blk.id is None
    assert blk.text == GOOD


def test_parse_flags_missing_line_as_invalid() -> None:
    (blk,) = parse_findings(NO_LINE)
    assert not blk.valid
    assert blk.line is None


def test_parse_rejects_malformed_id() -> None:
    text = GOOD.replace("- status: open", "- status: open\n- id: ../../evil")
    (blk,) = parse_findings(text)
    assert not blk.valid


def test_parse_ignores_headings_inside_code_fences() -> None:
    text = "```\n## Finding: fake\n```\n\n" + GOOD
    assert [b.title for b in parse_findings(text)] == ["Off by one"]


def test_block_ends_at_next_h2() -> None:
    text = GOOD + "\n\n## Summary\n\nDone.\n"
    (blk,) = parse_findings(text)
    assert "Summary" not in blk.text


def test_sha8_and_finding_id() -> None:
    assert len(sha8("x")) == 8
    assert finding_id("file", GOOD) == f"file-{sha8(GOOD)}"


def test_stamp_inserts_id_after_metadata_and_counts() -> None:
    res = stamp_findings(GOOD + "\n\n" + NO_LINE + "\n", "design")
    assert (res.stamped, res.already_stamped, res.unparseable) == (1, 0, 1)
    assert f"- status: open\n- id: design-{sha8(GOOD)}\n\nBody text." in res.text
    assert NO_LINE in res.text


def test_stamp_handles_blank_line_before_metadata() -> None:
    block = "## Finding: X\n\n- severity: P1\n- file: a.py\n- line: 3\n- status: open"
    res = stamp_findings(block + "\n", "file")
    assert res.stamped == 1
    assert len(res.blocks) == 1
    assert res.blocks[0].valid
    assert res.blocks[0].id == f"file-{sha8(block)}"


def test_stamp_is_idempotent() -> None:
    first = stamp_findings(GOOD + "\n", "file")
    second = stamp_findings(first.text, "file")
    assert second.text == first.text
    assert (second.stamped, second.already_stamped) == (0, 1)


def test_stamp_rejects_unknown_pass() -> None:
    with pytest.raises(ValueError, match="pass_name"):
        stamp_findings(GOOD, "summary")


def test_stamp_file_rewrites_only_when_stamped(tmp_path: Path) -> None:
    path = tmp_path / "f.md"
    path.write_text(GOOD + "\n", encoding="utf-8")
    assert stamp_findings_file(path, "file").stamped == 1
    stamped_text = path.read_text(encoding="utf-8")
    assert "- id: file-" in stamped_text
    mtime = path.stat().st_mtime_ns
    assert stamp_findings_file(path, "file").stamped == 0
    assert path.stat().st_mtime_ns == mtime


def test_address_finding_defaults() -> None:
    (blk,) = parse_findings(NO_LINE)
    af = AddressFinding("file-00000000", "file", Path("x.md"), blk)
    assert (af.file, af.line) == ("src/b.py", 1)
