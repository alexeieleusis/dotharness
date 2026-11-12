"""Error family raised by code-host implementations.

No message, repr or attribute of these errors may hold a credential. Examples write a token as ``<token>``.
"""


class CodeHostError(Exception):
    """Root of every error a code-host implementation raises."""


class NoChangeRequestError(CodeHostError):
    """No open change request exists for the requested branch."""


class AuthenticationError(CodeHostError):
    """The session is not authenticated, or its credential was rejected.

    The message never contains the token or the token command.
    """


class TokenCommandError(AuthenticationError):
    """The token command failed. The data fields let a consumer build its own text. No message holds argv or stderr.

    ``exit_code`` is the command's exit status, or ``None`` when it did not run to an exit. ``detail`` names the cause
    when it did not run (an exception class name).
    """

    def __init__(self, message: str, *, exit_code: int | None = None, detail: str | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.detail = detail


class CodeHostTimeoutError(CodeHostError):
    """The call timed out; the host may already have processed the request.

    Callers must not blindly retry (for example by a fallback post), because that could duplicate the review.
    ``write=True`` (the default) means the call was a write: no retry and no fallback. ``write=False`` means a
    read, which may be retried.
    """

    def __init__(self, message: str, *, write: bool = True) -> None:
        super().__init__(message)
        self.write = write


class UnsupportedCapabilityError(CodeHostError):
    """The host does not implement a capability interface or operation.

    ``capability`` is the interface name, for example ``"Checks"``.
    """

    def __init__(self, message: str, *, capability: str) -> None:
        super().__init__(message)
        self.capability = capability


class ConfigurationError(CodeHostError):
    """The code-host setup is unusable (missing repository identity, bad option)."""
