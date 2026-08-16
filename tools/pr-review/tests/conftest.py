import pytest


@pytest.fixture(autouse=True)
def no_harness_repo_root(monkeypatch):
    """Tests exercise Backend.run() with synthetic cwds like "/tmp" that have
    nothing to do with this checkout. Without this, the harness-repo-unchanged
    guard would shell out to git against dotharness's real checkout and collide
    with tests that mock subprocess.Popen for the backend invocation itself."""
    monkeypatch.setattr("harness.backend._HARNESS_REPO_ROOT", None)


@pytest.fixture(autouse=True)
def _clear_ssh_host_cache():
    """`_resolve_ssh_host` is memoized process-wide; clear it so a mocked
    `ssh -G` result from one test can't leak into another test reusing the
    same host/alias name."""
    from harness.repo_guard import _resolve_ssh_host

    _resolve_ssh_host.cache_clear()
    yield
    _resolve_ssh_host.cache_clear()


@pytest.fixture
def tmp_xdg(tmp_path, monkeypatch):
    """Redirect XDG runtime dir to tmp_path for all modules."""
    xdg = tmp_path / "dotharness"
    xdg.mkdir()
    monkeypatch.setattr("harness.backend.XDG_DATA", xdg)
    monkeypatch.setattr("harness.state.XDG_DATA", xdg)
    monkeypatch.setattr("harness.lock.XDG_RUNTIME", xdg)
    monkeypatch.setattr("harness.cli.XDG_LOGS", tmp_path / "logs")
    return xdg


@pytest.fixture
def minimal_toml(tmp_path):
    """Write a minimal .harness.toml and return its path."""
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    content = f"""
[harness]
backend = "opencode"
gh_token_cmd = "echo test-token"
knowledge_dir = "{knowledge_dir}"

[repo]
name = "acme/frontend"
working_dir = "{tmp_path}"
"""
    p = tmp_path / ".harness.toml"
    p.write_text(content)
    return p
