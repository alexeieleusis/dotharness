"""Reference code host, registered under the name ``reference``. It runs the ``gh`` CLI.

This module owns every host-specific rule of the review flow: the ``gh`` command lines, the remote-URL rules, the
review text, the size limits and the fallback to a top-level comment. It imports nothing from ``vibe_heal``. The
review result is read by duck typing (``files``, ``base_branch``, ``project_label``, ``head_sha``).
"""

import asyncio
import json
import logging
import math
import re
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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
    ReviewPreview,
    ReviewThread,
    User,
    Workflow,
    WorkItem,
)
from dotharness.code_host.version import CODE_HOST_INTERFACE_VERSION
from dotharness.gh import gh_argv
from dotharness.subprocess_runner import RunResult, run

logger = logging.getLogger(__name__)

_MAX_INLINE_COMMENTS = 50
_MAX_REVIEW_BODY_CHARS = 60_000  # The host limit is about 65536. Keep headroom.
_MAX_OVERFLOW_BODY_LINES = 100
_POST_TIMEOUT_SECONDS = 60
_TRUNCATION_NOTICE = "\n\n_...review body truncated to fit GitHub's size limit._"
_NOT_INSTALLED = "gh CLI is not installed or not in PATH. Install it from https://cli.github.com/"
_PR_FIELDS = "number,baseRefName,title,body,url,state,isDraft,headRefName,headRefOid,isCrossRepository,createdAt,author"


# --------------------------------------------------------------------------------------------------------------------
# Command running
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Outcome:
    success: bool
    stdout: str
    stderr: str


@asynccontextmanager
async def _deadline(cmd: list[str], seconds: float) -> AsyncGenerator[None]:
    """Bound the enclosed block by a deadline. A read that expires raises CodeHostError (not the timeout error)."""
    try:
        async with asyncio.timeout(seconds):
            yield
    except TimeoutError:
        msg = f"`{cmd[0]} {cmd[1] if len(cmd) > 1 else ''}`".strip() + f" timed out after {seconds:g}s"
        raise CodeHostError(msg) from None


async def _run_gh(cmd: list[str], seconds: float) -> _Outcome:
    """Run a command under a caller-chosen deadline. The deadline kills the child process group on expiry."""
    async with _deadline(cmd, seconds):
        result = await run(cmd, cwd=Path.cwd(), timeout=math.inf)
    if result.not_found:
        raise FileNotFoundError(result.stderr)
    return _Outcome(result.returncode == 0, result.stdout, result.stderr)


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
# Review text
# --------------------------------------------------------------------------------------------------------------------


def _truncate_body(body: str) -> str:
    if len(body) <= _MAX_REVIEW_BODY_CHARS:
        return body
    return body[: _MAX_REVIEW_BODY_CHARS - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE


def _coverage_table(file_reviews: Sequence[Any]) -> str:
    """Markdown table of coverage on changed lines. A percentage below 80 is bold."""
    header = "| File | Covered Lines | Instrumented Changed Lines | Coverage % |"
    alignment = "| :--- | ---: | ---: | ---: |"
    rows = []
    for fr in file_reviews:
        if fr.coverage_pct is None:
            continue
        pct_str = f"{fr.coverage_pct:.1f}%"
        if fr.coverage_pct < 80.0:
            pct_str = f"**{pct_str}**"
        rows.append(f"| {fr.file_path} | {fr.covered_lines} | {fr.instrumented_changed_lines} | {pct_str} |")
    if not rows:
        return ""
    return "\n".join([header, alignment, *rows])


def _issue_body(issue: Any) -> str:
    body = f"**{issue.rule}** {issue.message}"
    if issue.doc_url:
        body += f"\n\n{issue.doc_url}"
    if issue.root_cause:
        body += (
            f"\n\n<details>\n\n<summary>{issue.rule} — why this matters</summary>\n\n{issue.root_cause}\n\n</details>"
        )
    return body


def _collect_file_comments(
    file_review: Any, base_branch: str, comments: list[tuple[str, int, str]], nearby_lines: list[str]
) -> None:
    for issue in file_review.issues:
        body = _issue_body(issue)
        if issue.on_changed_line:
            comments.append((file_review.file_path, issue.line, body))
        else:
            nearby_lines.append(f"- **{issue.rule}** ({file_review.file_path}:{issue.line}) {issue.message}")
    for dup in file_review.duplications:
        locations = ", ".join(f"`{loc.file_path}` lines {loc.from_line}-{loc.to_line}" for loc in dup.other_locations)
        body = (
            f"**Duplication detected** (lines {dup.from_line}-{dup.to_line})\n\n"
            f"This block is duplicated in: {locations}"
        )
        # anchor_line is a changed line in the diff, so the host accepts the comment. Older reports lack it.
        anchor = dup.anchor_line if dup.anchor_line is not None else dup.from_line
        comments.append((file_review.file_path, anchor, body))
    for res in file_review.resolved_duplications:
        other = "\n".join(f"- `{loc.file_path}` lines {loc.from_line}-{loc.to_line}" for loc in res.other_locations)
        body = (
            f"**Possible missed update** - lines {res.main_from_line}-{res.main_to_line} in `{base_branch}` were duplicated.\n\n"
            "You modified this region. The duplication may be resolved here, but check the other instances:\n\n"
            f"{other}"
        )
        comments.append((file_review.file_path, res.anchor_new_line, body))


def _build_summary(report: Any, inline: int, capped: int, nearby: int) -> str:
    label_suffix = f" [{report.project_label}]" if report.project_label else ""
    sha_suffix = f" (commit {report.head_sha[:7]})" if report.head_sha else ""
    total = inline + capped + nearby
    if not total:
        return f"SonarQube{label_suffix}: no findings on or near changed lines.{sha_suffix}"
    counts = [(inline, "inline"), (capped, "capped"), (nearby, "near changed lines")]
    breakdown = ", ".join(f"{n} {name}" for n, name in counts if n)
    return f"SonarQube{label_suffix}: {total} finding(s) ({breakdown}).{sha_suffix}"


def _build_overflow_section(overflow: list[tuple[str, int, str]]) -> str:
    visible = overflow[:_MAX_OVERFLOW_BODY_LINES]
    overflow_lines = [f"- **{path}:{line}** {body.splitlines()[0]}" for path, line, body in visible]
    extra = len(overflow) - len(visible)
    suffix = f"\n_...and {extra} more not shown._" if extra else ""
    return (
        f"**{len(overflow)} finding(s) over the {_MAX_INLINE_COMMENTS}-comment inline limit**"
        f" (capped to avoid GitHub rate limits):\n" + "\n".join(overflow_lines) + suffix
    )


def _build_review(report: Any) -> ReviewPreview:
    """Review body and inline comments for ``report``. The only place that builds review text."""
    all_comments: list[tuple[str, int, str]] = []
    nearby_lines: list[str] = []
    for file_review in report.files:
        _collect_file_comments(file_review, report.base_branch, all_comments, nearby_lines)

    comments, overflow = all_comments[:_MAX_INLINE_COMMENTS], all_comments[_MAX_INLINE_COMMENTS:]

    body_parts = [_build_summary(report, len(comments), len(overflow), len(nearby_lines))]
    if nearby_lines:
        body_parts.append(
            "**Issues near changed lines** (outside diff — shown here instead of inline):\n" + "\n".join(nearby_lines)
        )
    if overflow:
        body_parts.append(_build_overflow_section(overflow))
    files_with_coverage = [fr for fr in report.files if fr.coverage_pct is not None]
    if files_with_coverage:
        body_parts.append("**Coverage on changed lines:**\n\n" + _coverage_table(files_with_coverage))
    return ReviewPreview(
        body=_truncate_body("\n\n".join(body_parts)),
        inline_comments=[InlineComment(body, Anchor(path, line, "RIGHT")) for path, line, body in comments],
    )


def _build_fallback_body(report: Any) -> str:
    """Top-level fallback text: every issue, duplication and resolved duplication as a bullet, plus coverage."""
    sha_suffix = f" (commit {report.head_sha[:7]})" if report.head_sha else ""
    lines: list[str] = [f"SonarQube fallback review{sha_suffix}"] if sha_suffix else []
    seen_rules: set[str] = set()
    for file_review in report.files:
        for issue in file_review.issues:
            lines.append(f"- **{issue.rule}** ({file_review.file_path}:{issue.line}) {issue.message}")
            if issue.root_cause and issue.rule not in seen_rules:
                seen_rules.add(issue.rule)
                lines.append(
                    f"\n<details>\n\n<summary>{issue.rule} — why this matters</summary>"
                    f"\n\n{issue.root_cause}\n\n</details>\n"
                )
        for dup in file_review.duplications:
            lines.append(
                f"- **Duplication** ({file_review.file_path} lines {dup.from_line}-{dup.to_line}) "
                f"duplicated in {len(dup.other_locations)} other location(s)"
            )
        for res in file_review.resolved_duplications:
            lines.append(
                f"- **Possible missed update** ({file_review.file_path}) - "
                f"lines {res.main_from_line}-{res.main_to_line} in main were duplicated; "
                f"{len(res.other_locations)} other instance(s) may need updating"
            )
    files_with_coverage = [fr for fr in report.files if fr.coverage_pct is not None]
    if files_with_coverage:
        lines.extend(["", "**Coverage on changed lines:**\n\n" + _coverage_table(files_with_coverage)])
    return _truncate_body("\n".join(lines))


# --------------------------------------------------------------------------------------------------------------------
# Capability objects
# --------------------------------------------------------------------------------------------------------------------


def _unsupported(capability: Capability, operation: str) -> UnsupportedCapabilityError:
    msg = f"{capability.value}.{operation} is not supported by the reference code host"
    return UnsupportedCapabilityError(msg, capability=capability.value)


class _Session:
    async def available(self) -> bool:
        """True when ``gh --version`` succeeds within 10 s. Never raises."""
        try:
            return (await _run_gh(gh_argv("--version"), 10)).success
        except Exception:
            return False

    async def credential(self) -> Credential:
        raise _unsupported(Capability.SESSION, "credential")

    async def current_user(self) -> str:
        raise _unsupported(Capability.SESSION, "current_user")

    async def agent_environment_names(self) -> Sequence[str]:
        raise _unsupported(Capability.SESSION, "agent_environment_names")

    async def agent_environment(self, base: Mapping[str, str] | None = None) -> Mapping[str, str]:
        raise _unsupported(Capability.SESSION, "agent_environment")


class _Repository:
    async def current(self) -> RepositoryRef:
        """Owner and repository of the clone, from ``gh`` and then from the ``origin`` remote."""
        outcome = await _run_gh(
            gh_argv("repo", "view", "--json", "owner,name", "--jq", '"\\(.owner.login)/\\(.name)"'), 15
        )
        if outcome.success and outcome.stdout.strip():
            return RepositoryRef(outcome.stdout.strip())

        try:
            git_outcome = await _run_gh(["git", "remote", "get-url", "origin"], 10)
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
        raise _unsupported(Capability.REPOSITORY, "default_branch")

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


class _ChangeRequests:
    async def for_current_branch(self, repo: RepositoryRef) -> ChangeRequest:
        """The open pull request of the checked-out branch (``gh pr view``, no branch argument)."""
        await _require_gh()
        outcome = await _run_gh(gh_argv("pr", "view", "--json", _PR_FIELDS), 30)
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
        raise _unsupported(Capability.CHANGE_REQUESTS, "get")

    async def state(self, repo: RepositoryRef, number: int) -> ChangeRequestState:
        raise _unsupported(Capability.CHANGE_REQUESTS, "state")

    async def find_by_branch(
        self, repo: RepositoryRef, branch: str, *, state: ChangeRequestState = ChangeRequestState.OPEN
    ) -> Sequence[ChangeRequest]:
        raise _unsupported(Capability.CHANGE_REQUESTS, "find_by_branch")

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
        raise _unsupported(Capability.CHANGE_REQUESTS, "create_draft")

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
    async def preview_review(self, report: object) -> ReviewPreview:
        """Review body and inline comments for ``report``. Makes no host call."""
        return _build_review(report)

    async def post_review(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        body: str,
        inline_comments: Sequence[InlineComment],
        head_revision: str | None = None,
        report: object | None = None,
    ) -> ReviewPostResult:
        """Post the review. On a rejection post one top-level fallback comment.

        Only a validation rejection (HTTP 422) is followed by a fallback. A timeout, 5xx, dropped connection,
        authentication failure or unsupported capability is re-raised, because the write may have succeeded.
        """
        await _require_gh()
        cmd = _review_endpoint(repo, number)
        payload = {
            "event": "COMMENT",
            "body": body,
            "comments": [
                {"path": c.anchor.path, "line": c.anchor.line, "side": c.anchor.side, "body": c.body}
                for c in inline_comments
            ],
        }
        try:
            await _post_json(cmd, payload)
        except _RejectedError as inline_err:  # Only a rejection is safe to retry. Anything else may have posted.
            reason = str(inline_err)
            logger.warning("Inline review failed (%s) — falling back to top-level comment", reason)
            fallback_body = body if report is None else _build_fallback_body(report)
            try:
                await _post_json(cmd, {"event": "COMMENT", "body": fallback_body, "comments": []})
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


class _AbsentWorkItems:
    """WorkItems are not declared. Every call raises and runs no ``gh``."""

    async def get(self, repo: RepositoryRef, number: int) -> WorkItem | None:
        raise _unsupported(Capability.WORK_ITEMS, "get")

    async def list_open(
        self, repo: RepositoryRef, *, assignee: str | None = None, label: str | None = None
    ) -> Sequence[WorkItem]:
        raise _unsupported(Capability.WORK_ITEMS, "list_open")

    async def remove_label(self, repo: RepositoryRef, number: int, label: str) -> None:
        raise _unsupported(Capability.WORK_ITEMS, "remove_label")

    async def closing_change_requests(self, repo: RepositoryRef, number: int) -> Sequence[ChangeRequest]:
        raise _unsupported(Capability.WORK_ITEMS, "closing_change_requests")

    async def open_change_request_bodies(self, repo: RepositoryRef) -> Sequence[ChangeRequest]:
        raise _unsupported(Capability.WORK_ITEMS, "open_change_request_bodies")

    async def linked_work_items(self, repo: RepositoryRef, number: int) -> Sequence[WorkItem]:
        raise _unsupported(Capability.WORK_ITEMS, "linked_work_items")


# --------------------------------------------------------------------------------------------------------------------
# Declaration and registration
# --------------------------------------------------------------------------------------------------------------------

DECLARATION = HostDeclaration(
    capabilities=frozenset({
        Capability.SESSION,
        Capability.REPOSITORY,
        Capability.CHANGE_REQUESTS,
        Capability.DISCUSSION,
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
                "ChangeRequests.find_by_branch": 30,
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


def _create(options: ReferenceOptions, token_cmd: TokenCmd) -> CodeHost:
    """Build the reference ``CodeHost``. Runs no command. ``gh`` owns its own authentication."""
    return CodeHost(
        session=_Session(),
        repository=_Repository(),
        change_requests=_ChangeRequests(),
        discussion=_Discussion(),
        checks=_AbsentChecks(),
        work_items=_AbsentWorkItems(),
        declaration=DECLARATION,
    )


REGISTRATION = HostRegistration(
    interface_version=CODE_HOST_INTERFACE_VERSION,
    options_model=ReferenceOptions,
    factory=_create,
)
