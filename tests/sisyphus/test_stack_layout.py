"""Tests for the per-file layout predicates in ``dotharness.sisyphus.stack``."""

from __future__ import annotations

from pathlib import Path

import pytest

from dotharness.sisyphus.stack import fastapi_layout, react_layout, read_source_lines


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["from fastapi import APIRouter"], "app/x.py: from fastapi import APIRouter"),
        (["  import fastapi"], "app/x.py: import fastapi"),
        (["router = APIRouter()"], "app/x.py: router = APIRouter()"),
        (["app = FastAPI()"], "app/x.py: app = FastAPI()"),
        (["x = 1", "# no framework here"], None),
        (["import fastapi_utils"], None),
        ([], None),
        (["x = 1", "import fastapi", "app = FastAPI()"], "app/x.py: import fastapi"),
    ],
)
def test_fastapi_layout(lines: list[str], expected: str | None) -> None:
    assert fastapi_layout("app/x.py", lines) == expected
    assert fastapi_layout("app/x.py", lines) == expected


@pytest.mark.parametrize(
    ("rel", "line", "matches"),
    [
        ("components/Foo.tsx", "import React from 'react'", True),
        ("components/Foo.tsx", 'import { useState } from "react"', True),
        ("components/Foo.tsx", "const r = require('react')", True),
        ("components/Foo.tsx", "import x from 'react-dom'", False),
        ("components/Foo.tsx", "import x from 'react/jsx-runtime'", True),
        ("src/util.ts", "import React from 'react'", False),
        ("src/hooks/useThing.ts", "import React from 'react'", True),
        ("src/components/a.js", "import React from 'react'", True),
        ("src/hooks/useThing.ts", "const x = 1", False),
        ("a.TSX", "import React from 'react'", True),
        ("src/Components/a.ts", "import React from 'react'", False),
        ("a.jsx", "  import React from 'react'  ", True),
    ],
)
def test_react_layout(rel: str, line: str, matches: bool) -> None:
    expected = f"{rel}: {line.strip()}" if matches else None
    assert react_layout(rel, [line]) == expected


def test_react_first_matching_line_wins() -> None:
    lines = ["x", "import a from 'react'", "const r = require('react')"]
    assert react_layout("a.tsx", lines) == "a.tsx: import a from 'react'"


def test_react_path_hint_ignores_empty_lines() -> None:
    assert react_layout("src/components/a.ts", []) is None
    assert react_layout("src/util.ts", []) is None


def test_read_existing_file(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("one\ntwo\n")
    result = read_source_lines(tmp_path, "a.py")
    assert result == (("one", "two"), None)
    assert isinstance(result[0], tuple)


def test_read_empty_file(tmp_path: Path) -> None:
    (tmp_path / "e.py").write_text("")
    assert read_source_lines(tmp_path, "e.py") == ((), None)


def test_read_missing_file(tmp_path: Path) -> None:
    lines, error = read_source_lines(tmp_path, "missing.py")
    assert lines == ()
    assert error is not None
    assert error.startswith("missing.py: FileNotFoundError: ")


def test_read_directory(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    lines, error = read_source_lines(tmp_path, "d")
    assert lines == ()
    assert error is not None
    assert error.startswith("d: IsADirectoryError: ")


def test_read_invalid_utf8(tmp_path: Path) -> None:
    (tmp_path / "b.py").write_bytes(b"ok\n\xff\xfe bad\n")
    lines, error = read_source_lines(tmp_path, "b.py")
    assert error is None
    assert lines[0] == "ok"
    assert "�" in lines[1]


def test_react_multiline_import_matches() -> None:
    lines = ["import {", "  useState,", '} from "react";', "export const A = 1"]
    assert react_layout("src/A.tsx", lines) == 'src/A.tsx: } from "react";'


def test_react_jsx_runtime_matches() -> None:
    assert react_layout("a.tsx", ["import { jsx } from 'react/jsx-runtime'"]) is not None


def test_fastapi_users_not_fastapi() -> None:
    assert fastapi_layout("a.py", ["from fastapi_users import X"]) is None
