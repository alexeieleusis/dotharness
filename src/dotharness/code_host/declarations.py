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
    max_change_request_body_chars: int
    timeouts: OperationTimeouts
    list_bounds: ListBounds


@dataclass(frozen=True)
class HostDeclaration:
    capabilities: frozenset[Capability]
    limits: HostLimits
    anchors: AnchorRules
