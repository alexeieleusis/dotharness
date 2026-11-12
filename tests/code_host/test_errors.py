"""Tests for the code-host error family and the vibe_heal aliases."""

import pytest

from dotharness.code_host import errors
from dotharness.code_host.errors import (
    AuthenticationError,
    CodeHostError,
    CodeHostTimeoutError,
    ConfigurationError,
    NoChangeRequestError,
    UnsupportedCapabilityError,
)

SUBCLASSES = [
    AuthenticationError,
    CodeHostTimeoutError,
    UnsupportedCapabilityError,
    ConfigurationError,
    NoChangeRequestError,
]


@pytest.mark.parametrize("cls", SUBCLASSES)
def test_subclass_of_root(cls: type[Exception]) -> None:
    assert issubclass(cls, CodeHostError)
    assert issubclass(CodeHostError, Exception)


def test_capability_attribute() -> None:
    exc = UnsupportedCapabilityError("no checks", capability="Checks")
    assert exc.capability == "Checks"
    assert str(exc) == "no checks"


def test_timeout_write_defaults_true() -> None:
    assert CodeHostTimeoutError("slow").write is True
    assert CodeHostTimeoutError("slow", write=False).write is False


def test_no_secret_in_text() -> None:
    sentinel = "<token>"
    for exc in (
        AuthenticationError("auth failed"),
        CodeHostTimeoutError("timed out"),
        UnsupportedCapabilityError("unsupported", capability="Checks"),
    ):
        assert sentinel not in str(exc)
        assert sentinel not in repr(exc)


def test_aliases_are_identical() -> None:
    from dotharness.vibe_heal.review import github

    assert github.GitHubReviewError is CodeHostError
    assert github.NoOpenPrError is NoChangeRequestError
    assert github.GitHubTimeoutError is CodeHostTimeoutError


def test_old_import_paths() -> None:
    from dotharness.vibe_heal.review import NoOpenPrError
    from dotharness.vibe_heal.review.github import GitHubReviewError, GitHubTimeoutError
    from dotharness.vibe_heal.review.github import NoOpenPrError as GithubNoOpenPrError

    assert NoOpenPrError is GithubNoOpenPrError is NoChangeRequestError
    assert issubclass(NoOpenPrError, GitHubReviewError)
    assert issubclass(GitHubTimeoutError, GitHubReviewError)


def test_old_root_catches_subclasses() -> None:
    from dotharness.vibe_heal.review.github import GitHubReviewError, GitHubTimeoutError, NoOpenPrError

    for exc in (
        NoOpenPrError("x"),
        GitHubTimeoutError("y"),
        *(c("z") for c in (AuthenticationError, ConfigurationError)),
    ):
        with pytest.raises(GitHubReviewError):
            raise exc


def test_module_exports() -> None:
    assert errors.CodeHostError is CodeHostError
