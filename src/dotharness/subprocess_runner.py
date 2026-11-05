"""Shared async subprocess runner with process-group kill.

POSIX only (macOS, Linux): the child is started in its own session
(``start_new_session=True``) so that it leads a process group, and the whole
group (grandchildren included) is signalled on timeout and on cancellation.

Result convention (see ``RunResult``):

* Normal exit, zero or not: ``returncode`` is the child's code (negative when
  killed by a signal) and both flags are false.
* Executable missing or not launchable (``FileNotFoundError``, any other
  ``OSError``): ``not_found=True``, ``returncode=127``, ``stderr`` holds the
  error text.
* Timeout: ``timed_out=True``, ``returncode=124`` and
  ``timed out after {timeout}s`` is appended to ``stderr``.

``not_found`` and ``timed_out`` are mutually exclusive; callers must tell them
apart from a plain non-zero exit by the flags, not only by the code.

Kill policy (timeout and task cancellation): ``SIGTERM`` to the group, wait up
to ``KILL_GRACE_SECONDS`` (default 5 s) for the child to exit, then ``SIGKILL``
to the group and reap the child. After ``run`` returns or raises, no member of
the group is left (a ``SIGKILL`` sweep also runs after a normal exit).

Consequences of the post-exit sweep: background processes the child started on
purpose are killed too, so callers cannot rely on a daemon outliving the call.
The sweep runs right after asyncio reaps the leader (asyncio reaps it itself, so
it cannot run earlier). While any member survives, the kernel keeps the pgid
reserved; if none survives, the sweep targets a number that could in theory be
recycled, which is only possible if the pid space wraps within that instant.

Neither the runner nor the result logs or carries argv or env.
"""

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

KILL_GRACE_SECONDS = 5.0
DRAIN_SECONDS = 2.0  # bounded wait for pipe readers after the child exited
NOT_FOUND_RETURNCODE = 127
TIMEOUT_RETURNCODE = 124


@dataclass(frozen=True)
class RunResult:
    """Outcome of ``run``. Carries no argv or env."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    not_found: bool = False


def _killpg(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


async def _kill_group(proc: asyncio.subprocess.Process, pgid: int) -> None:
    """SIGTERM the group, wait the grace period, SIGKILL the group, reap the child."""
    _killpg(pgid, signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), KILL_GRACE_SECONDS)
    _killpg(pgid, signal.SIGKILL)
    await proc.wait()


async def _cleanup_shielded(proc: asyncio.subprocess.Process, pgid: int) -> None:
    """Run ``_kill_group`` to completion even if this task is cancelled again."""
    task = asyncio.ensure_future(_kill_group(proc, pgid))
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)
    task.result()


def _text(data: bytes) -> str:
    return data.decode(errors="replace")


async def _drain(comm: "asyncio.Future[tuple[bytes, bytes]]", wait: float) -> tuple[bytes, bytes]:
    """Return the communicate() output if it finishes within ``wait`` seconds, else empty output."""
    done, _ = await asyncio.wait({comm}, timeout=wait)
    if comm in done and not comm.cancelled() and comm.exception() is None:
        return comm.result()
    return b"", b""


async def _cancel_pending(*futs: "asyncio.Future[object]") -> None:
    for fut in futs:
        if not fut.done():
            fut.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await fut


async def run(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float,
    stdin: str | None = None,
    on_start: Callable[[int], None] | None = None,
) -> RunResult:
    """Run ``cmd`` and return its ``RunResult``; see the module docstring for the convention.

    ``on_start``, when given, is called with the child's pid (also its group id) right after it spawned.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return RunResult(returncode=NOT_FOUND_RETURNCODE, stdout="", stderr=str(exc), not_found=True)

    pgid = proc.pid  # session leader, so its pid is the group id
    if on_start is not None:
        try:
            on_start(pgid)
        except BaseException:
            await _cleanup_shielded(proc, pgid)
            raise
    comm = asyncio.ensure_future(proc.communicate(stdin.encode() if stdin is not None else None))
    waiter = asyncio.ensure_future(proc.wait())
    try:
        # Wait on the child's exit, not on pipe EOF: a grandchild may keep the pipes open.
        done, _ = await asyncio.wait({waiter}, timeout=timeout)
        if waiter in done:
            _killpg(pgid, signal.SIGKILL)  # sweep any grandchildren left behind; closes their pipe ends
            out, err = await _drain(comm, DRAIN_SECONDS)
            return RunResult(returncode=proc.returncode or 0, stdout=_text(out), stderr=_text(err))

        await _kill_group(proc, pgid)
        out, err = await _drain(comm, KILL_GRACE_SECONDS)
        message = f"timed out after {timeout}s"
        stderr = f"{_text(err)}\n{message}" if err else message
        return RunResult(returncode=TIMEOUT_RETURNCODE, stdout=_text(out), stderr=stderr, timed_out=True)
    except asyncio.CancelledError:
        await _cleanup_shielded(proc, pgid)
        raise
    finally:
        await _cancel_pending(comm, waiter)
