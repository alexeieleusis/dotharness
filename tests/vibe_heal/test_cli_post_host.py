"""Real --post code host startup."""

import pytest

from dotharness.vibe_heal import cli
from dotharness.vibe_heal.config import InvalidConfigurationError

REAL_CODE_HOST_STARTUP = True

_SENTINEL = object()


@pytest.fixture
def seen(monkeypatch):
    """Clean code host env vars; capture the selection passed to create_code_host."""
    monkeypatch.delenv("DOTHARNESS_CODE_HOST", raising=False)
    monkeypatch.delenv("DOTHARNESS_CODE_HOST_OPTIONS", raising=False)
    captured = {}

    def fake(sel):
        captured["sel"] = sel
        return _SENTINEL

    monkeypatch.setattr(cli, "create_code_host", fake)
    return captured


def test_start_code_host_for_post_reads_env_file(tmp_path, seen) -> None:
    """The code host named only in the env file reaches create_code_host."""
    env = tmp_path / ".env.vibeheal"
    env.write_text('DOTHARNESS_CODE_HOST=my_pkg.hosts:Foo\nDOTHARNESS_CODE_HOST_OPTIONS={"a": 1}\n')
    assert cli._start_code_host_for_post(str(env)) is _SENTINEL
    assert seen["sel"].name == "my_pkg.hosts:Foo"
    assert seen["sel"].options == {"a": 1}


def test_env_file_wins_over_env_var(tmp_path, seen, monkeypatch) -> None:
    env = tmp_path / "e"
    env.write_text("DOTHARNESS_CODE_HOST=my_pkg.hosts:Foo\n")
    monkeypatch.setenv("DOTHARNESS_CODE_HOST", "other.pkg:Bar")
    cli._start_code_host_for_post(str(env))
    assert seen["sel"].name == "my_pkg.hosts:Foo"  # same precedence as VibeHealConfig(env_file=...)


def test_missing_env_file_raises(tmp_path, seen) -> None:
    with pytest.raises(InvalidConfigurationError):
        cli._start_code_host_for_post(str(tmp_path / "nope"))


@pytest.mark.parametrize("raw", ["not-json", "[1]"])
def test_bad_options_raise_without_leaking_value(tmp_path, seen, monkeypatch, raw) -> None:
    env = tmp_path / "e"
    env.write_text("")
    monkeypatch.setenv("DOTHARNESS_CODE_HOST_OPTIONS", raw)
    with pytest.raises(InvalidConfigurationError) as exc:
        cli._start_code_host_for_post(str(env))
    assert raw not in str(exc.value)


def test_default_files_dotenv_overrides_dotenv_vibeheal(tmp_path, seen, monkeypatch) -> None:
    (tmp_path / ".env").write_text("DOTHARNESS_CODE_HOST=a:A\nDOTHARNESS_CODE_HOST_OPTIONS={}\n")
    (tmp_path / ".env.vibeheal").write_text("DOTHARNESS_CODE_HOST=b:B\n")
    monkeypatch.chdir(tmp_path)
    cli._start_code_host_for_post(None)
    assert seen["sel"].name == "a:A"


def test_no_files_gives_unset_selection(tmp_path, seen, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cli._start_code_host_for_post(None)
    assert seen["sel"].name is None
    assert seen["sel"].options == {}


def test_empty_value_is_none(tmp_path, seen, monkeypatch) -> None:
    monkeypatch.setenv("DOTHARNESS_CODE_HOST", "")
    monkeypatch.chdir(tmp_path)
    cli._start_code_host_for_post(None)
    assert seen["sel"].name is None
