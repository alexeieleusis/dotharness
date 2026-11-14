"""``_validate_path`` rejects absolute and ``..`` paths and accepts everything else."""

from __future__ import annotations

import dataclasses

import pytest

from dotharness.sisyphus import stack as stack_mod
from dotharness.sisyphus.stack import Classified, _kind, _validate_path, classify_paths


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


@pytest.mark.parametrize(
    ("path", "bucket"),
    [
        ("foo.test.ts", "tests"),
        ("tests/x.py", "tests"),
        ("src/app/spec/a.spec.tsx", "tests"),
        ("pkg/test_foo.py", "tests"),
        ("pkg/foo_test.py", "tests"),
        ("pkg/contest.py", "sources"),
        ("src/a.PY", "sources"),
        ("README.md", None),
        ("pyproject.toml", None),
        ("logo.png", None),
        ("docs/guide.md", None),
        ("tests/data.json", None),
        ("", None),
    ],
)
def test_classify_single(path: str, bucket: str | None) -> None:
    result = classify_paths([path])
    expected = {"sources": (), "tests": ()}
    if bucket:
        expected[bucket] = (path,)
    assert result == Classified(sources=expected["sources"], tests=expected["tests"])


MIXED = ["z.py", "tests/b.py", "a.ts", "foo.test.ts", "README.md", "m/n.jsx", "pkg/test_a.py"]


def test_sorted_and_order_independent() -> None:
    result = classify_paths(MIXED)
    assert result.sources == ("a.ts", "m/n.jsx", "z.py")
    assert result.tests == ("foo.test.ts", "pkg/test_a.py", "tests/b.py")
    assert classify_paths(list(reversed(MIXED))) == result
    shuffled = [MIXED[i] for i in (3, 0, 5, 1, 6, 2, 4)]
    assert classify_paths(shuffled) == result


def test_duplicates_removed() -> None:
    assert classify_paths(["a.py", "a.py", "tests/t.py", "tests/t.py"]) == Classified(
        sources=("a.py",), tests=("tests/t.py",)
    )


@pytest.mark.parametrize("bad", ["/etc/hosts", "../README.md"])
def test_invalid_path_raises_even_when_it_would_be_dropped(bad: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        classify_paths([bad])
    assert bad in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        classify_paths(["a.py", bad, "b.py"])
    assert bad in str(excinfo.value)


def test_error_names_first_invalid_path() -> None:
    with pytest.raises(ValueError) as excinfo:
        classify_paths(["a.py", "../first.py", "/second.py"])
    assert "../first.py" in str(excinfo.value)
    assert "/second.py" not in str(excinfo.value)


def test_empty_input() -> None:
    result = classify_paths([])
    assert result == Classified(sources=(), tests=())
    assert result.sources == ()
    assert result.tests == ()


def test_classified_is_frozen() -> None:
    c = classify_paths(["a.py"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.sources = ()  # ty: ignore[invalid-assignment]


def test_single_source_equals_literal() -> None:
    assert classify_paths(["a.py"]) == Classified(sources=("a.py",), tests=())


def test_input_not_mutated_and_result_tuples() -> None:
    paths = ["b.py", "a.py", "tests/t.py"]
    copy = list(paths)
    result = classify_paths(paths)
    assert paths == copy
    assert isinstance(result.sources, tuple)
    assert isinstance(result.tests, tuple)


def test_module_docstring_records_rule() -> None:
    doc = stack_mod.__doc__ or ""
    for needle in ("tests", "__tests__", "test_*.py", "*.spec.*", ".tsx", "case-sensitive"):
        assert needle in doc
