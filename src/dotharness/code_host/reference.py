"""Reference code host, registered under the name ``reference``. It runs the ``gh`` CLI.

This module owns every host-specific rule of the review flow: the ``gh`` command lines, the remote-URL rules, the
size limits and the fallback to a top-level comment. It imports nothing from ``vibe_heal``: the caller renders the
review text.
"""

import asyncio
import json
import logging
import math
import os
import re
import shlex
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotharness.code_host.declarations import (
    AnchorRules,
    Capability,
    HostDeclaration,
    HostLimits,
    ListBounds,
    OperationTimeouts,
)
from dotharness.code_host.errors import (
    AuthenticationError,
    CodeHostError,
    CodeHostTimeoutError,
    NoChangeRequestError,
    UnsupportedCapabilityError,
)
from dotharness.code_host.factory import HostRegistration, TokenCmd
from dotharness.code_host.interfaces import CodeHost
from dotharness.code_host.models import (
    ChangeRequest,
    ChangeRequestState,
    Check,
    Comment,
    CommitInfo,
    Credential,
    InlineComment,
    Job,
    RepositoryRef,
    Review,
    ReviewPostResult,
    ReviewThread,
    User,
    Workflow,
    WorkItem,
    WorkItemState,
)
from dotharness.code_host.version import CODE_HOST_INTERFACE_VERSION
from dotharness.gh import gh_argv
from dotharness.subprocess_runner import RunResult, run

logger = logging.getLogger(__name__)

_MAX_INLINE_COMMENTS = 50
_MAX_REVIEW_BODY_CHARS = 60_000  # The host limit is about 65536. Keep headroom.
_POST_TIMEOUT_SECONDS = 60
_TRUNCATION_NOTICE = "\n\n_...review body truncated to fit GitHub's size limit._"
_NOT_INSTALLED = "gh CLI is not installed or not in PATH. Install it from https://cli.github.com/"
_PR_VIEW = "pr view"
_FIND_BY_BRANCH_OP = "ChangeRequests.find_by_branch"
_PR_FIELDS = "number,baseRefName,title,body,url,state,isDraft,headRefName,headRefOid,isCrossRepository,createdAt,author"


# --------------------------------------------------------------------------------------------------------------------
# Command running
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Outcome:
    success: bool
    stdout: str
    stderr: str
    returncode: int = 0


@asynccontextmanager
async def _deadline(cmd: list[str], seconds: float, *, write: bool = False) -> AsyncGenerator[None]:
    """Bound the enclosed block by a deadline. Expiry raises CodeHostTimeoutError (``write`` says if it was a write)."""
    try:
        async with asyncio.timeout(seconds):
            yield
    except TimeoutError:
        msg = f"`{cmd[0]} {cmd[1] if len(cmd) > 1 else ''}`".strip() + f" timed out after {seconds:g}s"
        raise CodeHostTimeoutError(msg, write=write) from None


async def _run_gh(
    cmd: list[str],
    seconds: float,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    write: bool = False,
) -> _Outcome:
    """Run a command under a caller-chosen deadline. The deadline kills the child process group on expiry.

    ``cwd`` defaults to the process directory. ``env`` replaces the child environment (``None`` inherits it).
    """
    async with _deadline(cmd, seconds, write=write):
        result = await run(cmd, cwd=cwd or Path.cwd(), env=env, timeout=math.inf)
    if result.not_found:
        raise FileNotFoundError(result.stderr)
    return _Outcome(result.returncode == 0, result.stdout, result.stderr, result.returncode)


async def _require_gh() -> None:
    """Raise OSError when ``gh --version`` fails. This is the only host-specific string vibe-heal prints."""
    try:
        outcome = await _run_gh(gh_argv("--version"), 10)
    except OSError as exc:
        raise OSError(_NOT_INSTALLED) from exc
    if not outcome.success:
        raise OSError(_NOT_INSTALLED)


class _RejectedError(CodeHostError):
    """The host rejected the payload (HTTP 422), so nothing was created and a fallback is safe."""


_AUTH_MARKERS = ("http 401", "http 403", "bad credentials", "gh auth login")


async def _post_json(cmd: list[str], payload: dict[str, Any]) -> None:
    """Post JSON to a ``gh api`` endpoint on stdin (no token in the arguments).

    Raises:
        CodeHostTimeoutError: On timeout. The write may have succeeded.
        FileNotFoundError: When the program is not found.
        AuthenticationError: On HTTP 401/403 or a ``gh auth login`` hint.
        _RejectedError: On a validation rejection (HTTP 422).
        CodeHostError: On any other non-zero exit (5xx, dropped connection). The write may have succeeded.
    """
    result: RunResult = await run(cmd, timeout=_POST_TIMEOUT_SECONDS, stdin=json.dumps(payload))
    if result.timed_out:
        msg = f"gh API call timed out after {_POST_TIMEOUT_SECONDS}s"
        raise CodeHostTimeoutError(msg)
    if result.not_found:
        raise FileNotFoundError(result.stderr)
    if result.returncode != 0:
        parts = [p for p in (result.stderr.strip(), result.stdout.strip()) if p]
        error_msg = " — ".join(parts) if parts else "Unknown error"
        lowered = error_msg.lower()
        if any(m in lowered for m in _AUTH_MARKERS):
            raise AuthenticationError(error_msg)
        if "http 422" in lowered or "unprocessable" in lowered or "validation" in lowered:
            raise _RejectedError(error_msg)
        raise CodeHostError(error_msg)


# --------------------------------------------------------------------------------------------------------------------
# Remote URL rules
# --------------------------------------------------------------------------------------------------------------------


def _parse_remote_url(remote_url: str) -> str | None:
    """Parse ``owner/repo`` from a git remote URL.

    SSH ``git@host:owner/repo.git`` is accepted for any host. HTTPS is accepted only when the host part contains
    ``github``, so an owner/repo pair is never resolved against the wrong host.
    """
    # The repo name may contain dots (for example org/my.repo), so only a trailing .git suffix is removed.
    if remote_url.startswith("git@"):
        match = re.search(r":([^/]+)/(.+?)(?:\.git)?$", remote_url)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
        return None

    parsed = urlparse(remote_url)
    if "github" not in parsed.netloc:
        return None
    path = parsed.path.strip("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) == 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return None


# --------------------------------------------------------------------------------------------------------------------
# Size limits
# --------------------------------------------------------------------------------------------------------------------


def _truncate_body(body: str) -> str:
    if len(body) <= _MAX_REVIEW_BODY_CHARS:
        return body
    return body[: _MAX_REVIEW_BODY_CHARS - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE


# --------------------------------------------------------------------------------------------------------------------
# Host context: working directory, credential and the ``gh`` environment
# --------------------------------------------------------------------------------------------------------------------

_TOKEN_CMD_TIMEOUT_SECONDS = 30
_STDERR_TAIL = 300


class TokenCommandError(AuthenticationError):
    """The token command failed. The data fields let a consumer build its own text. No message holds argv or stderr.

    ``exit_code`` is the command's exit status, or ``None`` when it did not run to an exit. ``detail`` names the cause
    when it did not run (an exception class name).
    """

    def __init__(self, message: str, *, exit_code: int | None = None, detail: str | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.detail = detail


def _seconds(operation: str) -> float:
    timeouts = DECLARATION.limits.timeouts
    return timeouts.per_operation.get(operation, timeouts.default_seconds)


def _bound(operation: str) -> int | None:
    bounds = DECLARATION.limits.list_bounds
    return bounds.per_operation.get(operation, bounds.default)


class _Gh:
    """Runs ``gh`` for one host: in the working directory, with ``GH_TOKEN`` when a token command is set."""

    def __init__(self, token_cmd: TokenCmd, working_directory: Path | None) -> None:
        self.cwd = working_directory
        self._token_cmd = token_cmd
        self._credential: Credential | None = None

    def __repr__(self) -> str:
        return f"_Gh(cwd={self.cwd!r})"

    async def credential(self) -> Credential:
        """Run the token command once (``shlex.split``, no shell, 30 s) and keep the token for the process."""
        if self._credential is not None:
            return self._credential
        if not self._token_cmd:
            msg = "no token command is configured"
            raise AuthenticationError(msg)
        try:
            argv = shlex.split(self._token_cmd) if isinstance(self._token_cmd, str) else list(self._token_cmd)
        except ValueError:
            argv = []
        if not argv:
            msg = "token command failed to run (ValueError)"
            raise TokenCommandError(msg, detail="ValueError") from None
        result = await run(argv, timeout=_TOKEN_CMD_TIMEOUT_SECONDS)
        # Never put argv or stderr in a message: the command may hold a secret.
        if result.timed_out or result.not_found:
            detail = "TimeoutExpired" if result.timed_out else "OSError"
            msg = f"token command failed to run ({detail})"
            raise TokenCommandError(msg, detail=detail)
        if result.returncode != 0 or not result.stdout.strip():
            msg = f"token command failed (exit {result.returncode}) or printed no token"
            raise TokenCommandError(msg, exit_code=result.returncode)
        self._credential = Credential(token=result.stdout.strip())
        return self._credential

    async def _env(self) -> dict[str, str] | None:
        """The child environment: the inherited one plus ``GH_TOKEN`` only. ``None`` when no token command is set."""
        if not self._token_cmd:
            return None
        return {**os.environ, "GH_TOKEN": (await self.credential()).token}

    async def run(
        self, args: Sequence[str], operation: str, *, write: bool = False, seconds: float | None = None
    ) -> _Outcome:
        """Run ``gh <args>`` under the declared timeout of ``operation`` (``Interface.method``)."""
        return await _run_gh(
            gh_argv(*args),
            _seconds(operation) if seconds is None else seconds,
            cwd=self.cwd,
            env=await self._env(),
            write=write,
        )

    def _failure(self, args: Sequence[str], outcome: _Outcome) -> CodeHostError:
        """Error for a non-zero exit: exit code and the stderr tail. No command line, no token."""
        tail = outcome.stderr.strip()[-_STDERR_TAIL:]
        if self._credential is not None:
            tail = tail.replace(self._credential.token, "<token>")
        msg = f"gh {' '.join(args[:2])} failed (exit {outcome.returncode}): {tail}"
        if any(marker in tail.lower() for marker in _AUTH_MARKERS):
            return AuthenticationError(msg)
        return CodeHostError(msg)

    async def ok(self, args: Sequence[str], operation: str, *, write: bool = False) -> _Outcome:
        """Run and raise on a non-zero exit."""
        outcome = await self.run(args, operation, write=write)
        if not outcome.success:
            raise self._failure(args, outcome)
        return outcome

    async def json(self, args: Sequence[str], operation: str) -> Any:
        """Run a read and parse its JSON output. Raises CodeHostError on a non-zero exit or invalid JSON."""
        outcome = await self.ok(args, operation)
        try:
            return json.loads(outcome.stdout or "null")
        except ValueError:
            msg = f"gh {' '.join(args[:2])} returned invalid JSON"
            raise CodeHostError(msg) from None


# --------------------------------------------------------------------------------------------------------------------
# Capability objects
# --------------------------------------------------------------------------------------------------------------------


def _unsupported(capability: Capability, operation: str) -> UnsupportedCapabilityError:
    msg = f"{capability.value}.{operation} is not supported by the reference code host"
    return UnsupportedCapabilityError(msg, capability=capability.value)


class _Session:
    def __init__(self, gh: _Gh) -> None:
        self._gh = gh

    async def available(self) -> bool:
        """True when ``gh --version`` succeeds within 10 s. Never raises."""
        try:
            return (await _run_gh(gh_argv("--version"), _seconds("Session.available"), cwd=self._gh.cwd)).success
        except Exception:
            return False

    async def credential(self) -> Credential:
        """The token from the configured token command. Raises AuthenticationError (no command, failure)."""
        return await self._gh.credential()

    async def current_user(self) -> str:
        raise _unsupported(Capability.SESSION, "current_user")

    async def agent_environment_names(self) -> Sequence[str]:
        raise _unsupported(Capability.SESSION, "agent_environment_names")

    async def agent_environment(self, base: Mapping[str, str] | None = None) -> Mapping[str, str]:
        raise _unsupported(Capability.SESSION, "agent_environment")


class _Repository:
    def __init__(self, gh: _Gh) -> None:
        self._gh = gh

    async def current(self) -> RepositoryRef:
        """Owner and repository of the clone, from ``gh`` and then from the ``origin`` remote."""
        outcome = await self._gh.run(
            ("repo", "view", "--json", "owner,name", "--jq", '"\\(.owner.login)/\\(.name)"'),
            "Repository.current",
            seconds=15,
        )
        if outcome.success and outcome.stdout.strip():
            return RepositoryRef(outcome.stdout.strip())

        try:
            git_outcome = await _run_gh(["git", "remote", "get-url", "origin"], 10, cwd=self._gh.cwd)
        except (OSError, CodeHostError):
            git_outcome = None
        if git_outcome is not None and git_outcome.success:
            owner_repo = _parse_remote_url(git_outcome.stdout.strip())
            if owner_repo:
                return RepositoryRef(owner_repo)

        msg = "Could not detect GitHub owner/repo"
        raise CodeHostError(msg)

    async def resolve(self, identity: str) -> RepositoryRef:
        raise _unsupported(Capability.REPOSITORY, "resolve")

    async def default_branch(self, repo: RepositoryRef) -> str:
        """Name of the default branch (``gh repo view --json defaultBranchRef``). Raises CodeHostError."""
        data = await self._gh.json(("repo", "view", "--json", "defaultBranchRef"), "Repository.default_branch")
        ref = data.get("defaultBranchRef") if isinstance(data, dict) else None
        name = ref.get("name") if isinstance(ref, dict) else None
        if not isinstance(name, str) or not name:
            msg = "gh repo view returned no default branch"
            raise CodeHostError(msg)
        return name

    async def display_name(self, repo: RepositoryRef) -> str:
        raise _unsupported(Capability.REPOSITORY, "display_name")


def _parse_time(value: object) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.fromtimestamp(0, tz=UTC)


def _str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


_PR_URL = re.compile(r"/pull/(\d+)")


def _int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _summary(
    repo: RepositoryRef, data: Mapping[str, Any], *, state: ChangeRequestState | None = None, head: str = ""
) -> ChangeRequest:
    """A ``ChangeRequest`` from the few fields sisyphus reads. Unread fields take the empty defaults (decision 4)."""
    return ChangeRequest(
        repository=repo,
        number=_int(data, "number"),
        title=_str(data, "title"),
        body=_str(data, "body"),
        url=_str(data, "url"),
        state=_state(_str(data, "state")) if state is None else state,
        is_draft=False,
        author=None,
        head_reference=head,
        head_revision=_str(data, "headRefOid"),
        base_reference="",
        is_cross_repository=False,
        created_at=_parse_time(None),
    )


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        msg = f"gh {what} returned an unexpected response"
        raise CodeHostError(msg)
    return value


class _ChangeRequests:
    def __init__(self, gh: _Gh) -> None:
        self._gh = gh

    async def for_current_branch(self, repo: RepositoryRef) -> ChangeRequest:
        """The open pull request of the checked-out branch (``gh pr view``, no branch argument)."""
        await _require_gh()
        outcome = await self._gh.run(("pr", "view", "--json", _PR_FIELDS), "ChangeRequests.for_current_branch")
        if not outcome.success:
            stderr = outcome.stderr.strip()
            if "no pull requests found" in stderr.lower():
                msg = "No open pull request found for the current branch"
                raise NoChangeRequestError(msg)
            msg = f"Failed to detect PR: {stderr or 'not authenticated'}"
            raise CodeHostError(msg)

        try:
            parsed = json.loads(outcome.stdout)
        except json.JSONDecodeError:
            parsed = None
        number = parsed.get("number") if isinstance(parsed, dict) else None
        if not isinstance(parsed, dict) or not isinstance(number, int) or isinstance(number, bool):
            msg = "Failed to detect PR: unexpected response from gh"
            raise CodeHostError(msg)
        data: dict[str, Any] = parsed

        author = data.get("author")
        return ChangeRequest(
            repository=repo,
            number=number,
            title=_str(data, "title"),
            body=_str(data, "body"),
            url=_str(data, "url"),
            state=_state(_str(data, "state")),
            is_draft=bool(data.get("isDraft", False)),
            author=_user(author),
            head_reference=_str(data, "headRefName"),
            head_revision=_str(data, "headRefOid"),
            base_reference=_str(data, "baseRefName"),
            is_cross_repository=bool(data.get("isCrossRepository", False)),
            created_at=_parse_time(data.get("createdAt")),
        )

    async def get(self, repo: RepositoryRef, number: int) -> ChangeRequest:
        """Number, state and head revision (``gh pr view N --json number,state,headRefOid``)."""
        data = await self._gh.json(
            ("pr", "view", str(number), "--json", "number,state,headRefOid"), "ChangeRequests.get"
        )
        return _summary(repo, _object(data, _PR_VIEW))

    async def state(self, repo: RepositoryRef, number: int) -> ChangeRequestState:
        """Lifecycle state only (``gh pr view N --json state``)."""
        data = await self._gh.json(("pr", "view", str(number), "--json", "state"), "ChangeRequests.state")
        value = data.get("state") if isinstance(data, dict) else None
        if not isinstance(value, str) or not value:
            msg = "gh pr view returned no state"
            raise CodeHostError(msg)
        return _state(value)

    async def find_by_branch(
        self, repo: RepositoryRef, branch: str, *, state: ChangeRequestState | None = ChangeRequestState.OPEN
    ) -> Sequence[ChangeRequest]:
        """Change requests whose head is ``branch``. ``state=None`` means any state: the most recent one, if any.

        ``gh pr view <branch>`` picks the most recent match. It exits non-zero with "no pull requests found" when there
        is none, which is an empty result here.
        """
        if state is None:
            args = ("pr", "view", branch, "--json", "number,state,headRefOid")
            outcome = await self._gh.run(args, _FIND_BY_BRANCH_OP)
            if not outcome.success:
                if "no pull requests found" in outcome.stderr.lower():
                    return []
                raise self._gh._failure(args, outcome)
            try:
                data = json.loads(outcome.stdout or "null")
            except ValueError:
                msg = "gh pr view returned invalid JSON"
                raise CodeHostError(msg) from None
            return [_summary(repo, _object(data, _PR_VIEW), head=branch)]
        rows = await self._gh.json(
            ("pr", "list", "--head", branch, "--state", state.value, "--json", "number,url"),
            _FIND_BY_BRANCH_OP,
        )
        return [_summary(repo, row, state=state, head=branch) for row in rows or [] if isinstance(row, dict)]

    async def list(
        self,
        repo: RepositoryRef,
        *,
        state: ChangeRequestState = ChangeRequestState.OPEN,
        author: str | None = None,
        assignee: str | None = None,
        requested_reviewer: str | None = None,
    ) -> Sequence[ChangeRequest]:
        raise _unsupported(Capability.CHANGE_REQUESTS, "list")

    async def find_by_commit(self, repo: RepositoryRef, revision: str) -> Sequence[ChangeRequest]:
        raise _unsupported(Capability.CHANGE_REQUESTS, "find_by_commit")

    async def commits(self, repo: RepositoryRef, number: int) -> Sequence[CommitInfo]:
        raise _unsupported(Capability.CHANGE_REQUESTS, "commits")

    async def reviews(self, repo: RepositoryRef, number: int) -> Sequence[Review]:
        raise _unsupported(Capability.CHANGE_REQUESTS, "reviews")

    async def create_draft(self, repo: RepositoryRef, *, head: str, base: str, title: str, body: str) -> ChangeRequest:
        """Create a draft (``gh pr create --draft``). The number comes from the printed URL; no URL is an error."""
        args = ("pr", "create", "--draft", "--head", head, "--base", base, "--title", title, "--body", body)
        outcome = await self._gh.run(args, "ChangeRequests.create_draft", write=True)
        match = _PR_URL.search(outcome.stdout)
        if not outcome.success or not match:
            raise self._gh._failure(
                args, _Outcome(False, outcome.stdout, outcome.stderr or outcome.stdout, outcome.returncode)
            )
        return ChangeRequest(
            repository=repo,
            number=int(match.group(1)),
            title=title,
            body=body,
            url=outcome.stdout.strip().splitlines()[-1],
            state=ChangeRequestState.OPEN,
            is_draft=True,
            author=None,
            head_reference=head,
            head_revision="",
            base_reference=base,
            is_cross_repository=False,
            created_at=_parse_time(None),
        )

    async def squash_merge(self, repo: RepositoryRef, number: int, *, expected_head: str) -> None:
        raise _unsupported(Capability.CHANGE_REQUESTS, "squash_merge")

    async def request_reviewer(self, repo: RepositoryRef, number: int, login: str) -> None:
        raise _unsupported(Capability.CHANGE_REQUESTS, "request_reviewer")

    async def approve(self, repo: RepositoryRef, number: int, *, head_revision: str) -> None:
        """Unsupported. ``head_revision`` is kept because the ``ChangeRequests`` protocol requires it."""
        raise _unsupported(Capability.CHANGE_REQUESTS, "approve")

    async def viewed_files(self, repo: RepositoryRef, number: int) -> Mapping[str, bool]:
        raise _unsupported(Capability.CHANGE_REQUESTS, "viewed_files")

    async def mark_viewed(self, repo: RepositoryRef, number: int, path: str) -> None:
        raise _unsupported(Capability.CHANGE_REQUESTS, "mark_viewed")


def _state(value: str) -> ChangeRequestState:
    try:
        return ChangeRequestState(value.lower())
    except ValueError:
        return ChangeRequestState.OPEN


def _user(value: object) -> User | None:
    if isinstance(value, dict) and isinstance(value.get("login"), str):
        return User(login=value["login"], is_bot=bool(value.get("is_bot", False)))
    return None


def _review_endpoint(repo: RepositoryRef, number: int) -> list[str]:
    return gh_argv("api", "--method", "POST", f"/repos/{repo.value}/pulls/{number}/reviews", "--input", "-")


class _Discussion:
    async def post_review(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        body: str,
        inline_comments: Sequence[InlineComment],
        head_revision: str | None = None,
        fallback_body: str | None = None,
    ) -> ReviewPostResult:
        """Post the review. On a rejection post one top-level fallback comment.

        Only a validation rejection (HTTP 422) is followed by a fallback. A timeout, 5xx, dropped connection,
        authentication failure or unsupported capability is re-raised, because the write may have succeeded.

        ``head_revision`` is accepted but unused: the ``Discussion`` protocol requires it, and the reference host
        posts the review against the pull request's current head.
        """
        await _require_gh()
        cmd = _review_endpoint(repo, number)
        payload = {
            "event": "COMMENT",
            "body": _truncate_body(body),
            "comments": [
                {"path": c.anchor.path, "line": c.anchor.line, "side": c.anchor.side, "body": c.body}
                for c in inline_comments[:_MAX_INLINE_COMMENTS]
            ],
        }
        try:
            await _post_json(cmd, payload)
        except _RejectedError as inline_err:  # Only a rejection is safe to retry. Anything else may have posted.
            reason = str(inline_err)
            logger.warning("Inline review failed (%s) — falling back to top-level comment", reason)
            fallback = _truncate_body(body if fallback_body is None else fallback_body)
            try:
                await _post_json(cmd, {"event": "COMMENT", "body": fallback, "comments": []})
            except CodeHostTimeoutError:
                raise  # The fallback may have posted. Keep the type that says do not retry.
            except CodeHostError as e:
                msg = f"Review posting failed: {e}"
                raise CodeHostError(msg) from e
            return ReviewPostResult(fell_back=True, review=None, comment=None, reason=reason)
        return ReviewPostResult(fell_back=False, review=None, comment=None)

    async def comments(self, repo: RepositoryRef, number: int) -> Sequence[Comment]:
        raise _unsupported(Capability.DISCUSSION, "comments")

    async def review_threads(self, repo: RepositoryRef, number: int) -> Sequence[ReviewThread]:
        raise _unsupported(Capability.DISCUSSION, "review_threads")

    async def post_comment(self, repo: RepositoryRef, number: int, body: str) -> Comment:
        raise _unsupported(Capability.DISCUSSION, "post_comment")

    async def reply(self, repo: RepositoryRef, number: int, thread_id: str, body: str) -> Comment:
        raise _unsupported(Capability.DISCUSSION, "reply")


class _AbsentChecks:
    """Checks are not declared. Every call raises and runs no ``gh``."""

    async def list_checks(self, repo: RepositoryRef, number: int, *, required_only: bool = False) -> Sequence[Check]:
        raise _unsupported(Capability.CHECKS, "list_checks")

    async def list_workflows(self, repo: RepositoryRef) -> Sequence[Workflow]:
        raise _unsupported(Capability.CHECKS, "list_workflows")

    async def failed_log(self, repo: RepositoryRef, check: Check) -> str:
        raise _unsupported(Capability.CHECKS, "failed_log")

    async def get_job(self, repo: RepositoryRef, job_id: str) -> Job:
        raise _unsupported(Capability.CHECKS, "get_job")


class _WorkItems:
    def __init__(self, gh: _Gh) -> None:
        self._gh = gh

    async def get(self, repo: RepositoryRef, number: int) -> WorkItem | None:
        raise _unsupported(Capability.WORK_ITEMS, "get")

    async def list_open(
        self, repo: RepositoryRef, *, assignee: str | None = None, label: str | None = None
    ) -> Sequence[WorkItem]:
        """Open issues in ``gh`` order, bounded by the declared list bound."""
        args = ["issue", "list", "--state", "open"]
        if assignee is not None:
            args += ["--assignee", assignee]
        if label is not None:
            args += ["--label", label]
        bound = _bound("WorkItems.list_open")
        args += ["--limit", str(bound), "--json", "number,title,createdAt,assignees,labels"]
        rows = await self._gh.json(args, "WorkItems.list_open")
        return [_work_item(repo, row) for row in rows or [] if isinstance(row, dict)]

    async def remove_label(self, repo: RepositoryRef, number: int, label: str) -> None:
        """Remove ``label`` (``gh issue edit N --remove-label L``). A non-zero exit raises, a timeout is a write timeout.

        A label the issue does not carry exits 0 (no-op). A label that does not exist in the repository exits non-zero.
        """
        await self._gh.ok(("issue", "edit", str(number), "--remove-label", label), "WorkItems.remove_label", write=True)

    async def closing_change_requests(self, repo: RepositoryRef, number: int) -> Sequence[ChangeRequest]:
        """Change requests GitHub links as closing the issue, any state, each resolved by ``gh pr view``."""
        view = await self._gh.json(
            ("issue", "view", str(number), "--json", "closedByPullRequestsReferences"),
            "WorkItems.closing_change_requests",
        )
        found: list[ChangeRequest] = []
        for ref in (
            (view or {}).get("closedByPullRequestsReferences", []) if isinstance(view, dict) or view is None else []
        ):
            pr = await self._gh.json(
                ("pr", "view", str(_int(ref, "number")), "--json", "number,state"), "WorkItems.closing_change_requests"
            )
            found.append(_summary(repo, _object(pr, _PR_VIEW)))
        return found

    async def open_change_request_bodies(self, repo: RepositoryRef) -> Sequence[ChangeRequest]:
        """Open change requests with their bodies, bounded by the declared list bound."""
        bound = _bound("WorkItems.open_change_request_bodies")
        rows = await self._gh.json(
            ("pr", "list", "--state", "open", "--limit", str(bound), "--json", "body"),
            "WorkItems.open_change_request_bodies",
        )
        return [_summary(repo, row, state=ChangeRequestState.OPEN) for row in rows or [] if isinstance(row, dict)]

    async def linked_work_items(self, repo: RepositoryRef, number: int) -> Sequence[WorkItem]:
        raise _unsupported(Capability.WORK_ITEMS, "linked_work_items")


def _work_item(repo: RepositoryRef, row: Mapping[str, Any]) -> WorkItem:
    assignees = row.get("assignees")
    labels = row.get("labels")
    return WorkItem(
        repository=repo,
        number=_int(row, "number"),
        title=_str(row, "title"),
        body="",
        labels=tuple(_str(lbl, "name") for lbl in labels if isinstance(lbl, dict)) if isinstance(labels, list) else (),
        assignees=tuple(u for u in (_user(a) for a in assignees) if u is not None)
        if isinstance(assignees, list)
        else (),
        created_at=_parse_time(row.get("createdAt")),
        state=WorkItemState.OPEN,
    )


# --------------------------------------------------------------------------------------------------------------------
# Declaration and registration
# --------------------------------------------------------------------------------------------------------------------

DECLARATION = HostDeclaration(
    capabilities=frozenset({
        Capability.SESSION,
        Capability.REPOSITORY,
        Capability.CHANGE_REQUESTS,
        Capability.DISCUSSION,
        Capability.WORK_ITEMS,
    }),
    limits=HostLimits(
        max_inline_comments_per_review=_MAX_INLINE_COMMENTS,
        max_review_body_chars=_MAX_REVIEW_BODY_CHARS,
        max_change_request_body_chars=60_000,
        timeouts=OperationTimeouts(
            default_seconds=30,
            per_operation={
                "ChangeRequests.state": 10.0,
                "Session.available": 10,
                _FIND_BY_BRANCH_OP: 30,
                "WorkItems.remove_label": 10.0,
                "Discussion.post_review": _POST_TIMEOUT_SECONDS,
            },
        ),
        list_bounds=ListBounds(
            default=100,
            per_operation={
                "ChangeRequests.list": 1000,
                "WorkItems.list_open": 100,
                "WorkItems.open_change_request_bodies": 200,
                "Discussion.comments": None,
                "Discussion.review_threads": None,
                "ChangeRequests.reviews": None,
                "ChangeRequests.commits": None,
                "ChangeRequests.viewed_files": None,
                "Checks.list_workflows": 100,
            },
        ),
    ),
    anchors=AnchorRules(
        inline_supported=True,
        sides=frozenset({"RIGHT"}),
        requires_line_in_diff=True,
        supports_ranges=False,
        fallback="top_level_comment",
    ),
)


@dataclass(frozen=True)
class ReferenceOptions:
    """Options of the reference host. There are none yet."""


def _create(options: ReferenceOptions, token_cmd: TokenCmd, working_directory: Path | None = None) -> CodeHost:
    """Build the reference ``CodeHost``. Runs no command: the token command runs at ``Session.credential()``.

    ``working_directory`` is the directory of every ``gh`` call (the process directory when ``None``). With a token
    command, ``gh`` calls get ``GH_TOKEN`` in their child environment (never ``GITHUB_TOKEN``). Without one, ``gh``
    owns its own authentication.
    """
    gh = _Gh(token_cmd, working_directory)
    return CodeHost(
        session=_Session(gh),
        repository=_Repository(gh),
        change_requests=_ChangeRequests(gh),
        discussion=_Discussion(),
        checks=_AbsentChecks(),
        work_items=_WorkItems(gh),
        declaration=DECLARATION,
    )


REGISTRATION = HostRegistration(
    interface_version=CODE_HOST_INTERFACE_VERSION,
    options_model=ReferenceOptions,
    factory=_create,
)
