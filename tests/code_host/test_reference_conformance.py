"""The reference code host (gh) passes the conformance suite. A stubbed runner is used: no network, no real gh."""

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from dotharness.code_host import HostSelection, create_code_host
from dotharness.code_host.errors import AuthenticationError, CodeHostTimeoutError
from dotharness.code_host.models import ChangeRequest, ChangeRequestState, RepositoryRef, User, WorkItem, WorkItemState
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
        self.token_runs = 0
        self.envs: list[dict[str, str] | None] = []
        # Seeded work items and change requests. ``run`` renders them as ``gh`` output.
        self.repository = RepositoryRef("example/repo")
        self.default_branch = "main"
        self.work_items: list[WorkItem] = []
        self.change_requests: list[ChangeRequest] = []
        self.links: list[tuple[int, int]] = []
        self.labels: set[str] = set()
        self._clock = 0

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

    # -- seeder: work items and change requests -----------------------------------------------------------------

    def _tick(self) -> datetime:
        self._clock += 1
        return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self._clock)

    def set_default_branch(self, name: str) -> None:
        self.default_branch = name

    def add_work_item(
        self,
        repo: RepositoryRef,
        *,
        title: str,
        body: str = "",
        labels: Sequence[str] = (),
        assignees: Sequence[str] = (),
        state: WorkItemState = WorkItemState.OPEN,
        number: int | None = None,
    ) -> WorkItem:
        item = WorkItem(
            repo,
            number or len(self.work_items) + 1,
            title,
            body,
            tuple(labels),
            tuple(User(a) for a in assignees),
            self._tick(),
            state,
        )
        self.work_items.append(item)
        self.labels.update(labels)
        return item

    def add_change_request(
        self,
        repo: RepositoryRef,
        *,
        head_reference: str,
        base_reference: str = "main",
        title: str = "",
        body: str = "",
        state: ChangeRequestState = ChangeRequestState.OPEN,
        is_draft: bool = False,
        author: str = "fake-user",
        head_revision: str | None = None,
        number: int | None = None,
        requested_reviewers: Sequence[str] = (),
    ) -> ChangeRequest:
        n = number or len(self.change_requests) + 1
        change_request = ChangeRequest(
            repo,
            n,
            title,
            body,
            f"https://example.test/{repo.value}/pull/{n}",
            state,
            is_draft,
            User(author),
            head_reference,
            f"rev-{n}" if head_revision is None else head_revision,
            base_reference,
            False,
            self._tick(),
            requested_reviewers=tuple(User(r) for r in requested_reviewers),
        )
        self.change_requests.append(change_request)
        return change_request

    def link_closing(self, repo: RepositoryRef, *, work_item: int, change_request: int) -> None:
        self.links.append((work_item, change_request))

    # -- gh output rendered from the seeded state ----------------------------------------------------------------

    @staticmethod
    def _flag(cmd: list[str], name: str) -> str | None:
        return cmd[cmd.index(name) + 1] if name in cmd else None

    @staticmethod
    def _ok(data: object) -> RunResult:
        return RunResult(0, json.dumps(data), "")

    def _pr_row(self, cr: ChangeRequest, fields: str) -> dict[str, object]:
        row: dict[str, object] = {
            "number": cr.number,
            "state": cr.state.value.upper(),
            "headRefOid": cr.head_revision,
            "body": cr.body,
            "url": cr.url,
        }
        return {k: v for k, v in row.items() if k in fields.split(",")}

    def _issue(self, cmd: list[str]) -> RunResult:
        verb = cmd[2]
        if verb == "list":
            assignee, label = self._flag(cmd, "--assignee"), self._flag(cmd, "--label")
            found = [
                w
                for w in self.work_items
                if w.state is WorkItemState.OPEN
                and (not assignee or assignee in {u.login for u in w.assignees})
                and (not label or label in w.labels)
            ]
            rows = [
                {
                    "number": w.number,
                    "title": w.title,
                    "createdAt": w.created_at.isoformat().replace("+00:00", "Z"),
                    "assignees": [{"login": u.login} for u in w.assignees],
                    "labels": [{"name": name} for name in w.labels],
                }
                for w in found[: int(self._flag(cmd, "--limit") or 30)]
            ]
            return self._ok(rows)
        number = int(cmd[3])
        if verb == "view":
            refs = [{"number": cr} for wi, cr in self.links if wi == number]
            return self._ok({"closedByPullRequestsReferences": refs})
        if verb == "edit":
            label = self._flag(cmd, "--remove-label") or ""
            if label not in self.labels:
                return RunResult(1, "", f"failed to update: '{label}' not found")
            self.work_items = [
                replace(w, labels=tuple(x for x in w.labels if x != label)) if w.number == number else w
                for w in self.work_items
            ]
            return RunResult(0, "", "")
        return RunResult(1, "", "unexpected command")

    def _pr(self, cmd: list[str]) -> RunResult:
        verb = cmd[2]
        if verb == "create":
            head, base = self._flag(cmd, "--head"), self._flag(cmd, "--base")
            if any(c.head_reference == head and c.base_reference == base for c in self.change_requests):
                return RunResult(1, "", f"a pull request for branch {head} into branch {base} already exists")
            created = self.add_change_request(
                self.repository,
                head_reference=head or "",
                base_reference=base or "",
                title=self._flag(cmd, "--title") or "",
                body=self._flag(cmd, "--body") or "",
                is_draft=True,
            )
            return RunResult(0, f"\nCreating draft pull request\n{created.url}\n", "")
        if verb == "list":
            wanted = self._flag(cmd, "--state") or "open"
            head = self._flag(cmd, "--head")
            found = [
                c
                for c in reversed(self.change_requests)
                if c.state.value == wanted and (head is None or c.head_reference == head)
            ]
            limit = int(self._flag(cmd, "--limit") or 30)
            return self._ok([self._pr_row(c, self._flag(cmd, "--json") or "") for c in found[:limit]])
        ref = cmd[3]
        fields = self._flag(cmd, "--json") or ""
        if ref.isdecimal():
            by_number = [c for c in self.change_requests if c.number == int(ref)]
            if not by_number:
                return RunResult(1, "", f"Could not resolve to a PullRequest with the number of {ref}.")
            return self._ok(self._pr_row(by_number[0], fields))
        by_branch = [c for c in self.change_requests if c.head_reference == ref]
        if not by_branch:
            return RunResult(1, "", f"no pull requests found for branch {ref!r}")
        return self._ok(self._pr_row(by_branch[-1], fields))

    def _repo_view(self, cmd: list[str]) -> RunResult:
        if "--jq" in cmd:
            return RunResult(0, f"{self.repository.value}\n", "")
        return self._ok({"defaultBranchRef": {"name": self.default_branch}})

    async def run(self, cmd: list[str], **kwargs: object) -> RunResult:
        if cmd == ["echo", SECRET]:  # the token command: it is not a gh call
            self.token_runs += 1
            return RunResult(0, f"{SECRET}\n", "")
        self.commands.append(cmd)
        self.envs.append(cast("dict[str, str] | None", kwargs.get("env")))
        if "--version" in cmd:
            return RunResult(0, "gh version 2.52.0", "") if self._available else RunResult(1, "", "gh missing")
        if cmd[1:2] == ["issue"]:
            return self._issue(cmd)
        if cmd[1:2] == ["repo"]:
            return self._repo_view(cmd)
        if (
            cmd[1:3] == ["pr", "list"]
            or cmd[1:3] == ["pr", "create"]
            or (cmd[1:3] == ["pr", "view"] and len(cmd) > 3 and not cmd[3].startswith("--"))
        ):
            return self._pr(cmd)
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
        # ``get`` and ``linked_work_items`` of WorkItems and ``display_name`` of Repository are declared but not implemented.
        unsupported = frozenset({"WorkItems.get", "WorkItems.linked_work_items", "Repository.display_name"})
        return ConformanceTarget(host=host, seeder=self.stub, secret=SECRET, unsupported=unsupported)

    async def test_token_reaches_gh_only_as_gh_token_in_the_environment(self) -> None:
        """The token command runs once. Its token is ``GH_TOKEN`` in the gh environment, never in argv or GITHUB_TOKEN."""
        target = await self.make_target()
        await target.host.session.available()
        self.stub.set_current_change_request(1)
        await target.host.change_requests.for_current_branch(target.repository)
        await target.host.change_requests.for_current_branch(target.repository)
        assert self.stub.commands
        assert self.stub.token_runs == 1
        assert not [c for c in self.stub.commands if SECRET in " ".join(c)]
        # ``gh --version`` (``Session.available``) needs no credential, so it runs without a ``GH_TOKEN`` environment.
        gh_calls = [(c, e) for c, e in zip(self.stub.commands, self.stub.envs, strict=True) if "--version" not in c]
        assert gh_calls
        for _, env in gh_calls:
            assert env is not None
            assert env["GH_TOKEN"] == SECRET
            assert env.get("GITHUB_TOKEN") != SECRET
