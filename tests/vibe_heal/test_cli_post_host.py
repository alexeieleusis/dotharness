"""Real --post code host startup."""

REAL_CODE_HOST_STARTUP = True


def test_start_code_host_for_post_reads_env_file(tmp_path, monkeypatch) -> None:
    """The code host named only in the env file reaches create_code_host."""
    from dotharness.vibe_heal import cli

    env = tmp_path / ".env.vibeheal"
    env.write_text('DOTHARNESS_CODE_HOST=my_pkg.hosts:Foo\nDOTHARNESS_CODE_HOST_OPTIONS={"a": 1}\n')
    monkeypatch.delenv("DOTHARNESS_CODE_HOST", raising=False)
    monkeypatch.delenv("DOTHARNESS_CODE_HOST_OPTIONS", raising=False)
    seen = {}
    monkeypatch.setattr(cli, "create_code_host", lambda sel: seen.setdefault("sel", sel))
    cli._start_code_host_for_post(str(env))
    assert seen["sel"].name == "my_pkg.hosts:Foo"
    assert seen["sel"].options == {"a": 1}
