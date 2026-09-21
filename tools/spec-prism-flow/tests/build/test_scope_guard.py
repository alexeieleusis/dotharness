from pathlib import Path

import pytest

from spec_prism_flow.build.errors import ScopeViolation
from spec_prism_flow.build.scope_guard import check, matches_any


@pytest.mark.parametrize(
    ("path", "globs", "expected"),
    [
        ("spec_prism_flow/build/scope_guard.py", ["spec_prism_flow/build/*.py"], True),
        ("spec_prism_flow/cli.py", ["spec_prism_flow/build/*.py"], False),
        ("anything.py", [], False),
    ],
)
def test_matches_any(path, globs, expected):
    assert matches_any(path, globs) is expected


def test_matches_any_rejects_path_match_style_bypass():
    # `Path("evil/src/App.tsx").match("src/*")` is True because Path.match anchors from the
    # right for a relative pattern -- a real bypass for a check whose job is exclusion.
    # matches_any must use fnmatch, which anchors the whole string, and reject it.
    assert Path("evil/src/App.tsx").match("src/*")
    assert not matches_any("evil/src/App.tsx", ["src/*"])


def test_check_returns_none_when_all_paths_match_at_least_one_glob():
    assert check(["a/x.py", "b/y.py"], ["a/*.py", "b/*.py"], "main") is None


def test_check_raises_scope_violation_listing_exactly_the_offending_paths():
    with pytest.raises(ScopeViolation) as exc_info:
        check(["a/x.py", "evil/App.tsx", "b/y.py"], ["a/*.py", "b/*.py"], "main")

    err = exc_info.value
    assert err.offending_files == ["evil/App.tsx"]
    assert err.allowed_globs == ["a/*.py", "b/*.py"]
    assert err.base_ref == "main"


def test_check_does_not_use_base_ref_beyond_formatting_the_hint():
    # base_ref is not resolved or validated -- an arbitrary string is accepted.
    assert check(["a/x.py"], ["a/*.py"], "not-a-real-ref") is None
