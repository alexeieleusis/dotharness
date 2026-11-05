"""Tests for the synchronous Backend adapter over dotharness.ai_backend.launch_sync."""

import contextlib
import os
import shutil
import signal
import subprocess
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from dotharness import subprocess_runner
from dotharness.ai_backend import (
    ClaudeOptions,
    OpencodeOptions,
    build_claude_command,
    build_opencode_command,
)
from dotharness.pr_review import backend as backend_mod
from dotharness.pr_review.backend import (
    Backend,
    BackendCwdDivergedError,
    OpencodePluginError,
    RepoIdentityError,
)
from dotharness.subprocess_runner import RunResult

LAUNCH = "dotharness.pr_review.backend.launch_sync"


def _make_backend(backend="opencode", **kwargs):
    return Backend(backend=backend, timeout=10, path_prepend=[], env_vars={}, **kwargs)


# --- flag mapping and pass-through --------------------------------------------------------------------------------


def test_opencode_flag_mapping_and_guards_passed(tmp_path):
    b = Backend("opencode", 77, ["/x/bin"], {"A": "1"}, expected_repo_name="acme/web")
    with patch(LAUNCH, return_value=RunResult(0, "o", "e")) as m:
        b.run("do it", str(tmp_path), opencode_dir="/some/dir", context="ctx")
    args, kw = m.call_args
    assert args == ("opencode", "do it")
    assert kw["backend_options"] == OpencodeOptions(
        standalone=True, auto=True, opencode_dir="/some/dir", monitor_cwd=True, check_plugins=True
    )
    assert kw["cwd"] == str(tmp_path)
    assert kw["timeout"] == 77
    assert kw["env"] == {"A": "1"}
    assert kw["path_prepend"] == ["/x/bin"]
    assert kw["context"] == "ctx"
    assert kw["expected_repo_name"] == "acme/web"
    assert kw["guard_harness_repo"] is True


def test_harness_root_is_discovered_in_a_checkout_and_passed_explicitly(tmp_path):
    root = backend_mod.find_harness_checkout()
    assert root is not None  # the test suite runs from the source checkout
    assert (root / "src" / "dotharness" / "pr_review" / "backend.py").is_file()
    with patch(LAUNCH, return_value=RunResult(0, "", "")) as m:
        _make_backend("claude").run("x", str(tmp_path))
    assert m.call_args.kwargs["guard_harness_repo"] is True
    assert m.call_args.kwargs["harness_repo_root"] == root


def test_explicit_harness_root_overrides_discovery(tmp_path):
    with patch(LAUNCH, return_value=RunResult(0, "", "")) as m:
        _make_backend("claude", harness_repo_root=tmp_path).run("x", str(tmp_path))
    assert m.call_args.kwargs["guard_harness_repo"] is True
    assert m.call_args.kwargs["harness_repo_root"] == tmp_path


def test_installed_package_skips_harness_guard_instead_of_pinning_wrong_repo(tmp_path, monkeypatch):
    # Simulate site-packages inside an unrelated git repo: .git exists above, but no src/dotharness/pr_review layout.
    unrelated = tmp_path / "venv-repo"
    pkg = unrelated / "lib" / "site-packages" / "dotharness" / "pr_review"
    pkg.mkdir(parents=True)
    (unrelated / ".git").mkdir()
    monkeypatch.setattr(backend_mod, "__file__", str(pkg / "backend.py"))
    backend_mod.find_harness_checkout.cache_clear()
    try:
        assert backend_mod.find_harness_checkout() is None
        with patch(LAUNCH, return_value=RunResult(0, "", "")) as m:
            _make_backend("claude").run("x", str(tmp_path))
    finally:
        monkeypatch.undo()
        backend_mod.find_harness_checkout.cache_clear()
    assert m.call_args.kwargs["guard_harness_repo"] is False
    assert m.call_args.kwargs["harness_repo_root"] is None


def test_source_layout_checkout_is_found(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    pkg = root / "src" / "dotharness" / "pr_review"
    pkg.mkdir(parents=True)
    (root / ".git").write_text("gitdir: elsewhere")  # a worktree has a .git file
    monkeypatch.setattr(backend_mod, "__file__", str(pkg / "backend.py"))
    backend_mod.find_harness_checkout.cache_clear()
    try:
        assert backend_mod.find_harness_checkout() == root.resolve()
    finally:
        monkeypatch.undo()
        backend_mod.find_harness_checkout.cache_clear()


def test_claude_flag_mapping(tmp_path):
    with patch(LAUNCH, return_value=RunResult(0, "", "")) as m:
        _make_backend("claude").run("do it", str(tmp_path))
    assert m.call_args.kwargs["backend_options"] == ClaudeOptions(skip_permissions=True, disable_slash_commands=True)


def test_max_retries_passed_explicitly_default_one(tmp_path):
    with patch(LAUNCH, return_value=RunResult(0, "", "")) as m:
        _make_backend().run("x", str(tmp_path))
        assert m.call_args.kwargs["max_retries"] == 1
        _make_backend(max_retries=0).run("x", str(tmp_path))
        assert m.call_args.kwargs["max_retries"] == 0
        _make_backend(max_retries=3).run("x", str(tmp_path))
        assert m.call_args.kwargs["max_retries"] == 3


def test_argv_pinned_through_shared_builders():
    """The flags the adapter selects produce today's pr-review argv."""
    assert build_claude_command("P", skip_permissions=True, disable_slash_commands=True) == [
        "claude",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "-p",
        "P",
    ]
    assert build_opencode_command("P", standalone=True, auto=True) == ["opencode", "run", "--standalone", "--auto", "P"]
    assert build_opencode_command("P", standalone=True, auto=True, opencode_dir="/d") == [
        "opencode",
        "run",
        "--standalone",
        "--auto",
        "--dir",
        "/d",
        "P",
    ]


def test_invalid_backend_raises():
    with pytest.raises(ValueError, match="Unknown backend"):
        Backend("codex", 10, [], {})


# --- result and exception mapping ---------------------------------------------------------------------------------


def test_success_returns_completed_process_with_bytes(tmp_path):
    with patch(LAUNCH, return_value=RunResult(0, "out é", "err")):
        result = _make_backend().run("x", str(tmp_path))
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert result.stdout == "out é".encode()
    assert result.stderr == b"err"


def test_nonzero_exit_is_returned_not_raised(tmp_path):
    with patch(LAUNCH, return_value=RunResult(3, "o", "e")):
        result = _make_backend().run("x", str(tmp_path))
    assert result.returncode == 3
    assert result.stdout == b"o"


def test_not_found_is_returned_with_127(tmp_path):
    with patch(LAUNCH, return_value=RunResult(127, "", "not found", not_found=True)):
        result = _make_backend().run("x", str(tmp_path))
    assert result.returncode == 127


def test_timeout_raises_timeout_expired(tmp_path):
    with (
        patch(LAUNCH, return_value=RunResult(124, "partial", "", timed_out=True)),
        pytest.raises(subprocess.TimeoutExpired) as exc,
    ):
        _make_backend().run("x", str(tmp_path))
    assert exc.value.timeout == 10
    assert exc.value.output == b"partial"


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("v"),
        TypeError("t"),
        OpencodePluginError("p"),
        BackendCwdDivergedError("d"),
        RepoIdentityError("r"),
    ],
)
def test_shared_layer_errors_propagate_unchanged(tmp_path, exc):
    with patch(LAUNCH, side_effect=exc), pytest.raises(type(exc)) as got:
        _make_backend().run("x", str(tmp_path))
    assert got.value is exc


def test_no_argv_env_or_output_logged(tmp_path, caplog):
    secret = "SENTINEL-secret-value"  # noqa: S105 - fake sentinel
    b = Backend("claude", 10, [], {"TOKEN": secret})
    with patch(LAUNCH, return_value=RunResult(1, secret, secret)), caplog.at_level("DEBUG"):
        b.run(f"prompt {secret}", str(tmp_path))
    assert secret not in caplog.text


def test_removed_internals_are_gone():
    for name in ("_cmd_for", "_build_command", "_execute", "_build_env"):
        assert not hasattr(Backend, name)
    assert not hasattr(backend_mod, "_OPENCODE_GLOBAL_LOCK_KEY")
    assert not hasattr(backend_mod, "acquire_lock")


# --- end to end against a fake executable (real launch_sync) ------------------------------------------------------


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")
    monkeypatch.setattr(subprocess_runner, "KILL_GRACE_SECONDS", 1.0)
    return d


def _exe(d: Path, name: str, body: str) -> None:
    p = d / name
    p.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    p.chmod(0o755)


def test_e2e_claude_runs_shared_layer_and_removes_prompt_file(tmp_path, fake_bin, tmp_xdg):
    log = tmp_path / "argv.log"
    _exe(fake_bin, "claude", f'for a in "$@"; do echo "$a" >> {log}; done\necho hello\nexit 0\n')
    work = tmp_path / "work"
    work.mkdir()
    result = _make_backend("claude", max_retries=0).run("instructions text", str(work))
    assert result.returncode == 0
    assert result.stdout.strip() == b"hello"
    argv = log.read_text().splitlines()
    assert argv[:3] == ["--dangerously-skip-permissions", "--disable-slash-commands", "-p"]
    assert argv[3].startswith("Read ") and "dotharness_" in argv[3]
    assert list((tmp_xdg / "tmp").glob("*")) == []


def test_e2e_failing_backend_launched_once_with_default_retries(tmp_path, fake_bin, tmp_xdg):
    # Runs are not idempotent (replies, pushes): a non-zero exit must not be retried, only timeouts are.
    count = tmp_path / "count"
    _exe(fake_bin, "claude", f"echo x >> {count}\nexit 1\n")
    work = tmp_path / "work"
    work.mkdir()
    result = _make_backend("claude").run("x", str(work))
    assert result.returncode == 1
    assert len(count.read_text().splitlines()) == 1


def test_retry_on_nonzero_not_enabled_by_adapter(tmp_path):
    with patch(LAUNCH, return_value=RunResult(0, "", "")) as m:
        _make_backend().run("x", str(tmp_path))
    assert not m.call_args.kwargs.get("retry_on_nonzero", False)


def test_e2e_missing_executable_returns_127(tmp_path, monkeypatch, tmp_xdg):
    # PATH holds only a symlink to git (the harness-HEAD guard reads the checkout's HEAD). Reusing git's own directory
    # could also expose a real `claude` (e.g. /opt/homebrew/bin), making the test non-hermetic.
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git") or "/usr/bin/git")
    monkeypatch.setenv("PATH", str(only_git))
    work = tmp_path / "work"
    work.mkdir()
    result = _make_backend("claude", max_retries=0).run("x", str(work))
    assert result.returncode == 127


def test_e2e_timeout_sigterm_then_sigkill_group_and_no_global_lock(tmp_path, fake_bin, tmp_xdg):
    marker = tmp_path / "term"
    pidfile = tmp_path / "pid"
    # Ignores SIGTERM so the group must be SIGKILLed after the grace period.
    _exe(
        fake_bin,
        "claude",
        f'echo $$ > {pidfile}\ntrap "echo t >> {marker}" TERM\nwhile true; do sleep 0.1; done\n',
    )
    work = tmp_path / "work"
    work.mkdir()
    b = Backend("claude", 1, [], {}, max_retries=0)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            b.run("x", str(work))
        assert marker.exists()  # SIGTERM delivered first
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        with pytest.raises(ProcessLookupError):  # SIGKILL escalation took the process down
            os.kill(pid, 0)
    finally:
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.killpg(int(pidfile.read_text()), signal.SIGKILL)
    assert not list(tmp_xdg.glob("locks/opencode-global*"))
