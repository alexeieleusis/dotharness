"""Code-enforced safety rails: push guard, limit tracker, retry bound, result types.

Pure and injectable; no network, no subprocess. Never approves or merges. Does not
import or reimplement build/scope_guard.py or build/merge_gates.py (result types only).
"""

# ruff: noqa: TRY003
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum

from sisyphus.config import LimitsConfig

# FR-R5: accepted (D-11). The boundary is the dedicated clone and the push guard below.
PERMISSION_FLAG = "--dangerously-skip-permissions"
DEFAULT_MAX_RETRY_CYCLES = 3


class Checkpoint(Enum):
    """Checkpoints C1..C8 (brief section on FR-3)."""

    C1 = "C1"  # brief has open decisions blocking the requirements doc
    C2 = "C2"  # OPEN_QUESTIONS.md unresolved after draft-overview
    C3 = "C3"  # a leaf reports an open question
    C4 = "C4"  # leaf deviation changes a later leaf / sub-agent cannot tell
    C5 = "C5"  # leaf exhausts max_retry_cycles or violates scope
    C6 = "C6"  # a hard FR-7 limit is hit
    C7 = "C7"  # branch cannot be updated without rebase or force-push (D-16)
    C8 = "C8"  # unrecoverable GitHub/Linear call failure


# ---------------------------------------------------------------- push guard


@dataclass(frozen=True)
class PushRequest:
    """A proposed push. ``refspec`` is ``branch``, ``src:dst`` or ``+src:dst``."""

    remote: str
    refspec: str
    flags: tuple[str, ...] = ()
    history_rewrite: bool = False
    needs_rebase: bool = False


@dataclass(frozen=True)
class PushDecision:
    allowed: bool
    reason: str
    checkpoint: Checkpoint | None = None


_FORCE_LONG = ("--force", "--force-with-lease", "--force-if-includes", "--mirror")
_REWRITE_LONG = ("--delete", "--prune")


def parse_push_argv(argv: Sequence[str], *, history_rewrite: bool = False, needs_rebase: bool = False) -> PushRequest:
    """Parse ``[git] push [flags] <remote> <refspec>`` into a :class:`PushRequest`.

    History-rewrite and rebase-needed are facts the caller knows from git state; pass them in.
    Raises ``ValueError`` unless exactly one remote and one refspec are present.
    """
    args = list(argv)
    if args and args[0] == "git":
        args = args[1:]
    if args and args[0] == "push":
        args = args[1:]
    flags = [a for a in args if a.startswith("-")]
    positional = [a for a in args if not a.startswith("-")]
    if len(positional) != 2:
        raise ValueError(f"expected '<remote> <refspec>', got positional args {positional!r}")
    return PushRequest(positional[0], positional[1], tuple(flags), history_rewrite, needs_rebase)


def _is_force_flag(flag: str) -> bool:
    if flag.startswith("--"):
        return any(flag == f or flag.startswith(f + "=") for f in _FORCE_LONG)
    return "f" in flag[1:]


def _is_rewrite_flag(flag: str) -> bool:
    if flag.startswith("--"):
        return flag in _REWRITE_LONG
    return "d" in flag[1:]


def target_branch(refspec: str) -> str:
    """Destination branch of a refspec, stripped of ``+`` and ``refs/heads/``."""
    dst = refspec.lstrip("+").split(":", 1)[-1]
    return dst.removeprefix("refs/heads/")


def push_guard(request: PushRequest, default_branch: str, created_branches: Iterable[str]) -> PushDecision:
    """Decide whether a push is allowed (D-16, NG4)."""
    created = set(created_branches)
    if request.refspec.startswith("+") or any(_is_force_flag(f) for f in request.flags):
        return PushDecision(False, "force-push is forbidden")
    if request.history_rewrite or any(_is_rewrite_flag(f) for f in request.flags) or request.refspec.startswith(":"):
        return PushDecision(False, "history rewrite or ref deletion is forbidden")
    branch = target_branch(request.refspec)
    if branch == default_branch:
        return PushDecision(False, f"push to the default branch '{default_branch}' is forbidden")
    if branch not in created:
        return PushDecision(False, f"branch '{branch}' was not created by Sisyphus")
    if request.needs_rebase:
        return PushDecision(False, f"branch '{branch}' needs a rebase or force-push", Checkpoint.C7)
    return PushDecision(True, "ok")


# ------------------------------------------------------------- limit tracker


@dataclass(frozen=True)
class LimitStatus:
    reached: tuple[str, ...]
    checkpoint: Checkpoint | None

    @property
    def tripped(self) -> bool:
        return bool(self.reached)


class LimitTracker:
    """Counts run activity against [limits]; at count == limit the limit is reached (C6)."""

    def __init__(self, limits: LimitsConfig | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits if limits is not None else LimitsConfig()
        self._clock = clock
        self._start = clock()
        self.tickets = 0
        self.leaves = 0
        self.fix_attempts: dict[str, int] = {}

    def record_ticket(self) -> None:
        self.tickets += 1

    def record_leaf(self) -> None:
        self.leaves += 1

    def record_fix_attempt(self, pr: str) -> int:
        self.fix_attempts[pr] = self.fix_attempts.get(pr, 0) + 1
        return self.fix_attempts[pr]

    def elapsed_minutes(self) -> float:
        return (self._clock() - self._start) / 60

    def status(self) -> LimitStatus:
        lim = self.limits
        reached: list[str] = []
        if self.elapsed_minutes() >= lim.max_run_minutes:
            reached.append("max_run_minutes")
        if self.tickets >= lim.max_tickets_per_run:
            reached.append("max_tickets_per_run")
        if self.leaves >= lim.max_leaves_per_run:
            reached.append("max_leaves_per_run")
        if any(n >= lim.max_fix_attempts_per_pr for n in self.fix_attempts.values()):
            reached.append("max_fix_attempts_per_pr")
        return LimitStatus(tuple(reached), Checkpoint.C6 if reached else None)


# ------------------------------------------------------------ bounded retries


@dataclass(frozen=True)
class RetryResult:
    attempts: int
    escalate: bool
    checkpoint: Checkpoint | None


class RetryBound:
    """Bounded retry cycles; after ``max_retry_cycles`` failures, escalate (C5)."""

    def __init__(self, max_retry_cycles: int = DEFAULT_MAX_RETRY_CYCLES, attempts: int = 0) -> None:
        self.max_retry_cycles = max_retry_cycles
        self.attempts = attempts

    def result(self) -> RetryResult:
        esc = self.attempts >= self.max_retry_cycles
        return RetryResult(self.attempts, esc, Checkpoint.C5 if esc else None)

    def record_failure(self) -> RetryResult:
        self.attempts += 1
        return self.result()


# ------------------------------------------------------------ result types


@dataclass(frozen=True)
class ScopeViolationResult:
    """Report of a scope-guard violation (produced by the caller, not computed here)."""

    files: tuple[str, ...] = ()
    detail: str = ""
    checkpoint: Checkpoint = field(default=Checkpoint.C5, init=False)


@dataclass(frozen=True)
class MergeGateFailureResult:
    """Report of a failed merge gate (produced by the caller, not computed here)."""

    gate: str
    detail: str = ""
    checkpoint: Checkpoint = field(default=Checkpoint.C5, init=False)
