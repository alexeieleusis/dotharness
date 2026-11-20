import pytest

from dotharness.pr_review.runners.self_review import has_test_and_non_test_files, is_test_file_path


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_foo.py",
        "test/foo.py",
        "src/app/tests/helpers.py",
        "web/src/test/foo.ts",
        "pkg/test_foo.py",
        "pkg/foo_test.py",
        "web/Foo.test.ts",
        "web/Foo.test.tsx",
        "web/foo.test.js",
        "web/foo.spec.ts",
        "web/foo.spec.js",
    ],
)
def test_is_test_file_path_true(path):
    assert is_test_file_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "src/foo.py",
        "README.md",
        "src/contest/foo.py",
        "src/latest.py",
        "src/testing/foo.py",
        "web/foo.ts",
        "web/foo.spec.py",
        "pkg/foo_test.go",
    ],
)
def test_is_test_file_path_false(path):
    assert not is_test_file_path(path)


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (["src/foo.py", "tests/test_foo.py"], True),
        (["web/Foo.tsx", "web/Foo.test.tsx", "README.md"], True),
        (["tests/test_foo.py"], False),
        (["src/foo.py", "src/bar.py"], False),
        ([], False),
    ],
)
def test_has_test_and_non_test_files(files, expected):
    assert has_test_and_non_test_files(files) is expected
