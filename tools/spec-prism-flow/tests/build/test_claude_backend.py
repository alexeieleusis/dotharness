import subprocess
from unittest.mock import Mock

import pytest

from spec_prism_flow.build.claude_backend import ClaudeBackend, ClaudeCommandError, RepoIdentityError


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)  # noqa: S603, S607


def _init_repo(tmp_path, *, origin: str = "git@github.com:acme/widget.git"):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "remote", "add", "origin", origin)
    return repo


class _FakeProc:
    """Stands in for one subprocess.Popen instance (i.e. one attempt): communicate()
    replays `outcome` (either an (stdout, stderr, returncode) tuple or an exception
    to raise) on its first call. _kill() calls communicate() a second time to reap
    the process after a timeout-kill, mirroring the real Popen contract -- that
    second call just returns empty output rather than re-raising/re-popping."""

    def __init__(self, outcome, *, pid: int = 4242):
        self._outcome = outcome
        self._communicated = False
        self.pid = pid
        self.returncode = 0

    def communicate(self, timeout=None):
        if self._communicated:
            return b"", b""
        self._communicated = True
        if isinstance(self._outcome, Exception):
            raise self._outcome
        stdout, stderr, returncode = self._outcome
        self.returncode = returncode
        return stdout, stderr


def test_build_command_includes_expected_flags_and_prompt(tmp_path):
    cmd = ClaudeBackend._build_command(tmp_path / "instructions.md")

    assert cmd == [
        "claude",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "-p",
        f"Read {tmp_path / 'instructions.md'} and follow the instructions exactly.",
    ]


def test_invoke_writes_instructions_runs_backend_and_returns_stdout(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    written = {}

    def fake_start_process(cmd, cwd):
        instructions_path = cmd[-1].removeprefix("Read ").split(" and follow", 1)[0]
        written["instructions"] = open(instructions_path, encoding="utf-8").read()  # noqa: SIM115
        written["cwd"] = cwd
        return _FakeProc((b"agent output\n", b"", 0))

    monkeypatch.setattr(ClaudeBackend, "_start_process", staticmethod(fake_start_process))

    backend = ClaudeBackend()
    result = backend.invoke("do the thing", repo)

    assert result == "agent output\n"
    assert written["instructions"] == "do the thing"
    assert written["cwd"] == repo


def test_invoke_raises_on_nonzero_exit(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(ClaudeBackend, "_start_process", staticmethod(lambda cmd, cwd: _FakeProc((b"", b"boom", 1))))

    with pytest.raises(ClaudeCommandError) as exc_info:
        ClaudeBackend().invoke("do the thing", repo)

    assert exc_info.value.returncode == 1
    assert exc_info.value.stderr == "boom"


def test_invoke_raises_repo_identity_error_when_cwd_is_not_a_git_repo(tmp_path, monkeypatch):
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    start_process_mock = Mock()
    monkeypatch.setattr(ClaudeBackend, "_start_process", staticmethod(start_process_mock))

    with pytest.raises(RepoIdentityError):
        ClaudeBackend().invoke("do the thing", not_a_repo)

    start_process_mock.assert_not_called()


def test_invoke_raises_repo_identity_error_when_cwd_is_a_subdirectory_of_a_repo(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    subdir = repo / "sub"
    subdir.mkdir()
    start_process_mock = Mock()
    monkeypatch.setattr(ClaudeBackend, "_start_process", staticmethod(start_process_mock))

    with pytest.raises(RepoIdentityError):
        ClaudeBackend().invoke("do the thing", subdir)

    start_process_mock.assert_not_called()


def test_invoke_raises_repo_identity_error_when_origin_missing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    start_process_mock = Mock()
    monkeypatch.setattr(ClaudeBackend, "_start_process", staticmethod(start_process_mock))

    with pytest.raises(RepoIdentityError):
        ClaudeBackend().invoke("do the thing", repo)

    start_process_mock.assert_not_called()


def test_invoke_raises_repo_identity_error_when_expected_repo_name_mismatches(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, origin="git@github.com:acme/widget.git")
    start_process_mock = Mock()
    monkeypatch.setattr(ClaudeBackend, "_start_process", staticmethod(start_process_mock))

    with pytest.raises(RepoIdentityError):
        ClaudeBackend(expected_repo_name="other/repo").invoke("do the thing", repo)

    start_process_mock.assert_not_called()


def test_invoke_checks_repo_identity_again_after_a_successful_run(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(ClaudeBackend, "_start_process", staticmethod(lambda cmd, cwd: _FakeProc((b"ok", b"", 0))))

    assert ClaudeBackend().invoke("do the thing", repo) == "ok"


def test_invoke_raises_when_origin_changes_during_the_run(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)

    def fake_start_process(cmd, cwd):
        _git(cwd, "remote", "set-url", "origin", "git@github.com:someone-else/other.git")
        return _FakeProc((b"ok", b"", 0))

    monkeypatch.setattr(ClaudeBackend, "_start_process", staticmethod(fake_start_process))

    with pytest.raises(RepoIdentityError):
        ClaudeBackend().invoke("do the thing", repo)


def test_sigkill_on_timeout_retries_once_and_succeeds(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    procs = [
        _FakeProc(subprocess.TimeoutExpired(cmd="claude", timeout=1), pid=111),
        _FakeProc((b"ok on retry", b"", 0), pid=222),
    ]
    start_process_calls = []

    def fake_start_process(cmd, cwd):
        start_process_calls.append((cmd, cwd))
        return procs.pop(0)

    killpg_mock = Mock()
    monkeypatch.setattr(ClaudeBackend, "_start_process", staticmethod(fake_start_process))
    monkeypatch.setattr("os.killpg", killpg_mock)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    result = ClaudeBackend().invoke("do the thing", repo)

    assert result == "ok on retry"
    assert len(start_process_calls) == 2
    killpg_mock.assert_called_once_with(111, 9)  # SIGKILL == 9, against the killed attempt's pid


def test_sigkill_retry_resets_cwd_to_a_clean_tree(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    tracked = repo / "README.md"
    tracked.write_text("dirtied by the killed attempt\n")
    (repo / "untracked.txt").write_text("stray file\n")

    procs = [
        _FakeProc(subprocess.TimeoutExpired(cmd="claude", timeout=1)),
        _FakeProc((b"ok", b"", 0)),
    ]
    monkeypatch.setattr(ClaudeBackend, "_start_process", staticmethod(lambda cmd, cwd: procs.pop(0)))
    monkeypatch.setattr("os.killpg", Mock())
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    ClaudeBackend().invoke("do the thing", repo)

    assert tracked.read_text() == "hello\n"
    assert not (repo / "untracked.txt").exists()


def test_final_timeout_after_exhausting_retries_raises(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    procs = [
        _FakeProc(subprocess.TimeoutExpired(cmd="claude", timeout=1)),
        _FakeProc(subprocess.TimeoutExpired(cmd="claude", timeout=1)),
    ]
    start_process_calls = []

    def fake_start_process(cmd, cwd):
        start_process_calls.append((cmd, cwd))
        return procs.pop(0)

    monkeypatch.setattr(ClaudeBackend, "_start_process", staticmethod(fake_start_process))
    monkeypatch.setattr("os.killpg", Mock())
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    with pytest.raises(subprocess.TimeoutExpired):
        ClaudeBackend().invoke("do the thing", repo)

    assert len(start_process_calls) == 2


def test_max_retries_zero_never_retries(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    proc = _FakeProc(subprocess.TimeoutExpired(cmd="claude", timeout=1))
    start_process_calls = []

    def fake_start_process(cmd, cwd):
        start_process_calls.append((cmd, cwd))
        return proc

    monkeypatch.setattr(ClaudeBackend, "_start_process", staticmethod(fake_start_process))
    monkeypatch.setattr("os.killpg", Mock())
    monkeypatch.setattr("os.getpgid", lambda pid: pid)

    with pytest.raises(subprocess.TimeoutExpired):
        ClaudeBackend(max_retries=0).invoke("do the thing", repo)

    assert len(start_process_calls) == 1
