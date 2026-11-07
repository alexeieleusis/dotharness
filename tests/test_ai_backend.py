import asyncio
import logging
import os
import re
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from dotharness import ai_backend, opencode_guards, subprocess_runner
from dotharness.ai_backend import (
    ClaudeOptions,
    OpencodeOptions,
    build_claude_command,
    build_opencode_command,
    launch,
    launch_sync,
    select_backend,
)
from dotharness.opencode_guards import BackendCwdDivergedError, OpencodePluginError
from dotharness.repo_guard import RepoIdentityError

SENTINEL = "tok-SENTINEL-0123456789abcdef"


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(subprocess_runner, "KILL_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(ai_backend, "TMP_DIR", tmp_path / "tmpdir")
    monkeypatch.setattr(ai_backend, "GROUP_EXIT_WAIT_SECONDS", 3.0)
    monkeypatch.setattr(ai_backend, "GROUP_EXIT_POLL_SECONDS", 0.05)
    monkeypatch.setattr(opencode_guards, "CWD_MONITOR_POLL_SECONDS", 0.1)


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "bin"
    path.mkdir()
    monkeypatch.setenv("PATH", f"{path}:{os.environ['PATH']}")
    return path


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def make_exe(bin_dir: Path, name: str, body: str) -> Path:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return path


def recorder(bin_dir: Path, name: str, log: Path, extra: str = "") -> Path:
    """A fake executable that appends one argv entry per line to ``log``, then runs ``extra``."""
    return make_exe(bin_dir, name, f'for a in "$@"; do echo "$a" >> {log}; done\n{extra}\n')


def prompt_files() -> list[Path]:
    return list(ai_backend.TMP_DIR.glob("*")) if ai_backend.TMP_DIR.exists() else []


# --- selection and argv -------------------------------------------------------------------------------------------


def test_select_backend() -> None:
    assert select_backend("claude") == "claude"
    assert select_backend("opencode") == "opencode"
    for bad in ("codex", "", "Claude"):
        with pytest.raises(ValueError, match="Unknown backend"):
            select_backend(bad)


async def test_launch_unknown_backend_raises(work: Path) -> None:
    with pytest.raises(ValueError, match="Unknown backend"):
        await launch("gemini", "hi", cwd=work, timeout=5)


async def test_launch_rejects_other_backend_options(work: Path) -> None:
    with pytest.raises(TypeError, match="does not apply to the claude"):
        await launch("claude", "hi", cwd=work, timeout=5, backend_options=OpencodeOptions(standalone=True))
    with pytest.raises(TypeError, match="does not apply to the opencode"):
        await launch("opencode", "hi", cwd=work, timeout=5, backend_options=ClaudeOptions(skip_permissions=True))


def test_argv_pr_review_claude() -> None:
    cmd = build_claude_command("T", skip_permissions=True, disable_slash_commands=True)
    assert cmd == ["claude", "--dangerously-skip-permissions", "--disable-slash-commands", "-p", "T"]


def test_argv_vibe_heal_claude() -> None:
    cmd = build_claude_command(
        "P", print_flag="--print", output_format="json", permission_mode="acceptEdits", allowed_tools="Edit,Read"
    )
    assert cmd == [
        "claude", "--print", "P", "--output-format", "json", "--permission-mode", "acceptEdits",
        "--allowedTools", "Edit,Read",
    ]  # fmt: skip


def test_argv_claude_defaults_carry_no_flags() -> None:
    assert build_claude_command("P") == ["claude", "-p", "P"]
    assert build_claude_command("P", print_mode=False) == ["claude", "P"]


def test_argv_pr_review_opencode() -> None:
    assert build_opencode_command("T", standalone=True, auto=True) == ["opencode", "run", "--standalone", "--auto", "T"]
    cmd = build_opencode_command("T", standalone=True, auto=True, opencode_dir="/d")
    assert cmd == ["opencode", "run", "--standalone", "--auto", "--dir", "/d", "T"]


def test_argv_vibe_heal_opencode() -> None:
    assert build_opencode_command("P") == ["opencode", "run", "P"]
    assert build_opencode_command("P", model="m") == ["opencode", "run", "P", "-m", "m"]


async def test_launch_argv_end_to_end(bin_dir: Path, work: Path, tmp_path: Path) -> None:
    log = tmp_path / "argv.log"
    recorder(bin_dir, "claude", log, "echo out")
    claude_opts = ClaudeOptions(skip_permissions=True, disable_slash_commands=True)
    result = await launch("claude", "do it", cwd=work, timeout=10, backend_options=claude_opts)
    assert result.returncode == 0
    assert result.stdout.strip() == "out"
    lines = log.read_text().splitlines()
    assert lines[:3] == ["--dangerously-skip-permissions", "--disable-slash-commands", "-p"]
    assert re.fullmatch(r"Read \S+ and follow the instructions exactly\.", lines[3])
    assert len(lines) == 4

    log.unlink()
    recorder(bin_dir, "opencode", log)
    opencode_opts = OpencodeOptions(standalone=True, auto=True, opencode_dir="/x", model="m")
    await launch("opencode", "do it", cwd=work, timeout=10, backend_options=opencode_opts)
    lines = log.read_text().splitlines()
    assert lines[:5] == ["run", "--standalone", "--auto", "--dir", "/x"]
    assert lines[5].startswith("Read ")
    assert lines[6:] == ["-m", "m"]


async def test_launch_inline_prompt(bin_dir: Path, work: Path, tmp_path: Path) -> None:
    log = tmp_path / "argv.log"
    recorder(bin_dir, "claude", log)
    await launch("claude", "inline text", cwd=work, timeout=10, prompt_via_file=False)
    assert log.read_text().splitlines() == ["-p", "inline text"]
    assert prompt_files() == []


# --- prompt file lifecycle ---------------------------------------------------------------------------------------


# Reads the prompt path from the last argument, copies its content next to the log, then runs ``extra``.
def _prompt_probe(bin_dir: Path, name: str, seen: Path, extra: str) -> None:
    make_exe(
        bin_dir,
        name,
        f"""
        for a in "$@"; do last="$a"; done
        p=${{last#Read }}; p=${{p% and follow*}}
        cat "$p" > {seen}
        {extra}
        """,
    )


async def test_prompt_file_removed_after_success(bin_dir: Path, work: Path, tmp_path: Path) -> None:
    seen = tmp_path / "seen"
    _prompt_probe(bin_dir, "claude", seen, "")
    result = await launch("claude", "hello prompt", cwd=work, timeout=10)
    assert result.returncode == 0
    assert seen.read_text() == "hello prompt"
    assert prompt_files() == []


async def test_prompt_file_removed_after_failure(bin_dir: Path, work: Path, tmp_path: Path) -> None:
    seen = tmp_path / "seen"
    _prompt_probe(bin_dir, "claude", seen, "echo boom >&2; exit 3")
    result = await launch("claude", "p", cwd=work, timeout=10)
    assert result.returncode == 3
    assert "boom" in result.stderr
    assert seen.read_text() == "p"
    assert prompt_files() == []


async def test_prompt_file_removed_after_timeout(bin_dir: Path, work: Path, tmp_path: Path) -> None:
    seen = tmp_path / "seen"
    _prompt_probe(bin_dir, "claude", seen, "sleep 30")
    result = await launch("claude", "p", cwd=work, timeout=0.7)
    assert result.timed_out
    assert seen.read_text() == "p"
    assert prompt_files() == []


async def test_prompt_file_removed_after_cancellation(bin_dir: Path, work: Path, tmp_path: Path) -> None:
    seen = tmp_path / "seen"
    _prompt_probe(bin_dir, "claude", seen, "sleep 30")
    task = asyncio.create_task(launch("claude", "p", cwd=work, timeout=30))
    for _ in range(100):
        if seen.exists() and seen.read_text():
            break
        await asyncio.sleep(0.05)
    assert len(prompt_files()) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert prompt_files() == []


async def test_missing_executable_is_not_found(bin_dir: Path, work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(bin_dir))
    result = await launch("claude", "p", cwd=work, timeout=5, max_retries=2)
    assert result.not_found
    assert result.returncode == 127
    assert prompt_files() == []


# --- retries -----------------------------------------------------------------------------------------------------


async def test_retries_until_success(bin_dir: Path, work: Path, tmp_path: Path) -> None:
    counter = tmp_path / "count"
    make_exe(
        bin_dir,
        "claude",
        f"""
        echo x >> {counter}
        [ "$(wc -l < {counter})" -ge 3 ] && exit 0
        exit 1
        """,
    )
    result = await launch("claude", "p", cwd=work, timeout=10, max_retries=2, retry_on_nonzero=True)
    assert result.returncode == 0
    assert len(counter.read_text().splitlines()) == 3


async def test_retries_exhausted_returns_last_failure(bin_dir: Path, work: Path, tmp_path: Path) -> None:
    counter = tmp_path / "count"
    make_exe(bin_dir, "claude", f"echo x >> {counter}\nexit 4\n")
    result = await launch("claude", "p", cwd=work, timeout=10, max_retries=1, retry_on_nonzero=True)
    assert result.returncode == 4
    assert len(counter.read_text().splitlines()) == 2
    counter.unlink()
    result = await launch("claude", "p", cwd=work, timeout=10)
    assert len(counter.read_text().splitlines()) == 1


async def test_nonzero_exit_not_retried_by_default(bin_dir: Path, work: Path, tmp_path: Path) -> None:
    counter = tmp_path / "count"
    make_exe(bin_dir, "claude", f"echo x >> {counter}\nexit 1\n")
    result = await launch("claude", "p", cwd=work, timeout=10, max_retries=2)
    assert result.returncode == 1
    assert len(counter.read_text().splitlines()) == 1


async def test_retries_after_timeout(bin_dir: Path, work: Path, tmp_path: Path) -> None:
    counter = tmp_path / "count"
    make_exe(
        bin_dir,
        "claude",
        f"""
        echo x >> {counter}
        [ "$(wc -l < {counter})" -ge 2 ] && exit 0
        sleep 30
        """,
    )
    result = await launch("claude", "p", cwd=work, timeout=0.7, max_retries=1)
    assert result.returncode == 0
    assert not result.timed_out


# --- environment -------------------------------------------------------------------------------------------------


async def test_pwd_equals_cwd_and_env_passed(bin_dir: Path, work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PWD", "/stale/somewhere")
    make_exe(bin_dir, "opencode", 'echo "$PWD|$EXTRA_VAR|$(pwd -P)"\n')
    result = await launch("opencode", "p", cwd=work, timeout=10, env={"EXTRA_VAR": "ev"})
    pwd, extra, real = result.stdout.strip().split("|")
    assert pwd == str(work)
    assert extra == "ev"
    assert real == str(work.resolve())


async def test_path_prepend(bin_dir: Path, work: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    make_exe(other, "claude", "echo from-other\n")
    make_exe(bin_dir, "claude", "echo from-bin\n")
    result = await launch("claude", "p", cwd=work, timeout=10, path_prepend=[str(other)])
    assert result.stdout.strip() == "from-other"


# --- kill, survivors ---------------------------------------------------------------------------------------------


async def test_term_ignoring_backend_killed_with_group(bin_dir: Path, work: Path, tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    make_exe(
        bin_dir,
        "claude",
        f"""
        trap '' TERM
        sleep 60 &
        echo "$$ $!" > {pids}
        wait
        """,
    )
    started = time.monotonic()
    result = await launch("claude", "p", cwd=work, timeout=0.7)
    assert result.timed_out
    assert result.returncode == 124
    assert time.monotonic() - started < 15
    for pid in map(int, pids.read_text().split()):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # gone (reaped), not just signalled


async def test_survive_warning_when_group_cannot_be_killed(
    bin_dir: Path, work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    make_exe(bin_dir, "claude", "exit 0\n")
    monkeypatch.setattr(ai_backend, "_group_alive", lambda _pgid: True)
    monkeypatch.setattr(ai_backend, "GROUP_EXIT_WAIT_SECONDS", 0.2)
    with caplog.at_level(logging.WARNING, logger="dotharness.ai_backend"):
        result = await launch("claude", "p", cwd=work, timeout=10, context="ctx")
    assert result.returncode == 0
    assert any("still has member(s)" in r.getMessage() and r.getMessage().startswith("ctx: ") for r in caplog.records)


async def test_no_survive_warning_on_clean_run(bin_dir: Path, work: Path, caplog: pytest.LogCaptureFixture) -> None:
    make_exe(bin_dir, "claude", "exit 0\n")
    with caplog.at_level(logging.WARNING, logger="dotharness.ai_backend"):
        await launch("claude", "p", cwd=work, timeout=10)
    assert not [r for r in caplog.records if "still has member" in r.getMessage()]


# --- guards ------------------------------------------------------------------------------------------------------


def git(path: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)  # noqa: S607
    return done.stdout.strip()


def make_repo(path: Path, origin: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "remote", "add", "origin", origin)
    git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    return path


async def test_repo_identity_guard(bin_dir: Path, tmp_path: Path) -> None:
    make_exe(bin_dir, "claude", "exit 0\n")
    repo = make_repo(tmp_path / "repo", "https://github.com/acme/widgets.git")
    result = await launch("claude", "p", cwd=repo, timeout=10, expected_repo_name="acme/widgets")
    assert result.returncode == 0
    with pytest.raises(RepoIdentityError, match="does not match"):
        await launch("claude", "p", cwd=repo, timeout=10, expected_repo_name="acme/other")
    sub = repo / "sub"
    sub.mkdir()
    with pytest.raises(RepoIdentityError, match="toplevel"):
        await launch("claude", "p", cwd=sub, timeout=10, expected_repo_name="acme/widgets")
    with pytest.raises(RepoIdentityError, match="not inside a git repository"):
        await launch("claude", "p", cwd=tmp_path / "bin", timeout=10, expected_repo_name="acme/widgets")
    assert prompt_files() == []


async def test_repo_identity_scp_origin(bin_dir: Path, tmp_path: Path) -> None:
    make_exe(bin_dir, "claude", "exit 0\n")
    repo = make_repo(tmp_path / "repo", "git@github.com:acme/widgets.git")
    result = await launch("claude", "p", cwd=repo, timeout=10, expected_repo_name="acme/widgets")
    assert result.returncode == 0


async def test_harness_repo_guard(bin_dir: Path, tmp_path: Path, work: Path) -> None:
    harness = make_repo(tmp_path / "harness", "https://github.com/acme/harness.git")
    make_exe(bin_dir, "claude", f"git -C {harness} -c user.email=t@t -c user.name=t commit -q --allow-empty -m rogue\n")
    with pytest.raises(RepoIdentityError, match="HEAD moved"):
        await launch("claude", "p", cwd=work, timeout=10, harness_repo_root=harness)
    # Not watched when cwd is the harness repo itself.
    result = await launch("claude", "p", cwd=harness, timeout=10, harness_repo_root=harness)
    assert result.returncode == 0
    # Unchanged HEAD passes.
    make_exe(bin_dir, "claude", "exit 0\n")
    result = await launch("claude", "p", cwd=work, timeout=10, harness_repo_root=harness)
    assert result.returncode == 0


async def test_plugin_check(bin_dir: Path, work: Path) -> None:
    make_exe(bin_dir, "opencode", '[ "$1" = plugin ] && { echo "No plugins found"; exit 0; }\necho ran\n')
    result = await launch("opencode", "p", cwd=work, timeout=10, backend_options=OpencodeOptions(check_plugins=True))
    assert result.stdout.strip() == "ran"

    make_exe(bin_dir, "opencode", '[ "$1" = plugin ] && { echo "some-plugin"; exit 0; }\necho ran\n')
    with pytest.raises(OpencodePluginError, match="plugins installed"):
        await launch("opencode", "p", cwd=work, timeout=10, backend_options=OpencodeOptions(check_plugins=True))

    make_exe(bin_dir, "opencode", '[ "$1" = plugin ] && { echo bad >&2; exit 2; }\necho ran\n')
    with pytest.raises(OpencodePluginError, match="could not verify"):
        await launch("opencode", "p", cwd=work, timeout=10, backend_options=OpencodeOptions(check_plugins=True))
    assert prompt_files() == []


async def test_cwd_divergence_monitor(bin_dir: Path, work: Path) -> None:
    make_exe(bin_dir, "lsof", 'echo "p1"\necho "n/somewhere/else"\n')
    make_exe(bin_dir, "opencode", "sleep 30\n")
    started = time.monotonic()
    with pytest.raises(BackendCwdDivergedError, match="diverged"):
        await launch(
            "opencode", "p", cwd=work, timeout=20, backend_options=OpencodeOptions(monitor_cwd=True), context="c"
        )
    assert time.monotonic() - started < 10
    assert prompt_files() == []


async def test_cwd_monitor_quiet_when_cwd_matches(bin_dir: Path, work: Path) -> None:
    make_exe(bin_dir, "lsof", f'echo "p1"\necho "n{work.resolve()}"\n')
    make_exe(bin_dir, "opencode", "sleep 0.6\necho done\n")
    result = await launch("opencode", "p", cwd=work, timeout=20, backend_options=OpencodeOptions(monitor_cwd=True))
    assert result.stdout.strip() == "done"


# --- no argv/env leaks -------------------------------------------------------------------------------------------


async def test_no_argv_or_env_in_logs_results_or_errors(
    bin_dir: Path, work: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    make_exe(bin_dir, "claude", "echo fine\nexit 1\n")
    make_exe(bin_dir, "opencode", "sleep 30\n")
    make_exe(bin_dir, "lsof", 'echo "n/elsewhere"\n')
    repo = make_repo(tmp_path / "repo", "https://github.com/acme/widgets.git")
    texts: list[str] = []
    with caplog.at_level(logging.DEBUG):
        failed = await launch(
            "claude",
            "p",
            cwd=work,
            timeout=10,
            env={"API_TOKEN": SENTINEL},
            backend_options=ClaudeOptions(allowed_tools=SENTINEL),
            max_retries=1,
        )
        texts += [failed.stdout, failed.stderr]
        timed = await launch(
            "opencode",
            "p",
            cwd=work,
            timeout=0.5,
            env={"API_TOKEN": SENTINEL},
            backend_options=OpencodeOptions(model=SENTINEL),
        )
        texts += [timed.stdout, timed.stderr]
        with pytest.raises(BackendCwdDivergedError) as div:
            await launch(
                "opencode",
                "p",
                cwd=work,
                timeout=20,
                env={"API_TOKEN": SENTINEL},
                backend_options=OpencodeOptions(monitor_cwd=True, model=SENTINEL),
            )
        with pytest.raises(RepoIdentityError) as ident:
            await launch(
                "claude", "p", cwd=repo, timeout=20, env={"API_TOKEN": SENTINEL}, expected_repo_name="acme/other"
            )
        texts += [str(div.value), str(ident.value)]
    texts.append(caplog.text)
    for text in texts:
        assert SENTINEL not in text


# --- sync adapter ------------------------------------------------------------------------------------------------


def test_launch_sync(bin_dir: Path, work: Path) -> None:
    make_exe(bin_dir, "claude", "echo sync-ok\n")
    result = launch_sync("claude", "p", cwd=work, timeout=10)
    assert result.stdout.strip() == "sync-ok"
    assert result.returncode == 0
    assert prompt_files() == []


def test_prompt_file_is_private_and_randomly_named():
    path = ai_backend._write_prompt_file("secret prompt")
    try:
        assert (path.stat().st_mode & 0o777) == 0o600
        assert (ai_backend.TMP_DIR.stat().st_mode & 0o777) == 0o700
        assert path.read_text(encoding="utf-8") == "secret prompt"
        assert path.name != ai_backend._write_prompt_file("x").name
    finally:
        for f in ai_backend.TMP_DIR.glob("*"):
            f.unlink()


@pytest.mark.skipif(shutil.which("claude") is None, reason="needs the real claude CLI")
@pytest.mark.skipif(not os.environ.get("DOTHARNESS_LIVE_CLAUDE"), reason="live check: set DOTHARNESS_LIVE_CLAUDE=1")
def test_claude_accept_edits_can_read_prompt_outside_cwd(tmp_path):
    """Fails if `claude --print --permission-mode acceptEdits --allowedTools Edit,Read` cannot read the prompt file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = ai_backend._write_prompt_file("The magic word is PINEAPPLE-7. Reply with only the magic word.")
    try:
        result = subprocess.run(
            [  # noqa: S607
                "claude",
                "--print",
                f"Read {prompt} and follow the instructions exactly.",
                "--output-format",
                "json",
                "--permission-mode",
                "acceptEdits",
                "--allowedTools",
                "Edit,Read",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        prompt.unlink(missing_ok=True)
    assert "PINEAPPLE-7" in result.stdout
