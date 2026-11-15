import asyncio
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from dotharness import subprocess_runner
from dotharness.subprocess_runner import RunResult, run

MARKER = "dotharness-fake-child-marker"

# Parent spawns a grandchild, both ignore SIGTERM, parent prints both pids then sleeps.
FAKE_CHILD = textwrap.dedent(
    """
    import os, signal, subprocess, sys, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    gc = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)  # {marker}"]
    )
    open(sys.argv[1], "w").write(f"{os.getpid()} {gc.pid}")
    time.sleep(60)
    """
).replace("{marker}", MARKER)


@pytest.fixture(autouse=True)
def short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess_runner, "KILL_GRACE_SECONDS", 0.3)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _assert_dead(*pids: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"processes still alive: {[p for p in pids if _alive(p)]}")


def _write_child(tmp_path: Path, pid_file: Path) -> list[str]:
    script = tmp_path / "child.py"
    script.write_text(FAKE_CHILD)
    return [sys.executable, str(script), str(pid_file)]


async def _read_pids(pid_file: Path) -> tuple[int, int]:
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().count(" ") == 1:
            parent, grandchild = pid_file.read_text().split()
            return int(parent), int(grandchild)
        await asyncio.sleep(0.05)
    pytest.fail("fake child did not report pids")


async def test_success() -> None:
    result = await run([sys.executable, "-c", "print('x')"], timeout=10)
    assert result == RunResult(returncode=0, stdout="x\n", stderr="", timed_out=False, not_found=False)


async def test_nonzero_exit_captures_stderr() -> None:
    code = "import sys; sys.stderr.write('boom'); sys.exit(3)"
    result = await run([sys.executable, "-c", code], timeout=10)
    assert result.returncode == 3
    assert result.stderr == "boom"
    assert not result.timed_out
    assert not result.not_found


async def test_stdin_cwd_env(tmp_path: Path) -> None:
    code = "import os, sys; print(sys.stdin.read() + os.getcwd() + os.environ['FOO'])"
    result = await run(
        [sys.executable, "-c", code], cwd=tmp_path, env={**os.environ, "FOO": "bar"}, timeout=10, stdin="in"
    )
    assert result.stdout.strip() == f"in{tmp_path.resolve()}bar"


async def test_missing_executable() -> None:
    result = await run(["definitely-not-a-real-binary-xyz"], timeout=10)
    assert result.not_found
    assert result.returncode == 127
    assert not result.timed_out


async def test_timeout_kills_child() -> None:
    start = time.monotonic()
    result = await run([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5)
    assert time.monotonic() - start < 5
    assert result.timed_out
    assert not result.not_found
    assert result.returncode == 124
    assert "timed out after 0.5s" in result.stderr


async def test_timeout_group_kill_with_grandchild(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids"
    cmd = _write_child(tmp_path, pid_file)
    task = asyncio.ensure_future(run(cmd, timeout=1.5))
    parent, grandchild = await _read_pids(pid_file)
    result = await task
    assert result.timed_out
    assert result.returncode == 124
    await _assert_dead(parent, grandchild)


async def test_cancellation_kills_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids"
    cmd = _write_child(tmp_path, pid_file)
    task = asyncio.ensure_future(run(cmd, timeout=60))
    parent, grandchild = await _read_pids(pid_file)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _assert_dead(parent, grandchild)


async def test_normal_exit_sweeps_leftover_grandchild(tmp_path: Path) -> None:
    pid_file = tmp_path / "gc"
    code = (
        "import subprocess, sys;"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)  # {MARKER}'],"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))"
    )
    result = await run([sys.executable, "-c", code], timeout=10)
    assert result.returncode == 0
    await _assert_dead(int(pid_file.read_text()))


async def test_on_start_exception_kills_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess_runner, "KILL_GRACE_SECONDS", 0.2)
    pids: list[int] = []

    def boom(pid: int) -> None:
        pids.append(pid)
        raise RuntimeError("hook failed")  # noqa: TRY003

    with pytest.raises(RuntimeError, match="hook failed"):
        await run(["sleep", "60"], timeout=5, on_start=boom)

    with pytest.raises(ProcessLookupError):
        os.killpg(pids[0], 0)


async def test_background_grandchild_holding_pipes_does_not_cause_timeout() -> None:
    start = time.monotonic()
    result = await run(["sh", "-c", "sleep 300 & echo done"], timeout=10)
    assert time.monotonic() - start < 5
    assert result.returncode == 0
    assert not result.timed_out
    assert result.stdout == "done\n"


def test_killpg_suppresses_only_process_lookup_error(monkeypatch):
    from dotharness import subprocess_runner as sr

    def gone(_pgid, _sig):
        raise ProcessLookupError

    def denied(_pgid, _sig):
        raise PermissionError

    monkeypatch.setattr(sr.os, "killpg", gone)
    sr.killpg(1, sr.signal.SIGKILL)
    monkeypatch.setattr(sr.os, "killpg", denied)
    with pytest.raises(PermissionError):
        sr.killpg(1, sr.signal.SIGKILL)


@pytest.mark.parametrize(("exc", "alive"), [(None, True), (ProcessLookupError, False), (PermissionError, True)])
def test_group_alive_outcomes(monkeypatch, exc, alive):
    from dotharness import subprocess_runner as sr

    def probe(_pgid, sig):
        assert sig == 0
        if exc:
            raise exc

    monkeypatch.setattr(sr.os, "killpg", probe)
    assert sr.group_alive(1) is alive
