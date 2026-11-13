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
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from dotharness.code_host.declarations import (
    Anchor,
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
    ConfigurationError,
    NoChangeRequestError,
    TokenCommandError,
)
from dotharness.code_host.factory import HostRegistration, TokenCmd
from dotharness.code_host.interfaces import CodeHost
from dotharness.code_host.models import (
    ChangeRequest,
    ChangeRequestState,
    Check,
    CheckStatus,
    Comment,
    CommitInfo,
    Credential,
    InlineComment,
    Job,
    MergeableState,
    RepositoryRef,
    Review,
    ReviewDecision,
    ReviewPostResult,
    ReviewState,
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
_LIST_OPEN_OP = "WorkItems.list_open"
_OPEN_CR_BODIES_OP = "WorkItems.open_change_request_bodies"
_PR_FIELDS = "number,baseRefName,title,body,url,state,isDraft,headRefName,headRefOid,isCrossRepository,createdAt,author"
_PR_FULL_FIELDS = f"{_PR_FIELDS},mergeable,autoMergeRequest,reviewDecision,reviewRequests,additions,deletions"
_PAGE_SIZE = 100
_PAGE_ATTEMPTS = 3
_GH_API_UNEXPECTED = "gh api returned an unexpected response"
_PAGE_RETRY_DELAY = 2.0
_ISSUE_NOT_FOUND_RE = re.compile(r"could not resolve to an issue", re.IGNORECASE)
_ISSUE_COMMENT_ID = re.compile(r"issuecomment-(\d+)")
_ISSUE_FIELDS = "number,title,body,labels,assignees,createdAt,state"


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

_STDERR_TAIL = 300


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
        """Run the token command once (``shlex.split``, no shell, declared timeout) and keep the token for the process."""
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
        seconds = _seconds("Session.credential")
        result = await run(argv, timeout=seconds)
        # Never put argv or stderr in a message: the command may hold a secret.
        if result.timed_out:
            msg = f"token command timed out after {seconds:g}s"
            raise CodeHostTimeoutError(msg, write=False)
        if result.not_found:
            msg = "token command failed to run (OSError)"
            raise TokenCommandError(msg, detail="OSError")
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

    def failure(self, args: Sequence[str], outcome: _Outcome) -> CodeHostError:
        """Error for a non-zero exit: exit code and the stderr tail. No command line, no token."""
        tail = outcome.stderr.strip()
        if self._credential is not None:
            # Redact before truncating: a cut through the token would leave a suffix that no longer matches.
            tail = tail.replace(self._credential.token, "<token>")
        tail = tail[-_STDERR_TAIL:]
        msg = f"gh {' '.join(args[:2])} failed (exit {outcome.returncode}): {tail}"
        if any(marker in tail.lower() for marker in _AUTH_MARKERS):
            return AuthenticationError(msg)
        return CodeHostError(msg)

    async def ok(self, args: Sequence[str], operation: str, *, write: bool = False) -> _Outcome:
        """Run and raise on a non-zero exit."""
        outcome = await self.run(args, operation, write=write)
        if not outcome.success:
            raise self.failure(args, outcome)
        return outcome

    async def json(self, args: Sequence[str], operation: str) -> Any:
        """Run a read and parse its JSON output. Raises CodeHostError on a non-zero exit or invalid JSON."""
        outcome = await self.ok(args, operation)
        try:
            return json.loads(outcome.stdout or "null")
        except ValueError:
            msg = f"gh {' '.join(args[:2])} returned invalid JSON"
            raise CodeHostError(msg) from None

    async def _page(self, args: Sequence[str], operation: str) -> list[Any]:
        """One page of a REST list. A failed ``gh`` call is retried, as declared. A timeout or an auth error is not."""
        limits = DECLARATION.limits
        for attempt in range(1, limits.page_attempts + 1):
            outcome = await self.run(args, operation)
            if outcome.success:
                try:
                    rows = json.loads(outcome.stdout or "null")
                except ValueError:
                    msg = "gh api returned invalid JSON"
                    raise CodeHostError(msg) from None
                if not isinstance(rows, list):
                    msg = _GH_API_UNEXPECTED
                    raise CodeHostError(msg)
                return rows
            error = self.failure(args, outcome)
            if isinstance(error, AuthenticationError) or attempt == limits.page_attempts:
                raise error
            logger.warning("%s (attempt %d/%d)", error, attempt, limits.page_attempts)
            await asyncio.sleep(limits.page_retry_delay_seconds * attempt)
        raise AssertionError  # pragma: no cover (page_attempts >= 1 returns or raises above)

    async def paged(self, path: str, operation: str) -> list[dict[str, Any]]:
        """Every item of a REST list endpoint. A declared bound that the result exceeds raises (never truncates)."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            args = ("api", "--method", "GET", path, "-F", f"per_page={_PAGE_SIZE}", "-F", f"page={page}")
            batch = await self._page(args, operation)
            items.extend(row for row in batch if isinstance(row, dict))
            if len(batch) < _PAGE_SIZE:
                break
            page += 1
        bound = _bound(operation)
        if bound is not None and len(items) > bound:
            msg = f"{operation} found more than the declared bound of {bound}"
            raise CodeHostError(msg)
        return items

    async def graphql(
        self, query: str, operation: str, variables: Mapping[str, str | int], *, write: bool = False
    ) -> dict[str, Any]:
        """Run a GraphQL document. Returns ``data``. An ``errors`` member or a missing ``data`` raises CodeHostError."""
        args = ["api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            args += ["-F" if isinstance(value, int) else "-f", f"{name}={value}"]
        outcome = await self.ok(args, operation, write=write)
        try:
            parsed = json.loads(outcome.stdout or "null")
        except ValueError:
            parsed = None
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(parsed, dict) or parsed.get("errors") or not isinstance(data, dict):
            msg = "gh api graphql returned errors or no data"
            raise CodeHostError(msg)
        return data


# --------------------------------------------------------------------------------------------------------------------
# Capability objects
# --------------------------------------------------------------------------------------------------------------------


_AGENT_ENV_NAMES = ("GITHUB_TOKEN", "GH_TOKEN")
_IDENTITY = re.compile(r"[^/\s]+/[^/\s]+")


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
        """Login of the acting user (``gh api user --jq .login``). An empty login raises AuthenticationError."""
        outcome = await self._gh.ok(("api", "user", "--jq", ".login"), "Session.current_user")
        login = outcome.stdout.strip()
        if not login:
            msg = "gh api user returned no login"
            raise AuthenticationError(msg)
        return login

    async def agent_environment_names(self) -> Sequence[str]:
        return _AGENT_ENV_NAMES

    async def agent_environment(self, base: Mapping[str, str] | None = None) -> Mapping[str, str]:
        """``base`` plus the token as ``GITHUB_TOKEN`` and ``GH_TOKEN``. The credential variables win over ``base``."""
        token = (await self._gh.credential()).token
        return {**(base or {}), **dict.fromkeys(_AGENT_ENV_NAMES, token)}


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
        """Identity is ``owner/repo``. Raises ConfigurationError for any other shape. Runs no command."""
        value = identity.strip()
        if not _IDENTITY.fullmatch(value):
            msg = "repository identity must be written owner/repo"
            raise ConfigurationError(msg)
        return RepositoryRef(value)

    async def default_branch(self, repo: RepositoryRef) -> str:
        """Name of the default branch (``gh repo view --json defaultBranchRef``). Raises CodeHostError."""
        data = await self._gh.json(
            ("repo", "view", repo.value, "--json", "defaultBranchRef"), "Repository.default_branch"
        )
        ref = data.get("defaultBranchRef") if isinstance(data, dict) else None
        name = ref.get("name") if isinstance(ref, dict) else None
        if not isinstance(name, str) or not name:
            msg = "gh repo view returned no default branch"
            raise CodeHostError(msg)
        return name

    async def display_name(self, repo: RepositoryRef) -> str:
        return repo.value


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


_LIST_OP = "ChangeRequests.list"
_UNBOUNDED_LIMIT = 1_000_000

_FILES_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      files(first: 100, after: $cursor) {
        nodes { path viewerViewedState }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

_NODE_ID_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) { pullRequest(number: $number) { id } }
}
"""

_MARK_VIEWED_MUTATION = """
mutation($pullRequestId: ID!, $path: String!) {
  markFileAsViewed(input: {pullRequestId: $pullRequestId, path: $path}) { clientMutationId }
}
"""


def _split(repo: RepositoryRef) -> tuple[str, str]:
    owner, _, name = repo.value.partition("/")
    if not owner or not name:
        msg = "repository identity must be written owner/repo"
        raise CodeHostError(msg)
    return owner, name


def _path(data: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    """Follow ``keys`` through nested objects. Raises CodeHostError when a step is not an object."""
    node: Any = data
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        msg = "gh api graphql returned an unexpected response"
        raise CodeHostError(msg)
    return node


_E = TypeVar("_E", bound=Enum)


def _enum(kind: type[_E], value: object, default: _E | None) -> _E | None:
    if isinstance(value, str):
        try:
            return kind(value.lower())
        except ValueError:
            return default
    return default


def _opt_int(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _change_request(repo: RepositoryRef, data: Mapping[str, Any], number: int = 0) -> ChangeRequest:
    """A ``ChangeRequest`` from ``gh pr view|list --json`` output. Absent fields take the empty defaults."""
    reviewers = data.get("reviewRequests")
    return ChangeRequest(
        repository=repo,
        number=_int(data, "number") or number,
        title=_str(data, "title"),
        body=_str(data, "body"),
        url=_str(data, "url"),
        state=_state(_str(data, "state")),
        is_draft=bool(data.get("isDraft", False)),
        author=_user(data.get("author")),
        head_reference=_str(data, "headRefName"),
        head_revision=_str(data, "headRefOid"),
        base_reference=_str(data, "baseRefName"),
        is_cross_repository=bool(data.get("isCrossRepository", False)),
        created_at=_parse_time(data.get("createdAt")),
        mergeable=_enum(MergeableState, data.get("mergeable"), MergeableState.UNKNOWN) if "mergeable" in data else None,
        auto_merge_enabled=data.get("autoMergeRequest") is not None,
        review_decision=_enum(ReviewDecision, data.get("reviewDecision"), None),
        requested_reviewers=tuple(u for u in (_user(r) for r in reviewers) if u is not None)
        if isinstance(reviewers, list)
        else (),
        additions=_opt_int(data, "additions"),
        deletions=_opt_int(data, "deletions"),
    )


def _obj(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _rest_user(value: object) -> User | None:
    if isinstance(value, dict) and isinstance(value.get("login"), str):
        login = value["login"]
        return User(login=login, is_bot=value.get("type") == "Bot" or login.endswith("[bot]"))
    return None


def _rest_change_request(repo: RepositoryRef, data: Mapping[str, Any]) -> ChangeRequest:
    """A ``ChangeRequest`` from a REST pull request object."""
    head = _obj(data, "head")
    base = _obj(data, "base")
    head_repo = _obj(head, "repo")
    base_repo = _obj(base, "repo")
    state = ChangeRequestState.MERGED if data.get("merged_at") else _state(_str(data, "state"))
    return ChangeRequest(
        repository=repo,
        number=_int(data, "number"),
        title=_str(data, "title"),
        body=_str(data, "body"),
        url=_str(data, "html_url"),
        state=state,
        is_draft=bool(data.get("draft", False)),
        author=_rest_user(data.get("user")),
        head_reference=_str(head, "ref"),
        head_revision=_str(head, "sha"),
        base_reference=_str(base, "ref"),
        is_cross_repository=_str(head_repo, "full_name") != _str(base_repo, "full_name"),
        created_at=_parse_time(data.get("created_at")),
    )


def _commit_info(row: Mapping[str, Any]) -> CommitInfo:
    commit = _obj(row, "commit")
    committer = _obj(commit, "committer")
    message = commit.get("message")
    authors = tuple(u for u in (_rest_user(row.get("author")),) if u is not None)
    stamp = committer.get("date")
    return CommitInfo(
        revision=_str(row, "sha"),
        message=message if isinstance(message, str) else None,
        authors=authors,
        committed_at=_parse_time(stamp) if isinstance(stamp, str) else None,
    )


def _review(row: Mapping[str, Any]) -> Review:
    stamp = row.get("submitted_at")
    commit = row.get("commit_id")
    return Review(
        id=str(row.get("id", "")),
        author=_rest_user(row.get("user")),
        state=_enum(ReviewState, row.get("state"), ReviewState.COMMENTED) or ReviewState.COMMENTED,
        body=_str(row, "body"),
        submitted_at=_parse_time(stamp) if isinstance(stamp, str) else None,
        revision=commit if isinstance(commit, str) and commit else None,
        comments=(),
    )


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
        """The change request with its full field set (``gh pr view N --repo R --json <fields>``)."""
        data = await self._gh.json(
            ("pr", "view", str(number), "--repo", repo.value, "--json", _PR_FULL_FIELDS), "ChangeRequests.get"
        )
        return _change_request(repo, _object(data, _PR_VIEW), number)

    async def state(self, repo: RepositoryRef, number: int) -> ChangeRequestState:
        """Lifecycle state only (``gh pr view N --repo R --json state``)."""
        data = await self._gh.json(
            ("pr", "view", str(number), "--repo", repo.value, "--json", "state"), "ChangeRequests.state"
        )
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
                raise self._gh.failure(args, outcome)
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
        """Change requests in ``gh`` order, bounded by the declared list bound. Filters are logins.

        ``requested_reviewer`` is a search qualifier (``user-review-requested:<login>``). One call returns every field,
        so no per-item read follows.
        """
        args = ["pr", "list", "--repo", repo.value, "--state", state.value]
        if author is not None:
            args += ["--author", author]
        if assignee is not None:
            args += ["--assignee", assignee]
        if requested_reviewer is not None:
            args += ["--search", f"user-review-requested:{requested_reviewer}"]
        bound = _bound(_LIST_OP)
        # One past the bound, so a result that exceeds it is detectable instead of silently cut short by gh.
        # With no declared bound, request effectively everything.
        limit = _UNBOUNDED_LIMIT if bound is None else bound + 1
        args += ["--limit", str(limit), "--json", _PR_FIELDS]
        rows = await self._gh.json(args, _LIST_OP)
        if bound is not None and isinstance(rows, list) and len(rows) > bound:
            msg = f"{_LIST_OP} found more than the declared bound of {bound}"
            raise CodeHostError(msg)
        return [_change_request(repo, row) for row in rows or [] if isinstance(row, dict)]

    async def find_by_commit(self, repo: RepositoryRef, revision: str) -> Sequence[ChangeRequest]:
        """Change requests that contain the commit (``gh api repos/R/commits/SHA/pulls``, every page)."""
        rows = await self._gh.paged(f"repos/{repo.value}/commits/{revision}/pulls", "ChangeRequests.find_by_commit")
        return [_rest_change_request(repo, row) for row in rows]

    async def commits(self, repo: RepositoryRef, number: int) -> Sequence[CommitInfo]:
        """Commits in the order the host returns them (oldest first), every page."""
        rows = await self._gh.paged(f"repos/{repo.value}/pulls/{number}/commits", "ChangeRequests.commits")
        return [_commit_info(row) for row in rows]

    async def reviews(self, repo: RepositoryRef, number: int) -> Sequence[Review]:
        """All reviews, every page."""
        rows = await self._gh.paged(f"repos/{repo.value}/pulls/{number}/reviews", "ChangeRequests.reviews")
        return [_review(row) for row in rows]

    async def create_draft(self, repo: RepositoryRef, *, head: str, base: str, title: str, body: str) -> ChangeRequest:
        """Create a draft (``gh pr create --draft``). The number comes from the printed URL; no URL is an error."""
        args = ("pr", "create", "--draft", "--head", head, "--base", base, "--title", title, "--body", body)
        outcome = await self._gh.run(args, "ChangeRequests.create_draft", write=True)
        match = _PR_URL.search(outcome.stdout)
        if not outcome.success or not match:
            raise self._gh.failure(
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
        """Squash-merge pinned to ``expected_head`` (``--match-head-commit``). A mismatch raises CodeHostError."""
        args = ("pr", "merge", str(number), "--repo", repo.value, "--squash", "--match-head-commit", expected_head)
        await self._gh.ok(args, "ChangeRequests.squash_merge", write=True)

    async def request_reviewer(self, repo: RepositoryRef, number: int, login: str) -> None:
        await self._gh.ok(
            ("pr", "edit", str(number), "--repo", repo.value, "--add-reviewer", login),
            "ChangeRequests.request_reviewer",
            write=True,
        )

    async def approve(self, repo: RepositoryRef, number: int, *, head_revision: str) -> None:
        """Post an APPROVE review pinned to ``head_revision`` (``commit_id``)."""
        args = (
            *("api", "--method", "POST", f"repos/{repo.value}/pulls/{number}/reviews"),
            *("-f", "event=APPROVE", "-f", f"commit_id={head_revision}"),
        )
        await self._gh.ok(args, "ChangeRequests.approve", write=True)

    async def viewed_files(self, repo: RepositoryRef, number: int) -> Mapping[str, bool]:
        """Path to viewed flag of the acting user. Every page; a cursor that does not advance raises."""
        owner, name = _split(repo)
        viewed: dict[str, bool] = {}
        cursor: str | None = None
        while True:
            variables: dict[str, str | int] = {"owner": owner, "repo": name, "number": number}
            if cursor:
                variables["cursor"] = cursor
            data = await self._gh.graphql(_FILES_QUERY, "ChangeRequests.viewed_files", variables)
            files = _path(data, "repository", "pullRequest", "files")
            nodes = files.get("nodes")
            info = files.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(info, dict):
                msg = "gh api graphql returned an unexpected files response"
                raise CodeHostError(msg)
            for node in nodes:
                if isinstance(node, dict) and isinstance(node.get("path"), str):
                    viewed[node["path"]] = node.get("viewerViewedState") == "VIEWED"
            if not info.get("hasNextPage"):
                return viewed
            cursor = info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                msg = "gh api graphql returned no cursor for the next page"
                raise CodeHostError(msg)

    async def mark_viewed(self, repo: RepositoryRef, number: int, path: str) -> None:
        """Mark one file viewed. The node id read is a read; the mutation is the write."""
        owner, name = _split(repo)
        data = await self._gh.graphql(
            _NODE_ID_QUERY, "ChangeRequests.mark_viewed", {"owner": owner, "repo": name, "number": number}
        )
        node_id = _path(data, "repository", "pullRequest").get("id")
        if not isinstance(node_id, str) or not node_id:
            msg = "gh api graphql returned no pull request id"
            raise CodeHostError(msg)
        await self._gh.graphql(
            _MARK_VIEWED_MUTATION,
            "ChangeRequests.mark_viewed",
            {"pullRequestId": node_id, "path": path},
            write=True,
        )


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
    def __init__(self, gh: _Gh) -> None:
        self._gh = gh

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
        """Change-request-level comments, every page (``gh api repos/R/issues/N/comments``)."""
        rows = await self._gh.paged(f"repos/{repo.value}/issues/{number}/comments", "Discussion.comments")
        return [_rest_comment(row) for row in rows]

    async def review_threads(self, repo: RepositoryRef, number: int) -> Sequence[ReviewThread]:
        """Review threads with their comments, every page. More than 100 comments in one thread raises."""
        owner, name = _split(repo)
        threads: list[ReviewThread] = []
        cursor: str | None = None
        while True:
            variables: dict[str, str | int] = {"owner": owner, "repo": name, "number": number}
            if cursor:
                variables["cursor"] = cursor
            data = await self._gh.graphql(_THREADS_QUERY, "Discussion.review_threads", variables)
            page = _path(data, "repository", "pullRequest", "reviewThreads")
            nodes = page.get("nodes")
            info = page.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(info, dict):
                msg = "gh api graphql returned an unexpected review thread response"
                raise CodeHostError(msg)
            threads.extend(_review_thread(node) for node in nodes if isinstance(node, dict))
            if not info.get("hasNextPage"):
                return threads
            cursor = info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                msg = "gh api graphql returned no cursor for the next page"
                raise CodeHostError(msg)

    async def post_comment(self, repo: RepositoryRef, number: int, body: str) -> Comment:
        """Post a change-request-level comment (``gh pr comment N --repo R --body B``). The id comes from the URL."""
        outcome = await self._gh.ok(
            ("pr", "comment", str(number), "--repo", repo.value, "--body", body), "Discussion.post_comment", write=True
        )
        url = outcome.stdout.strip().splitlines()[-1] if outcome.stdout.strip() else None
        match = _ISSUE_COMMENT_ID.search(url or "")
        return Comment(
            id=match.group(1) if match else "",
            author=None,
            body=body,
            created_at=_parse_time(None),
            url=url,
        )

    async def reply(self, repo: RepositoryRef, number: int, thread_id: str, body: str) -> Comment:
        """Reply in a review thread (GraphQL mutation). ``thread_id`` is the id from ``ReviewThread``."""
        data = await self._gh.graphql(
            _REPLY_MUTATION, "Discussion.reply", {"threadId": thread_id, "body": body}, write=True
        )
        reply = _path(data, "addPullRequestReviewThreadReply", "comment")
        return _graphql_comment(reply)


_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          path
          line
          originalLine
          diffSide
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes {
              id databaseId isMinimized body createdAt url diffHunk
              author { login __typename }
            }
          }
        }
      }
    }
  }
}
"""

_REPLY_MUTATION = """
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment { id databaseId isMinimized body createdAt url diffHunk author { login __typename } }
  }
}
"""


def _rest_comment(row: Mapping[str, Any]) -> Comment:
    url = row.get("html_url")
    return Comment(
        id=str(row.get("id", "")),
        author=_rest_user(row.get("user")),
        body=_str(row, "body"),
        created_at=_parse_time(row.get("created_at")),
        url=url if isinstance(url, str) else None,
    )


def _graphql_comment(node: Mapping[str, Any]) -> Comment:
    """A ``Comment`` from a GraphQL node. The id is the REST database id when present, else the node id."""
    author = node.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    bot = isinstance(author, dict) and (author.get("__typename") == "Bot" or str(login).endswith("[bot]"))
    database_id = node.get("databaseId")
    hunk = node.get("diffHunk")
    url = node.get("url")
    return Comment(
        id=str(database_id) if database_id is not None else _str(node, "id"),
        author=User(login=login, is_bot=bot) if isinstance(login, str) else None,
        body=_str(node, "body"),
        created_at=_parse_time(node.get("createdAt")),
        url=url if isinstance(url, str) else None,
        is_hidden=bool(node.get("isMinimized", False)),
        diff_hunk=hunk if isinstance(hunk, str) and hunk else None,
    )


def _review_thread(node: Mapping[str, Any]) -> ReviewThread:
    comments = node.get("comments")
    items = comments.get("nodes") if isinstance(comments, dict) else None
    info = comments.get("pageInfo") if isinstance(comments, dict) else None
    if not isinstance(items, list) or (isinstance(info, dict) and info.get("hasNextPage")):
        msg = "a review thread has more comments than one page returns"
        raise CodeHostError(msg)
    path = node.get("path")
    line = _opt_int(node, "line") or _opt_int(node, "originalLine")
    side = node.get("diffSide")
    diff_side = side if isinstance(side, str) and side else "RIGHT"
    anchor = Anchor(path, line, diff_side) if isinstance(path, str) and path and line else None
    return ReviewThread(
        id=_str(node, "id"),
        is_resolved=bool(node.get("isResolved", False)),
        anchor=anchor,
        comments=tuple(_graphql_comment(item) for item in items if isinstance(item, dict)),
    )


_CHECK_FIELDS = "name,bucket,link,workflow,startedAt,completedAt"
_ZERO_YEAR = 1  # gh reports an unset completedAt as 0001-01-01T00:00:00Z
_NO_CHECKS_TEXT = "no checks reported"
_NO_REQUIRED_CHECKS_TEXT = "no required checks reported"
_PENDING_EXIT = 8  # ``gh pr checks`` exits 8 while checks are pending and still prints the JSON
_JOB_ID_RE = re.compile(r"/job/(\d+)")
_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")
_UNNAMED_CHECK = "<unnamed>"


def _check_time(value: object) -> datetime | None:
    """A check timestamp, or ``None`` when it is unset (empty, unparseable or the zero value)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.year <= _ZERO_YEAR:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _check(row: Mapping[str, Any]) -> Check:
    """A ``Check`` from one ``gh pr checks --json`` entry. The ``/job/<id>`` of the link is the job id."""
    status = _enum(CheckStatus, row.get("bucket"), None)
    if status is None:
        msg = "gh pr checks returned a check with an unknown bucket"
        raise CodeHostError(msg)
    name = row.get("name")
    link = row.get("link")
    workflow = row.get("workflow")
    job = _JOB_ID_RE.search(link) if isinstance(link, str) else None
    return Check(
        name=name if isinstance(name, str) and name else _UNNAMED_CHECK,
        status=status,
        details_url=link if isinstance(link, str) and link else None,
        workflow_name=workflow if isinstance(workflow, str) and workflow else None,
        started_at=_check_time(row.get("startedAt")),
        completed_at=_check_time(row.get("completedAt")),
        job_id=job.group(1) if job else None,
    )


class _Checks:
    def __init__(self, gh: _Gh) -> None:
        self._gh = gh

    async def list_checks(self, repo: RepositoryRef, number: int, *, required_only: bool = False) -> Sequence[Check]:
        """Every run of every check, as ``gh`` reports it. Pending checks (exit 8 with JSON) are not a failure.

        No checks (or no required checks) is an empty result. A non-zero exit, or exit 8 with no JSON, raises.
        """
        args = ["pr", "checks", str(number), "--repo", repo.value, "--json", _CHECK_FIELDS]
        if required_only:
            args.append("--required")
        outcome = await self._gh.run(args, "Checks.list_checks")
        stderr = outcome.stderr.lower()
        if outcome.returncode == 1 and (_NO_REQUIRED_CHECKS_TEXT if required_only else _NO_CHECKS_TEXT) in stderr:
            return []
        if outcome.returncode not in (0, _PENDING_EXIT) or (
            outcome.returncode == _PENDING_EXIT and not outcome.stdout.strip()
        ):
            raise self._gh.failure(args, outcome)
        try:
            rows = json.loads(outcome.stdout or "null")
        except ValueError:
            msg = "gh pr checks returned invalid JSON"
            raise CodeHostError(msg) from None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            msg = "gh pr checks returned an unexpected response"
            raise CodeHostError(msg)
        return [_check(row) for row in rows]

    async def list_workflows(self, repo: RepositoryRef) -> Sequence[Workflow]:
        """The active workflows. More than the declared bound raises: the result is never cut short."""
        operation = "Checks.list_workflows"
        bound = _bound(operation)
        data = await self._gh.json(["api", f"repos/{repo.value}/actions/workflows?per_page=100"], operation)
        rows = data.get("workflows") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            msg = _GH_API_UNEXPECTED
            raise CodeHostError(msg)
        total = data.get("total_count")
        if bound is not None and (len(rows) > bound or (isinstance(total, int) and total > len(rows))):
            msg = f"{operation} found more than the declared bound of {bound}"
            raise CodeHostError(msg)
        return [
            Workflow(id=str(row["id"]), name=_str(row, "name"))
            for row in rows
            if isinstance(row, dict) and row.get("state") == "active" and "id" in row
        ]

    async def failed_log(self, repo: RepositoryRef, check: Check) -> str:
        """The failing part of the log: the job when the check has a job id, else the run of its link."""
        link = check.details_url or ""
        run_match = _RUN_ID_RE.search(link)
        if check.job_id:
            args = ["run", "view", "--job", check.job_id, "--repo", repo.value, "--log-failed"]
        elif run_match:
            args = ["run", "view", run_match.group(1), "--repo", repo.value, "--log-failed"]
        else:
            msg = "the check names no job or run to read a log from"
            raise CodeHostError(msg)
        return (await self._gh.ok(args, "Checks.failed_log")).stdout

    async def get_job(self, repo: RepositoryRef, job_id: str) -> Job:
        """The job with its first failed step, or ``None`` as the step when no step failed."""
        data = await self._gh.json(["api", f"repos/{repo.value}/actions/jobs/{job_id}"], "Checks.get_job")
        if not isinstance(data, dict):
            msg = _GH_API_UNEXPECTED
            raise CodeHostError(msg)
        steps = data.get("steps")
        failed = next(
            (
                str(step["name"])
                for step in (steps if isinstance(steps, list) else [])
                if isinstance(step, dict) and step.get("conclusion") == "failure" and step.get("name")
            ),
            None,
        )
        return Job(id=job_id, failed_step=failed)


class _WorkItems:
    def __init__(self, gh: _Gh) -> None:
        self._gh = gh

    async def get(self, repo: RepositoryRef, number: int) -> WorkItem | None:
        """The work item, ``None`` for a confirmed absence (the number is no issue). Any other failure raises."""
        args = ("issue", "view", str(number), "--repo", repo.value, "--json", _ISSUE_FIELDS)
        outcome = await self._gh.run(args, "WorkItems.get")
        if not outcome.success:
            if _ISSUE_NOT_FOUND_RE.search(outcome.stderr):
                return None
            raise self._gh.failure(args, outcome)
        try:
            data = json.loads(outcome.stdout or "null")
        except ValueError:
            msg = "gh issue view returned invalid JSON"
            raise CodeHostError(msg) from None
        return _work_item(repo, _object(data, "issue view"))

    async def list_open(
        self, repo: RepositoryRef, *, assignee: str | None = None, label: str | None = None
    ) -> Sequence[WorkItem]:
        """Open issues in ``gh`` order, bounded by the declared list bound."""
        args = ["issue", "list", "--state", "open"]
        if assignee is not None:
            args += ["--assignee", assignee]
        if label is not None:
            args += ["--label", label]
        bound = _bound(_LIST_OPEN_OP)
        args += ["--limit", str(bound), "--json", "number,title,createdAt,assignees,labels"]
        rows = await self._gh.json(args, _LIST_OPEN_OP)
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
        # gh returns each reference as {number, repository: {name, owner: {login}}, ...}.
        refs = view.get("closedByPullRequestsReferences") if isinstance(view, dict) else None
        found: list[ChangeRequest] = []
        for ref in refs if isinstance(refs, list) else []:
            if not isinstance(ref, dict) or _int(ref, "number") == 0 or not _same_repository(repo, ref):
                continue
            pr = await self._gh.json(
                ("pr", "view", str(_int(ref, "number")), "--json", "number,state"), "WorkItems.closing_change_requests"
            )
            found.append(_summary(repo, _object(pr, _PR_VIEW)))
        return found

    async def open_change_request_bodies(self, repo: RepositoryRef) -> Sequence[ChangeRequest]:
        """Open change requests with their bodies, bounded by the declared list bound."""
        bound = _bound(_OPEN_CR_BODIES_OP)
        rows = await self._gh.json(
            ("pr", "list", "--state", "open", "--limit", str(bound), "--json", "body"),
            _OPEN_CR_BODIES_OP,
        )
        return [_summary(repo, row, state=ChangeRequestState.OPEN) for row in rows or [] if isinstance(row, dict)]

    async def linked_work_items(self, repo: RepositoryRef, number: int) -> Sequence[WorkItem]:
        """Work items the change request closes, each read in its own repository. Confirmed absences are dropped."""
        view = await self._gh.json(
            ("pr", "view", str(number), "--repo", repo.value, "--json", "closingIssuesReferences"),
            "WorkItems.linked_work_items",
        )
        refs = view.get("closingIssuesReferences") if isinstance(view, dict) else None
        found: list[WorkItem] = []
        seen: set[tuple[str, int]] = set()
        for ref in refs if isinstance(refs, list) else []:
            if not isinstance(ref, dict) or _int(ref, "number") == 0:
                continue
            target = _reference_repository(repo, ref)
            key = (target.value.lower(), _int(ref, "number"))
            if key in seen:
                continue
            seen.add(key)
            item = await self.get(target, _int(ref, "number"))
            if item is not None:
                found.append(item)
        return found


def _reference_repository(repo: RepositoryRef, ref: Mapping[str, Any]) -> RepositoryRef:
    """The repository a closing reference names. A reference without repository data is local."""
    repository = ref.get("repository")
    if isinstance(repository, dict):
        full = repository.get("nameWithOwner")
        if isinstance(full, str) and full:
            return RepositoryRef(full)
        owner = repository.get("owner")
        login = owner.get("login") if isinstance(owner, dict) else None
        name = repository.get("name")
        if isinstance(login, str) and isinstance(name, str):
            return RepositoryRef(f"{login}/{name}")
    return repo


def _same_repository(repo: RepositoryRef, ref: Mapping[str, Any]) -> bool:
    """False only when ``ref`` names a different repository. A reference without repository data is taken as local."""
    repository = ref.get("repository")
    if not isinstance(repository, dict):
        return True
    full = repository.get("nameWithOwner")
    if not isinstance(full, str):
        owner = repository.get("owner")
        login = owner.get("login") if isinstance(owner, dict) else None
        name = repository.get("name")
        if not isinstance(login, str) or not isinstance(name, str):
            return True
        full = f"{login}/{name}"
    return full.lower() == repo.value.lower()


def _work_item(repo: RepositoryRef, row: Mapping[str, Any]) -> WorkItem:
    assignees = row.get("assignees")
    labels = row.get("labels")
    return WorkItem(
        repository=repo,
        number=_int(row, "number"),
        title=_str(row, "title"),
        body=_str(row, "body"),
        labels=tuple(_str(lbl, "name") for lbl in labels if isinstance(lbl, dict)) if isinstance(labels, list) else (),
        assignees=tuple(u for u in (_user(a) for a in assignees) if u is not None)
        if isinstance(assignees, list)
        else (),
        created_at=_parse_time(row.get("createdAt")),
        state=_enum(WorkItemState, row.get("state"), WorkItemState.OPEN) or WorkItemState.OPEN,
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
        Capability.CHECKS,
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
        page_attempts=_PAGE_ATTEMPTS,
        page_retry_delay_seconds=_PAGE_RETRY_DELAY,
        list_bounds=ListBounds(
            default=100,
            per_operation={
                _LIST_OP: 1000,
                _LIST_OPEN_OP: 100,
                _OPEN_CR_BODIES_OP: 200,
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
        discussion=_Discussion(gh),
        checks=_Checks(gh),
        work_items=_WorkItems(gh),
        declaration=DECLARATION,
    )


REGISTRATION = HostRegistration(
    interface_version=CODE_HOST_INTERFACE_VERSION,
    options_model=ReferenceOptions,
    factory=_create,
)
