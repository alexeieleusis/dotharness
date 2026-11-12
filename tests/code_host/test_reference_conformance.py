"""The reference code host (gh) passes the conformance suite. A stubbed runner is used: no network, no real gh."""

import json

import pytest

from dotharness.code_host import HostSelection, create_code_host
from dotharness.code_host.errors import AuthenticationError, CodeHostTimeoutError
from dotharness.code_host.testing import SECRET, CodeHostConformance, ConformanceTarget, PostedItem
from dotharness.subprocess_runner import RunResult


class GhStub:
    """Plays the ``gh`` command line for the reference host. It is the seeder of the conformance target."""

    def __init__(self) -> None:
        self._available = True
        self._change_request: tuple[int, str] | None = None
        self._reject_inline: str | None = None
        self._reject_all: str | None = None
        self._error: Exception | None = None
        self._posts: list[PostedItem] = []
        self._attempts = 0
        self._rejected = False
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []

    def set_available(self, available: bool) -> None:
        self._available = available

    def set_current_change_request(self, number: int | None, base: str = "main") -> None:
        self._change_request = None if number is None else (number, base)

    def reject_inline_reviews(self, reason: str) -> None:
        self._reject_inline = reason

    def reject_all_posts(self, reason: str) -> None:
        self._reject_all = reason

    def fail_posts_with(self, error: Exception) -> None:
        self._error = error

    def posts(self) -> list[PostedItem]:
        return list(self._posts)

    def post_attempts(self) -> int:
        return self._attempts

    async def run(self, cmd: list[str], **kwargs: object) -> RunResult:
        if cmd == ["echo", SECRET]:  # the token command: it is not a gh call
            return RunResult(0, f"{SECRET}\n", "")
        self.commands.append(cmd)
        self.envs.append(kwargs.get("env"))  # type: ignore[arg-type]
        if "--version" in cmd:
            return RunResult(0, "gh version 2.52.0", "") if self._available else RunResult(1, "", "gh missing")
        if "pr" in cmd and "view" in cmd:
            if self._change_request is None:
                return RunResult(1, "", "no pull requests found for branch")
            number, base = self._change_request
            return RunResult(0, json.dumps({"number": number, "baseRefName": base}), "")
        if "api" in cmd:
            return self._post(json.loads(str(kwargs["stdin"])))
        return RunResult(1, "", "unexpected command")

    def _post(self, payload: dict) -> RunResult:
        self._attempts += 1
        if isinstance(self._error, CodeHostTimeoutError):
            return RunResult(124, "", "timed out", timed_out=True)
        if isinstance(self._error, AuthenticationError):
            return RunResult(1, "", f"HTTP 401 {self._error}")  # exercises the host's stderr -> error mapping
        if self._error is not None:
            raise self._error  # the runner itself fails: the host must not fall back
        inline = payload["comments"]
        reason = self._reject_all or (self._reject_inline if inline else None)
        if reason is not None:
            self._rejected = True
            return RunResult(1, "", f"HTTP 422 {reason}")
        self._posts.append(PostedItem("comment" if self._rejected else "review", payload["body"], len(inline)))
        return RunResult(0, "{}", "")


class TestReferenceHostConformance(CodeHostConformance):
    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dotharness.code_host.reference.run", lambda cmd, **kw: self.stub.run(cmd, **kw))

    async def make_target(self) -> ConformanceTarget:
        self.stub = GhStub()  # a fresh seeder per target: the suite may build several targets in one test
        host = create_code_host(
            HostSelection(setting="code_host", name="reference", options={}, token_cmd=["echo", SECRET])
        )
        return ConformanceTarget(host=host, seeder=self.stub, secret=SECRET)

    async def test_token_reaches_gh_only_as_gh_token_in_the_environment(self) -> None:
        """The token command runs once. Its token is ``GH_TOKEN`` in the gh environment, never in argv or GITHUB_TOKEN."""
        target = await self.make_target()
        await target.host.session.available()
        self.stub.set_current_change_request(1)
        await target.host.change_requests.for_current_branch(target.repository)
        assert self.stub.commands
        assert not [c for c in self.stub.commands if SECRET in " ".join(c)]
        env = self.stub.envs[-1]
        assert env is not None
        assert env["GH_TOKEN"] == SECRET
        assert "GITHUB_TOKEN" not in env or env["GITHUB_TOKEN"] != SECRET
