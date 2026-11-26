"""Host declarations: capabilities, limits and anchor rules (shapes only; a host sets the values)."""

from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    """Interface names, verbatim."""

    SESSION = "Session"
    REPOSITORY = "Repository"
    CHANGE_REQUESTS = "ChangeRequests"
    DISCUSSION = "Discussion"
    CHECKS = "Checks"
    WORK_ITEMS = "WorkItems"


REMOVE_LABEL = "WorkItems.remove_label"  # ``Interface.method`` key of ``WorkItems.remove_label``


@dataclass(frozen=True)
class Anchor:
    """File and line position of an inline comment."""

    path: str
    line: int
    side: str  # one of the host's declared `sides`


@dataclass(frozen=True)
class AnchorRules:
    inline_supported: bool
    sides: frozenset[str]
    requires_line_in_diff: bool
    supports_ranges: bool
    fallback: str = "top_level_comment"


@dataclass(frozen=True)
class OperationTimeouts:
    """Keys of ``per_operation`` are written ``Interface.method``."""

    default_seconds: float
    per_operation: dict[str, float]


@dataclass(frozen=True)
class ListBounds:
    """Keys of ``per_operation`` are written ``Interface.method``. ``None`` means unbounded."""

    default: int
    per_operation: dict[str, int | None]


@dataclass(frozen=True)
class HostLimits:
    max_inline_comments_per_review: int
    max_review_body_chars: int
    # Unicode code points (Python `len(str)`). A host that counts UTF-16 units or bytes declares a limit safe under its own rule.
    max_change_request_body_chars: int
    timeouts: OperationTimeouts
    list_bounds: ListBounds
    page_attempts: int = 3  # tries per page of a paged read, first try included
    page_retry_delay_seconds: float = 2.0  # wait after try N is this value times N


@dataclass(frozen=True)
class HostDeclaration:
    capabilities: frozenset[Capability]
    limits: HostLimits
    anchors: AnchorRules
    # Operations ("Interface.method") the host declares but does not implement.
    unsupported_operations: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        capabilities = {c.value for c in Capability}
        for operation in self.unsupported_operations:
            interface, dot, method = operation.partition(".")
            if interface not in capabilities or not dot or not method:
                msg = f"unsupported_operations entry {operation!r} is not an 'Interface.method' key of a Capability"
                raise ValueError(msg)

    def supports_operation(self, operation: str) -> bool:
        """Whether the host supports ``operation`` (an ``Interface.method`` key); reads the declaration only."""
        return operation not in self.unsupported_operations


def supports_operation(declaration: HostDeclaration, operation: str) -> bool:
    """Function form of :meth:`HostDeclaration.supports_operation`."""
    return declaration.supports_operation(operation)
