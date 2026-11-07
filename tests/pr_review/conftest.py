import pytest


@pytest.fixture
def tmp_xdg(tmp_path, monkeypatch):
    """Redirect XDG runtime dir to tmp_path for all modules."""
    xdg = tmp_path / "dotharness"
    xdg.mkdir()
    monkeypatch.setattr("dotharness.ai_backend.TMP_DIR", xdg / "tmp")
    monkeypatch.setattr("dotharness.pr_review.state.XDG_DATA", xdg)
    monkeypatch.setattr("dotharness.pr_review.lock.XDG_RUNTIME", xdg)
    monkeypatch.setattr("dotharness.pr_review.cli.XDG_LOGS", tmp_path / "logs")
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
