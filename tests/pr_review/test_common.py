"""Host scaffold, credential and fail-open/fail-closed wrappers in ``runners/common.py``."""

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from dotharness.code_host import (
    AuthenticationError,
    ChangeRequestState,
    CodeHostError,
    ConfigurationError,
    RepositoryRef,
    TokenCommandError,
)
from dotharness.code_host.testing import SECRET, InMemoryCodeHost
from dotharness.pr_review.config import HarnessConfig, HarnessSection, RepoConfig
from dotharness.pr_review.runners.common import (
    agent_env,
    build_agent_base_env,
    host_current_user,
    host_selection,
    host_token,
    is_change_request_open,
    open_host,
)


def _config(tmp_path: Path, **harness: object) -> HarnessConfig:
    return HarnessConfig(
        harness=HarnessSection(**harness),  # ty: ignore[invalid-argument-type]
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
    )


@pytest.fixture
def fake() -> InMemoryCodeHost:
    return InMemoryCodeHost(repository="acme/frontend", user="me")


def test_host_selection_carries_config(tmp_path):
    cfg = _config(tmp_path, host="examplehost", host_options={"a": 1}, gh_token_cmd="old cmd")  # noqa: S106
    selection = host_selection(cfg)
    assert selection.setting == "host"
    assert selection.name == "examplehost"
    assert dict(selection.options) == {"a": 1}
    assert selection.token_cmd == "old cmd"  # noqa: S105
    assert selection.working_directory == tmp_path


def test_host_selection_default_token_command_and_missing_directory(tmp_path):
    selection = host_selection(_config(tmp_path / "absent"))
    assert selection.token_cmd == "gh auth token"  # noqa: S105
    assert selection.working_directory is None
    assert selection.name is None


def test_base_env_prepends_path_and_applies_env_vars(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    base = build_agent_base_env(["/x/bin", "/y/bin"], {"FOO": "bar"})
    assert base["PATH"] == "/x/bin:/y/bin:/usr/bin"
    assert base["FOO"] == "bar"
    assert "GITHUB_TOKEN" not in base or base["GITHUB_TOKEN"] == os.environ.get("GITHUB_TOKEN")


def test_base_env_without_prepend_keeps_path(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    assert build_agent_base_env([], {})["PATH"] == "/usr/bin"


async def test_agent_env_adds_credential_variables_that_win(tmp_path, fake):
    cfg = _config(tmp_path, env={"GITHUB_TOKEN": "from-config", "KEEP": "1"})
    env = await agent_env(fake.code_host(), cfg)
    assert env["GITHUB_TOKEN"] == SECRET
    assert env["GH_TOKEN"] == SECRET
    assert env["KEEP"] == "1"
    assert "Session.agent_environment" in [c.operation for c in fake.calls]


async def test_agent_env_credential_failure_propagates(tmp_path, fake):
    fake.fail_next("Session.agent_environment", TokenCommandError("no token"))
    with pytest.raises(TokenCommandError):
        await agent_env(fake.code_host(), _config(tmp_path))


async def test_open_host_builds_once_in_order(tmp_path, fake):
    with patch("dotharness.pr_review.runners.common.create_code_host", return_value=fake.code_host()) as factory:
        scaffold = await open_host(_config(tmp_path))
    factory.assert_called_once()
    assert scaffold.repo == RepositoryRef("acme/frontend")
    assert scaffold.env["GITHUB_TOKEN"] == SECRET
    operations = [c.operation for c in fake.calls]
    assert operations.count("Repository.resolve") == 1
    assert operations.index("Repository.resolve") < operations.index("Session.agent_environment")
    assert fake.writes == []


async def test_open_host_factory_configuration_error_exits_with_message(tmp_path, capsys):
    with (
        patch("dotharness.pr_review.runners.common.create_code_host", side_effect=ConfigurationError("bad host")),
        pytest.raises(SystemExit) as exc,
    ):
        await open_host(_config(tmp_path))
    assert exc.value.code == 1
    assert "bad host" in capsys.readouterr().err


async def test_open_host_resolve_error_exits_with_message(tmp_path, fake, capsys):
    fake.fail_next("Repository.resolve", ConfigurationError("bad identity"))
    with (
        patch("dotharness.pr_review.runners.common.create_code_host", return_value=fake.code_host()),
        pytest.raises(SystemExit) as exc,
    ):
        await open_host(_config(tmp_path))
    assert exc.value.code == 1
    assert "bad identity" in capsys.readouterr().err


async def test_open_host_credential_failure_propagates(tmp_path, fake):
    fake.fail_next("Session.agent_environment", AuthenticationError("expired"))
    with (
        patch("dotharness.pr_review.runners.common.create_code_host", return_value=fake.code_host()),
        pytest.raises(AuthenticationError),
    ):
        await open_host(_config(tmp_path))


async def test_host_token_returns_credential(fake):
    assert await host_token(fake.code_host()) == SECRET


async def test_host_token_failure_propagates(fake):
    fake.fail_next("Session.credential", TokenCommandError("failed"))
    with pytest.raises(TokenCommandError):
        await host_token(fake.code_host())


async def test_is_change_request_open_states(fake):
    repo = await fake.code_host().repository.resolve("acme/frontend")
    open_cr = fake.add_change_request(repo, head_reference="a")
    merged = fake.add_change_request(repo, head_reference="b", state=ChangeRequestState.MERGED)
    host = fake.code_host()
    assert await is_change_request_open(host, repo, open_cr.number) is True
    assert await is_change_request_open(host, repo, merged.number) is False


async def test_is_change_request_open_fails_closed(fake):
    repo = await fake.code_host().repository.resolve("acme/frontend")
    cr = fake.add_change_request(repo, head_reference="a")
    fake.fail_next("ChangeRequests.state", CodeHostError("boom"))
    host = fake.code_host()
    assert await is_change_request_open(host, repo, cr.number) is False
    assert await is_change_request_open(host, repo, 999) is False  # missing: CodeHostError


async def test_host_current_user_returns_login(fake):
    assert await host_current_user(fake.code_host()) == "me"


async def test_host_current_user_fails_open_and_logs(fake, caplog):
    fake.fail_next("Session.current_user", AuthenticationError("token revoked"))
    with caplog.at_level(logging.ERROR):
        assert await host_current_user(fake.code_host()) == ""
    assert any("token revoked" in r.getMessage() for r in caplog.records)
    assert SECRET not in caplog.text
