"""The reference code host (gh) passes the conformance suite. A stubbed runner is used: no network, no real gh."""

import itertools
import json
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from dotharness.code_host import HostSelection, create_code_host, reference
from dotharness.code_host.declarations import Anchor
from dotharness.code_host.errors import AuthenticationError, CodeHostError, CodeHostTimeoutError
from dotharness.code_host.models import (
    ChangeRequest,
    ChangeRequestState,
    Check,
    CheckStatus,
    Comment,
    CommitInfo,
    Job,
    RepositoryRef,
    Review,
    ReviewState,
    ReviewThread,
    User,
    Workflow,
    WorkItem,
    WorkItemState,
)
from dotharness.code_host.testing import SECRET, CodeHostConformance, ConformanceTarget, PostedItem
from dotharness.subprocess_runner import RunResult


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


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
        self.user = "fake-user"
        self.commits: dict[int, list[CommitInfo]] = {}
        self.reviews: dict[int, list[Review]] = {}
        self.comments: dict[int, list[Comment]] = {}
        self.threads: dict[int, list[ReviewThread]] = {}
        self.checks: dict[int, list[Check]] = {}
        self.required: set[tuple[int, str]] = set()
        self.workflows: list[Workflow] = []
        self.jobs: dict[str, tuple[Job, str]] = {}
        self.viewed: dict[int, dict[str, bool]] = {}
        self._ids = 0
        self._pending: tuple[str, Exception] | None = None

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

    def _next_id(self) -> str:
        self._ids += 1
        return str(self._ids)

    def set_user(self, login: str) -> None:
        self.user = login

    def add_commit(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        revision: str,
        message: str | None = None,
        authors: Sequence[str] = (),
    ) -> CommitInfo:
        commit = CommitInfo(revision, message, tuple(User(a) for a in authors), self._tick())
        self.commits.setdefault(number, []).append(commit)
        return commit

    def add_review(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        author: str,
        state: ReviewState,
        revision: str | None = None,
        body: str = "",
    ) -> Review:
        review = Review(self._next_id(), User(author), state, body, self._tick(), revision, ())
        self.reviews.setdefault(number, []).append(review)
        return review

    def add_comment(self, repo: RepositoryRef, number: int, *, author: str, body: str) -> Comment:
        comment_id = self._next_id()
        url = f"https://example.test/{repo.value}/pull/{number}#issuecomment-{comment_id}"
        comment = Comment(comment_id, User(author), body, self._tick(), url)
        self.comments.setdefault(number, []).append(comment)
        return comment

    def add_thread(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        anchor: Anchor | None,
        comments: Sequence[Comment],
        is_resolved: bool = False,
    ) -> ReviewThread:
        thread = ReviewThread(f"thread-{self._next_id()}", is_resolved, anchor, tuple(comments))
        self.threads.setdefault(number, []).append(thread)
        return thread

    def add_check(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        name: str,
        status: CheckStatus,
        required: bool = False,
        job_id: str | None = None,
    ) -> Check:
        started = self._tick()
        url = f"https://example.test/{repo.value}/runs/{self._next_id()}"
        check = Check(
            name,
            status,
            url if job_id is None else f"{url}/job/{job_id}",
            None,
            started,
            None if status is CheckStatus.PENDING else started + timedelta(seconds=1),
            job_id,
        )
        self.checks.setdefault(number, []).append(check)
        if required:
            self.required.add((number, name))
        return check

    def add_workflow(self, repo: RepositoryRef, *, name: str) -> Workflow:
        workflow = Workflow(self._next_id(), name)
        self.workflows.append(workflow)
        return workflow

    def add_job(self, repo: RepositoryRef, *, job_id: str, failed_step: str | None = None, log: str = "") -> Job:
        job = Job(job_id, failed_step)
        self.jobs[job_id] = (job, log)
        return job

    def fail_next(self, operation: str, error: CodeHostError) -> None:
        """The ``gh`` calls of ``operation`` fail the way the error says, until the target is dropped."""
        self._pending = (operation, error)

    def _failure(self, cmd: list[str]) -> RunResult | None:
        """The failure of a pending ``fail_next``. A write that reads first fails at its write call."""
        if self._pending is None:
            return None
        operation, error = self._pending
        if operation == "ChangeRequests.mark_viewed" and "markFileAsViewed" not in " ".join(cmd):
            return None
        if isinstance(error, CodeHostTimeoutError):
            raise TimeoutError  # the host's deadline: it decides if the call was a write
        if isinstance(error, AuthenticationError):
            return RunResult(1, "", f"HTTP 401 {error}")
        if type(error) is CodeHostError:
            return RunResult(1, "", f"HTTP 500 {error}")
        raise error  # any other type: the runner itself fails

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
            "title": cr.title,
            "isDraft": cr.is_draft,
            "headRefName": cr.head_reference,
            "baseRefName": cr.base_reference,
            "isCrossRepository": False,
            "createdAt": _iso(cr.created_at),
            "author": {"login": cr.author.login} if cr.author else None,
            "reviewRequests": [{"login": u.login} for u in cr.requested_reviewers],
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
            if "closedByPullRequestsReferences" not in (self._flag(cmd, "--json") or ""):
                found = [w for w in self.work_items if w.number == number]
                if not found:
                    return RunResult(1, "", f"GraphQL: Could not resolve to an issue with the number of {number}.")
                item = found[0]
                return self._ok({
                    "number": item.number,
                    "title": item.title,
                    "body": item.body,
                    "labels": [{"name": name} for name in item.labels],
                    "assignees": [{"login": u.login} for u in item.assignees],
                    "createdAt": item.created_at.isoformat().replace("+00:00", "Z"),
                    "state": item.state.value.upper(),
                })
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
            author = self._flag(cmd, "--author")
            search = self._flag(cmd, "--search") or ""
            reviewer = (
                search.removeprefix("user-review-requested:") if search.startswith("user-review-requested:") else None
            )
            found = [
                c
                for c in reversed(self.change_requests)
                if c.state.value == wanted
                and (head is None or c.head_reference == head)
                and (author is None or (c.author is not None and c.author.login == author))
                and (reviewer is None or reviewer in {u.login for u in c.requested_reviewers})
            ]
            limit = int(self._flag(cmd, "--limit") or 30)
            return self._ok([self._pr_row(c, self._flag(cmd, "--json") or "") for c in found[:limit]])
        ref = cmd[3]
        fields = self._flag(cmd, "--json") or ""
        if ref.isdecimal() and "closingIssuesReferences" in fields:
            refs = [{"number": wi} for wi, cr in self.links if cr == int(ref)]
            return self._ok({"closingIssuesReferences": refs})
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

    def _route(self, cmd: list[str]) -> RunResult | None:
        """The result for a command that has its own handler, or ``None``."""
        if cmd[1:2] == ["issue"]:
            return self._issue(cmd)
        if cmd[1:2] == ["repo"]:
            return self._repo_view(cmd)
        if cmd[1:3] in (["pr", "checks"], ["pr", "merge"], ["pr", "edit"], ["pr", "comment"], ["run", "view"]):
            return self._pr_action(cmd)
        if cmd[1:2] == ["api"] and "--input" not in cmd:
            return self._api(cmd)
        return None

    async def run(self, cmd: list[str], **kwargs: object) -> RunResult:
        if cmd == ["echo", SECRET]:  # the token command: it is not a gh call
            self.token_runs += 1
            return RunResult(0, f"{SECRET}\n", "")
        self.commands.append(cmd)
        self.envs.append(cast("dict[str, str] | None", kwargs.get("env")))
        if "--version" in cmd:
            return RunResult(0, "gh version 2.52.0", "") if self._available else RunResult(1, "", "gh missing")
        failure = self._failure(cmd)
        if failure is not None:
            return failure
        routed = self._route(cmd)
        if routed is not None:
            return routed
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

    # -- gh commands that act on one change request -------------------------------------------------------------

    def _find(self, number: int) -> ChangeRequest | None:
        return next((c for c in self.change_requests if c.number == number), None)

    def _replace(self, updated: ChangeRequest) -> None:
        self.change_requests = [updated if c.number == updated.number else c for c in self.change_requests]

    def _pr_action(self, cmd: list[str]) -> RunResult:
        if cmd[1] == "run":  # run view --job ID --repo R --log-failed
            found = self.jobs.get(self._flag(cmd, "--job") or "")
            return RunResult(0, found[1], "") if found else RunResult(1, "", "HTTP 404: Not Found")
        verb, number = cmd[2], int(cmd[3])
        cr = self._find(number)
        if cr is None:
            return RunResult(1, "", f"Could not resolve to a PullRequest with the number of {number}.")
        if verb == "merge":
            if self._flag(cmd, "--match-head-commit") != cr.head_revision:
                return RunResult(1, "", "Pull request head branch was modified, the head commit does not match")
            if cr.state is not ChangeRequestState.OPEN:
                return RunResult(1, "", f"Pull request #{number} is not open")
            self._replace(replace(cr, state=ChangeRequestState.MERGED))
            return RunResult(0, "", "")
        if verb == "edit":
            login = self._flag(cmd, "--add-reviewer") or ""
            if login not in {u.login for u in cr.requested_reviewers}:
                self._replace(replace(cr, requested_reviewers=(*cr.requested_reviewers, User(login))))
            return RunResult(0, f"{cr.url}\n", "")
        if verb == "comment":
            comment = self.add_comment(self.repository, number, author=self.user, body=self._flag(cmd, "--body") or "")
            return RunResult(0, f"{comment.url}\n", "")
        return self._checks(cmd, number)

    def _checks(self, cmd: list[str], number: int) -> RunResult:
        required_only = "--required" in cmd
        found = [c for c in self.checks.get(number, []) if not required_only or (number, c.name) in self.required]
        if not found:
            what = "no required checks reported" if required_only else "no checks reported"
            return RunResult(1, "", f"{what} on the 'feature' branch")
        rows = [
            {
                "name": c.name,
                "bucket": c.status.value,
                "link": c.details_url,
                "workflow": "",
                "startedAt": _iso(c.started_at) if c.started_at else "0001-01-01T00:00:00Z",
                "completedAt": _iso(c.completed_at) if c.completed_at else "0001-01-01T00:00:00Z",
            }
            for c in found
        ]
        pending = any(c.status is CheckStatus.PENDING for c in found)
        return RunResult(8 if pending else 0, json.dumps(rows), "")

    # -- gh api: REST and GraphQL -------------------------------------------------------------------------------

    @staticmethod
    def _fields(cmd: list[str]) -> dict[str, str]:
        """The ``-f name=value`` and ``-F name=value`` fields of a ``gh api`` call."""
        return {
            k: v for flag, pair in itertools.pairwise(cmd) if flag in ("-f", "-F") for k, _, v in [pair.partition("=")]
        }

    @staticmethod
    def _page_of(rows: list[Any], fields: dict[str, str]) -> RunResult:
        page = int(fields.get("page", "1"))
        size = int(fields.get("per_page", "30"))
        return RunResult(0, json.dumps(rows[(page - 1) * size : page * size]), "")

    @staticmethod
    def _rest_user(user: User | None) -> dict[str, str] | None:
        return {"login": user.login, "type": "User"} if user else None

    def _api(self, cmd: list[str]) -> RunResult:
        args = cmd[2:]
        fields = self._fields(cmd)
        if args[0] == "user":
            return RunResult(0, f"{self.user}\n", "")
        if args[0] == "graphql":
            return self._graphql(fields)
        if args[:2] == ["--method", "POST"]:
            return self._approve(args[2], fields)
        path = args[2] if args[:2] == ["--method", "GET"] else args[0]
        path = path.removeprefix("repos/").partition("/")  # owner
        rest = f"{path[0]}/{path[2]}"
        match = re.fullmatch(
            r"[^/]+/[^/]+/(pulls/(\d+)/(commits|reviews)|issues/(\d+)/comments|commits/([^/]+)/pulls)", rest
        )
        if match:
            return self._rest_list(match, fields)
        if re.search(r"/actions/workflows\?", rest):
            rows = [{"id": int(w.id), "name": w.name, "state": "active"} for w in self.workflows]
            return RunResult(0, json.dumps({"total_count": len(rows), "workflows": rows[:100]}), "")
        job = re.search(r"/actions/jobs/(.+)$", rest)
        if job:
            found = self.jobs.get(job.group(1))
            if found is None:
                return RunResult(1, "", "HTTP 404: Not Found")
            step = found[0].failed_step
            steps = [{"name": step, "conclusion": "failure"}] if step else [{"name": "ok", "conclusion": "success"}]
            return RunResult(0, json.dumps({"id": job.group(1), "steps": steps}), "")
        return RunResult(1, "", "unexpected api call")

    def _rest_list(self, match: re.Match[str], fields: dict[str, str]) -> RunResult:
        if match.group(5):  # commits/SHA/pulls
            sha = match.group(5)
            found = [
                c
                for c in self.change_requests
                if c.head_revision == sha or any(k.revision == sha for k in self.commits.get(c.number, []))
            ]
            return self._page_of([self._rest_pr(c) for c in found], fields)
        if match.group(4):  # issues/N/comments
            rows = [
                {
                    "id": int(c.id),
                    "user": self._rest_user(c.author),
                    "body": c.body,
                    "created_at": _iso(c.created_at),
                    "html_url": c.url,
                }
                for c in self.comments.get(int(match.group(4)), [])
            ]
            return self._page_of(rows, fields)
        number = int(match.group(2))
        if match.group(3) == "commits":
            rows = [
                {
                    "sha": k.revision,
                    "commit": {
                        "message": k.message,
                        "committer": {"date": _iso(k.committed_at) if k.committed_at else ""},
                    },
                    "author": self._rest_user(k.authors[0]) if k.authors else None,
                }
                for k in self.commits.get(number, [])
            ]
        else:
            rows = [
                {
                    "id": int(r.id),
                    "user": self._rest_user(r.author),
                    "state": r.state.value.upper(),
                    "body": r.body,
                    "submitted_at": _iso(r.submitted_at) if r.submitted_at else None,
                    "commit_id": r.revision,
                }
                for r in self.reviews.get(number, [])
            ]
        return self._page_of(rows, fields)

    def _rest_pr(self, cr: ChangeRequest) -> dict[str, object]:
        return {
            "number": cr.number,
            "title": cr.title,
            "body": cr.body,
            "html_url": cr.url,
            "state": "closed" if cr.state is ChangeRequestState.MERGED else cr.state.value,
            "merged_at": _iso(cr.created_at) if cr.state is ChangeRequestState.MERGED else None,
            "draft": cr.is_draft,
            "user": self._rest_user(cr.author),
            "head": {"ref": cr.head_reference, "sha": cr.head_revision, "repo": {"full_name": self.repository.value}},
            "base": {"ref": cr.base_reference, "repo": {"full_name": self.repository.value}},
            "created_at": _iso(cr.created_at),
        }

    def _approve(self, path: str, fields: dict[str, str]) -> RunResult:
        match = re.search(r"/pulls/(\d+)/reviews$", path)
        if match is None or self._find(int(match.group(1))) is None:
            return RunResult(1, "", "HTTP 404: Not Found")
        self.add_review(
            self.repository,
            int(match.group(1)),
            author=self.user,
            state=ReviewState.APPROVED,
            revision=fields.get("commit_id"),
        )
        return RunResult(0, "{}", "")

    @staticmethod
    def _data(data: object) -> RunResult:
        return RunResult(0, json.dumps({"data": data}), "")

    def _graphql(self, fields: dict[str, str]) -> RunResult:
        query = fields["query"]
        number = int(fields.get("number", "0"))
        if "markFileAsViewed" in query:
            self.viewed.setdefault(int(fields["pullRequestId"].removeprefix("PR_")), {})[fields["path"]] = True
            return self._data({"markFileAsViewed": {"clientMutationId": None}})
        if "addPullRequestReviewThreadReply" in query:
            return self._reply(fields["threadId"], fields["body"])
        if self._find(number) is None:
            return self._data({"repository": {"pullRequest": None}})
        if "viewerViewedState" in query:
            rows = [
                {"path": p, "viewerViewedState": "VIEWED" if v else "UNVIEWED"}
                for p, v in self.viewed.get(number, {}).items()
            ]
            return self._graphql_page(rows, fields, "files")
        if "reviewThreads" in query:
            return self._graphql_page(
                [self._thread_node(t) for t in self.threads.get(number, [])], fields, "reviewThreads"
            )
        return self._data({"repository": {"pullRequest": {"id": f"PR_{number}"}}})

    def _graphql_page(self, rows: list[Any], fields: dict[str, str], name: str) -> RunResult:
        start = int(fields.get("cursor", "0"))
        end = start + 100
        info = {"hasNextPage": end < len(rows), "endCursor": str(end)}
        return self._data({"repository": {"pullRequest": {name: {"nodes": rows[start:end], "pageInfo": info}}}})

    @staticmethod
    def _comment_node(comment: Comment) -> dict[str, object]:
        return {
            "id": f"C_{comment.id}",
            "databaseId": int(comment.id) if comment.id.isdecimal() else None,
            "isMinimized": comment.is_hidden,
            "body": comment.body,
            "createdAt": _iso(comment.created_at),
            "url": comment.url,
            "diffHunk": comment.diff_hunk,
            "author": {"login": comment.author.login, "__typename": "User"} if comment.author else None,
        }

    def _thread_node(self, thread: ReviewThread) -> dict[str, object]:
        anchor = thread.anchor
        return {
            "id": thread.id,
            "isResolved": thread.is_resolved,
            "path": anchor.path if anchor else None,
            "line": anchor.line if anchor else None,
            "originalLine": None,
            "diffSide": anchor.side if anchor else None,
            "comments": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [self._comment_node(c) for c in thread.comments],
            },
        }

    def _reply(self, thread_id: str, body: str) -> RunResult:
        for threads in self.threads.values():
            for index, thread in enumerate(threads):
                if thread.id == thread_id:
                    comment = Comment(self._next_id(), User(self.user), body, self._tick(), None)
                    threads[index] = replace(thread, comments=(*thread.comments, comment))
                    return self._data({"addPullRequestReviewThreadReply": {"comment": self._comment_node(comment)}})
        return RunResult(0, json.dumps({"errors": [{"message": "Could not resolve to a node"}]}), "")

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
        # A paged read retries a failed page. The retries need no pause here.
        limits = replace(reference.DECLARATION.limits, page_retry_delay_seconds=0.0)
        monkeypatch.setattr(reference, "DECLARATION", replace(reference.DECLARATION, limits=limits))

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
