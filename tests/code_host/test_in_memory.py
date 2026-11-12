"""The in-memory fake passes the conformance suite and behaves as a consumer test needs."""

import pytest

from dotharness.code_host.declarations import Capability
from dotharness.code_host.errors import ConfigurationError, UnsupportedCapabilityError
from dotharness.code_host.testing import CodeHostConformance, ConformanceTarget, InMemoryCodeHost, PostedItem


class TestInMemoryCodeHost(CodeHostConformance):
    async def make_target(self) -> ConformanceTarget:
        return InMemoryCodeHost().target()


class TestInMemorySpecifics:
    async def test_declares_four_capabilities(self) -> None:
        host = InMemoryCodeHost().code_host()
        assert host.declaration.capabilities == {
            Capability.SESSION,
            Capability.REPOSITORY,
            Capability.CHANGE_REQUESTS,
            Capability.DISCUSSION,
        }

    async def test_repository_and_user(self) -> None:
        fake = InMemoryCodeHost(repository="o/r", user="me")
        host = fake.code_host()
        repo = await host.repository.current()
        assert repo.value == "o/r"
        assert await host.repository.resolve("a/b") == type(repo)("a/b")
        assert await host.repository.default_branch(repo) == "main"
        assert await host.repository.display_name(repo) == "o/r"
        assert await host.session.current_user() == "me"
        fake.set_repository("x/y")
        assert (await host.repository.current()).value == "x/y"

    async def test_resolve_empty_identity_raises(self) -> None:
        with pytest.raises(ConfigurationError):
            await InMemoryCodeHost().code_host().repository.resolve("")

    async def test_find_by_branch_is_empty_and_other_calls_unsupported(self) -> None:
        host = InMemoryCodeHost().code_host()
        repo = await host.repository.current()
        assert await host.change_requests.find_by_branch(repo, "b") == []
        with pytest.raises(UnsupportedCapabilityError):
            await host.change_requests.get(repo, 1)
        with pytest.raises(UnsupportedCapabilityError):
            await host.discussion.comments(repo, 1)
        with pytest.raises(UnsupportedCapabilityError):
            await host.session.credential()
        with pytest.raises(UnsupportedCapabilityError):
            await host.session.agent_environment_names()
        with pytest.raises(UnsupportedCapabilityError):
            await host.session.agent_environment()
        with pytest.raises(AttributeError):
            getattr(host.checks, "_private")  # noqa: B009
        with pytest.raises(AttributeError):
            getattr(host.checks, "not_a_method")  # noqa: B009

    async def test_clear_failures_restores_posting(self) -> None:
        fake = InMemoryCodeHost()
        host = fake.code_host()
        fake.reject_all_posts("no")
        fake.clear_failures()
        result = await host.discussion.post_review(await host.repository.current(), 1, body="b", inline_comments=[])
        assert result.fell_back is False
        assert fake.posts() == [PostedItem("review", "b", 0)]


class _LeakyHost(InMemoryCodeHost):
    def require_client(self) -> None:
        if not self._available:
            msg = f"client unavailable (token={self._token})"
            raise OSError(msg)


class _LeakyConformance(CodeHostConformance):
    async def make_target(self) -> ConformanceTarget:
        return _LeakyHost().target()


class _WrongSecretConformance(CodeHostConformance):
    async def make_target(self) -> ConformanceTarget:
        return InMemoryCodeHost(token="other").target()  # noqa: S106


class TestSecretCheckCanFail:
    async def test_leaking_host_fails(self) -> None:
        with pytest.raises(AssertionError):
            await _LeakyConformance().test_secret_sentinel_never_leaks()

    async def test_host_without_sentinel_fails(self) -> None:
        with pytest.raises(AssertionError, match="SECRET"):
            await _WrongSecretConformance().test_secret_sentinel_never_leaks()
