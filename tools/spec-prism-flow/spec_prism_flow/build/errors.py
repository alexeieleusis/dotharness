from __future__ import annotations

import subprocess
from pathlib import Path
from typing import ClassVar


class CommandError(RuntimeError):
    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.cmd_args = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"`{' '.join(args)}` exited {returncode}: {stderr.strip()}")


def run_subprocess(
    cmd: list[str],
    *,
    cwd: Path | None,
    timeout: int,
    error_cls: type[CommandError],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Shared `subprocess.run` primitive for every `*_ops`/`*_integration` module that
    shells out and needs `TimeoutExpired` (and, when `check`, a non-zero exit) turned
    into a `CommandError` subclass. `check=False` is for a caller like
    `gh_ops._run_raw` that must still convert a timeout but wants to inspect a
    non-zero-exit result itself before deciding how to raise (e.g. `gh_ops.pr_merge`
    wrapping it into `PRNotMergeableError`)."""
    try:
        result = subprocess.run(  # noqa: S603
            cmd, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise error_cls(cmd, -1, f"timed out after {timeout}s: {exc.stderr or ''}") from exc
    if check and result.returncode != 0:
        raise error_cls(cmd, result.returncode, result.stderr)
    return result


class OrchestrationError(Exception):
    exit_code: ClassVar[int] = 1

    def __init__(self, message: str, *, next_command: str | None = None) -> None:
        super().__init__(message)
        self.next_command = next_command
        self.phase_number: int | None = None
        self.phase_name: str | None = None

    def with_context(self, *, phase_number: int | None = None, phase_name: str | None = None) -> OrchestrationError:
        self.phase_number = phase_number
        self.phase_name = phase_name
        return self


class EmptyImplementationError(OrchestrationError):
    exit_code: ClassVar[int] = 10

    def __init__(self) -> None:
        super().__init__("No implementation was produced for this phase.")


class ScopeViolation(OrchestrationError):
    exit_code: ClassVar[int] = 11

    def __init__(self, offending_files: list[str], allowed_globs: list[str], base_ref: str) -> None:
        self.offending_files = offending_files
        self.allowed_globs = allowed_globs
        self.base_ref = base_ref
        super().__init__(
            f"Changed files outside declared scope: {', '.join(offending_files)}",
            next_command=f"git diff --name-only {base_ref}...HEAD",
        )


class MergeGateFailure(OrchestrationError):
    exit_code: ClassVar[int] = 12

    def __init__(self, gate_name: str, output_tail: str) -> None:
        self.gate_name = gate_name
        self.output_tail = output_tail
        super().__init__(
            f"Merge gate '{gate_name}' failed",
            next_command=f"re-run '{gate_name}' in the clone to reproduce",
        )


class RetryBudgetExhausted(OrchestrationError):
    exit_code: ClassVar[int] = 13

    def __init__(self, cycles: int, pr_url: str | None, unresolved_thread_count: int) -> None:
        self.cycles = cycles
        self.pr_url = pr_url
        self.unresolved_thread_count = unresolved_thread_count
        super().__init__(f"Retry budget exhausted after {cycles} cycles")


class ManualTestFailed(OrchestrationError):
    exit_code: ClassVar[int] = 14

    def __init__(self, notes: str | None = None) -> None:
        self.notes = notes
        message = "Manual test failed" if notes is None else f"Manual test failed: {notes}"
        super().__init__(message)
