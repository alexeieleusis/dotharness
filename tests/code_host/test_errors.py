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


def test_vibe_heal_aliases_are_removed() -> None:
    import importlib

    from dotharness.vibe_heal import review

    assert not hasattr(review, "NoOpenPrError")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("dotharness.vibe_heal.review.github")


def test_module_exports() -> None:
    assert errors.CodeHostError is CodeHostError
