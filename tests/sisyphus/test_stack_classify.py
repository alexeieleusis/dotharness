"""``_validate_path`` rejects absolute and ``..`` paths and accepts everything else."""

from __future__ import annotations

import pytest

from dotharness.sisyphus.stack import _validate_path


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
