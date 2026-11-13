"""``_validate_path`` rejects absolute and ``..`` paths and accepts everything else."""

from __future__ import annotations

import pytest

from dotharness.sisyphus.stack import _kind, _validate_path


@pytest.mark.parametrize("path", ["/etc/x.py", "../x.py", "a/../x.py", "//x"])
def test_invalid_paths_raise_naming_the_path(path: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        _validate_path(path)
    assert path in str(excinfo.value)


@pytest.mark.parametrize(
    "path",
    ["a/b.py", "src/a.PY", "tests/data.json", "a\\..\\b.py", "a/..b/c.py", "", "a/./b", "a//b"],
)
def test_valid_paths_return_none(path: str) -> None:
    assert _validate_path(path) is None
    assert _validate_path(path) is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("foo.test.ts", "test"),
        ("tests/x.py", "test"),
        ("src/app/spec/a.spec.tsx", "test"),
        ("pkg/test_foo.py", "test"),
        ("pkg/foo_test.py", "test"),
        ("pkg/__tests__/a.ts", "test"),
        ("pkg/contest.py", "source"),
        ("pkg/Tests/a.py", "source"),
        ("test.py", "source"),
        ("src/a.PY", "source"),
        ("test_x.PY", "source"),
        (".test.py", "test"),
        ("tests/data.json", None),
        ("README.md", None),
        ("pyproject.toml", None),
        ("logo.png", None),
        ("docs/guide.md", None),
        ("Makefile", None),
        ("README.test", None),
    ],
)
def test_kind(path: str, expected: str | None) -> None:
    assert _kind(path) == expected
    assert _kind(path) == expected
