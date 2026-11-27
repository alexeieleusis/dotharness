"""Pre-work set: Scope reader, glob matching against the ``origin/<base>`` listing, classification."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from dotharness.sisyphus.plan import read_scope, scope_section
from dotharness.sisyphus.prework_inputs import PreworkInputError, glob_to_regex, prework_set

TREE = [
    "src/a.py",
    "src/pkg/b.py",
    "src/pkg/deep/c.py",
    "src/pkg/readme.md",
    "tests/test_a.py",
    "web/app.ts",
    "web/app.test.ts",
    "x1.py",
]


def leaf(phase: Path, name: str, scope: list[str], extra: str = "") -> None:
    body = "\n".join(scope)
    (phase / f"{name}-leaf.md").write_text(f"## Scope\n{body}\n{extra}\n## Requirements\nr\n- not/scope.py\n")


def fake_git(tree=TREE, rc=0, calls=None):
    async def git(argv):
        if calls is not None:
            calls.append(list(argv))
        return SimpleNamespace(returncode=rc, stdout="\0".join(tree) + "\0", stderr="boom")

    return git


def run(phase: Path, tree=TREE, **kw):
    return asyncio.run(prework_set(phase, "main", fake_git(tree, **kw)))


def test_targets_origin_base_with_one_listing_call(tmp_path):
    leaf(tmp_path, "01-a", ["- src/a.py"])
    leaf(tmp_path, "02-b", ["- src/pkg/b.py"])
    calls: list = []
    result = asyncio.run(prework_set(tmp_path, "release/1", fake_git(calls=calls)))
    assert result == ("src/a.py", "src/pkg/b.py")
    assert len(calls) == 1
    assert calls[0][:3] == ["ls-tree", "-r", "--name-only"]
    assert calls[0][-1] == "origin/release/1"


def test_glob_semantics_pinned(tmp_path):
    leaf(tmp_path, "01-a", ["- src/*.py"])
    assert run(tmp_path) == ("src/a.py",)
    leaf(tmp_path, "01-a", ["- src/**/*.py"])
    assert run(tmp_path) == ("src/a.py", "src/pkg/b.py", "src/pkg/deep/c.py")  # ``**/`` also matches zero dirs
    leaf(tmp_path, "01-a", ["- src/**"])
    assert run(tmp_path) == ("src/a.py", "src/pkg/b.py", "src/pkg/deep/c.py")
    leaf(tmp_path, "01-a", ["- x?.py", "- x[0-9].py"])
    assert run(tmp_path) == ("x1.py",)


def test_glob_regex_does_not_cross_slash():
    assert not glob_to_regex("src/*.py").fullmatch("src/pkg/b.py")
    assert glob_to_regex("a/**/b").fullmatch("a/b")
    assert glob_to_regex("a/**/b").fullmatch("a/x/y/b")
    assert not glob_to_regex("x[!0-9].py").fullmatch("x1.py")
    assert not glob_to_regex("a[/]b").fullmatch("a/b")


def test_tests_and_non_source_dropped(tmp_path):
    leaf(tmp_path, "01-a", ["- tests/test_a.py", "- web/app.test.ts", "- web/app.ts", "- src/pkg/readme.md"])
    assert run(tmp_path) == ("web/app.ts",)


def test_missing_literal_dropped_and_empty_tuple(tmp_path):
    leaf(tmp_path, "01-a", ["- src/new.py", "- src/nope/*.py"])
    assert run(tmp_path) == ()


def test_dedup_and_ordering(tmp_path):
    leaf(tmp_path, "01-a", ["- web/app.ts", "- src/a.py"])
    leaf(tmp_path, "02-b", ["- src/*.py", "- src/a.py", "- web/*.ts"])
    assert run(tmp_path) == ("src/a.py", "web/app.ts")


def test_leaf_enumeration_uses_graph_json(tmp_path):
    leaf(tmp_path, "01-a", ["- src/a.py"])
    leaf(tmp_path, "02-b", ["- x1.py"])
    (tmp_path / "graph.json").write_text('{"nodes": ["01-a-leaf"]}')
    assert run(tmp_path) == ("src/a.py",)


def test_fenced_headers_do_not_end_scope(tmp_path):
    (tmp_path / "01-a-leaf.md").write_text(
        "## Scope\n- src/a.py\n```\n## Requirements\n- x1.py\n```\n- web/app.ts\n\n## Requirements\n- src/pkg/b.py\n"
    )
    # a fenced "## " does not end Scope; body lines are taken as the phase-file parser would (fence lines are skipped)
    assert run(tmp_path) == ("src/a.py", "web/app.ts", "x1.py")
    assert read_scope(tmp_path).skipped == ("```", "## Requirements", "```")


def test_missing_scope_or_leaf_contributes_nothing(tmp_path):
    (tmp_path / "01-a-leaf.md").write_text("## Requirements\n- src/a.py\n")
    (tmp_path / "graph.json").write_text('{"nodes": ["01-a-leaf", "02-gone-leaf"]}')
    assert run(tmp_path) == ()


def test_non_bullet_lines_recorded_as_skipped():
    assert scope_section("## Scope\n- a.py\nplain\n## X\n- b.py") == ["- a.py", "plain"]


def test_read_scope_skipped(tmp_path):
    leaf(tmp_path, "01-a", ["- a.py", "note line", "-bad"])
    r = read_scope(tmp_path)
    assert r.entries == ("a.py",)
    assert r.skipped == ("note line", "-bad")


@pytest.mark.parametrize("entry", ["/etc/x.py", "../x.py", "src/../x.py", "/abs/*.py"])
def test_invalid_path_raises_value_error(tmp_path, entry):
    leaf(tmp_path, "01-a", [f"- {entry}"])
    with pytest.raises(ValueError):
        run(tmp_path)


def test_git_failure_raised(tmp_path):
    leaf(tmp_path, "01-a", ["- src/a.py"])
    with pytest.raises(PreworkInputError, match="origin/main"):
        run(tmp_path, rc=128)


def test_literal_path_with_glob_chars_is_kept(tmp_path):
    leaf(tmp_path, "01-a", ["- app/[id]/page.tsx"])
    assert run(tmp_path, tree=["app/[id]/page.tsx", "app/i/page.tsx"]) == ("app/[id]/page.tsx",)


def test_malformed_glob_raises_input_error(tmp_path):
    leaf(tmp_path, "01-a", ["- x[z-a].py"])
    with pytest.raises(PreworkInputError, match="invalid Scope glob"):
        run(tmp_path)
