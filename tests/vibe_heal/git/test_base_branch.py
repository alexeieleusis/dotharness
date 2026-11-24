"""Tests for the shared default base branch resolver."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dotharness.code_host import CodeHostError
from dotharness.code_host.testing import InMemoryCodeHost
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.git.base_branch import DEFAULT_BASE_BRANCH, BaseResolution, resolve_default_base


def _config(base_branch: str | None = None) -> VibeHealConfig:
    config = VibeHealConfig(
        sonarqube_url="https://sonar.test.com",
        sonarqube_token="t",  # noqa: S106
        sonarqube_project_key="p",
    )
    return config.model_copy(update={"base_branch": base_branch})


def _analyzer(existing: set[str]) -> MagicMock:
    analyzer = MagicMock()
    analyzer.validate_branch_exists.side_effect = lambda ref: ref in existing
    return analyzer


@pytest.fixture
def fake() -> InMemoryCodeHost:
    return InMemoryCodeHost()


async def test_setting_wins_without_host_call(fake: InMemoryCodeHost) -> None:
    fake.set_default_branch("trunk")
    result = await resolve_default_base(_config("develop"), fake.code_host(), _analyzer({"origin/develop"}))
    assert result == BaseResolution("origin/develop", "setting", False)
    assert not any(c for c in fake.calls if "default_branch" in str(c))


async def test_setting_falls_back_to_local_branch(fake: InMemoryCodeHost) -> None:
    result = await resolve_default_base(_config("develop"), fake.code_host(), _analyzer({"develop"}))
    assert (result.ref, result.source) == ("develop", "setting")


async def test_setting_missing_everywhere_returns_origin_ref(fake: InMemoryCodeHost) -> None:
    result = await resolve_default_base(_config("develop"), fake.code_host(), _analyzer(set()))
    assert (result.ref, result.source) == ("origin/develop", "setting")


async def test_setting_with_origin_prefix_is_not_double_prefixed(fake: InMemoryCodeHost) -> None:
    result = await resolve_default_base(_config("origin/develop"), fake.code_host(), _analyzer({"origin/develop"}))
    assert result.ref == "origin/develop"


async def test_host_default_is_prefixed_without_local_check(fake: InMemoryCodeHost) -> None:
    fake.set_default_branch("develop")
    analyzer = _analyzer(set())
    result = await resolve_default_base(_config(), fake.code_host(), analyzer)
    assert result == BaseResolution("origin/develop", "host", False)
    analyzer.validate_branch_exists.assert_not_called()


async def test_main_default_is_noop(fake: InMemoryCodeHost) -> None:
    fake.set_default_branch("main")
    result = await resolve_default_base(_config(), fake.code_host(), _analyzer(set()))
    assert result.ref == DEFAULT_BASE_BRANCH == "origin/main"


async def test_code_host_error_falls_back(fake: InMemoryCodeHost) -> None:
    fake.fail_always("Repository.default_branch", CodeHostError("boom"))
    result = await resolve_default_base(_config(), fake.code_host(), _analyzer(set()))
    assert result == BaseResolution("origin/main", "fallback", True)


async def test_oserror_from_host_falls_back(fake: InMemoryCodeHost) -> None:
    host = fake.code_host()
    with patch.object(host.repository, "default_branch", new_callable=AsyncMock, side_effect=OSError("no gh")):
        result = await resolve_default_base(_config(), host, _analyzer(set()))
    assert result == BaseResolution("origin/main", "fallback", True)


async def test_empty_host_default_is_a_failure(fake: InMemoryCodeHost) -> None:
    fake.set_default_branch("")
    result = await resolve_default_base(_config(), fake.code_host(), _analyzer(set()))
    assert result == BaseResolution("origin/main", "fallback", True)


async def test_setting_set_means_host_failure_is_irrelevant(fake: InMemoryCodeHost) -> None:
    fake.fail_always("Repository.default_branch", CodeHostError("boom"))
    result = await resolve_default_base(_config("develop"), fake.code_host(), _analyzer({"origin/develop"}))
    assert result.host_read_failed is False
