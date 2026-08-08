import pytest


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
