import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, TypeVar

from dotharness.code_host import (
    ChangeRequest,
    ChangeRequestState,
    CodeHost,
    CodeHostError,
    Comment,
    ConfigurationError,
    HostSelection,
    RepositoryRef,
    ReviewThread,
    UnsupportedCapabilityError,
    WorkItem,
    create_code_host,
)
from dotharness.pr_review.backend import Backend
from dotharness.pr_review.config import HarnessConfig, RepoConfig, SubDir
from dotharness.subprocess_runner import run

logger = logging.getLogger(__name__)

TIMEOUT_GIT = 60
TIMEOUT_FETCH_COMMENTS = 120


def resolve_env_file(repo: RepoConfig, subdir: SubDir) -> Path | None:
    """Absolute path of the subdir's vibe_heal env file, or None when unset.

    Relative paths resolve against ``repo.working_dir`` (not the subdir path or the process cwd).
    No containment check and no symlink resolution: the file may live anywhere.
    """
    if subdir.env_file is None:
        return None
    return repo.working_dir.absolute() / subdir.env_file


def env_file_args(repo: RepoConfig, subdir: SubDir) -> list[str]:
    """Argv tail ``["--env-file", <path>]`` for vibe_heal, or ``[]`` when the subdir has no env file."""
    resolved = resolve_env_file(repo, subdir)
    return [] if resolved is None else ["--env-file", str(resolved)]


def env_file_error(config: HarnessConfig, subdir: SubDir) -> str | None:
    """Return an error message when the subdir's configured env_file is not an existing regular file.

    None when env_file is unset or the file exists. Only the path is checked; the file is never opened.
    """
    resolved = resolve_env_file(config.repo, subdir)
    if resolved is None or resolved.is_file():
        return None
    return f"env_file for subdir {subdir.path} is not an existing regular file: {resolved}"


PR_COMMENTS_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "pr-comments.py"

FOCUSED_REVIEW_MARKER = "[focused-review-bot]"
INLINE_REVIEW_MARKER = "<!-- dotharness-review-inline -->"
DESIGN_REVIEW_MARKER = "<!-- dotharness-review-design -->"
TRACEABILITY_REVIEW_MARKER = "<!-- dotharness-review-traceability -->"
REGRET_REVIEW_MARKER = "<!-- dotharness-review-regret -->"

# Every PR-level pass marker shares the `dotharness-review` prefix that
# is_review_summary_comment's substring check looks for, so a comment carrying one of
# these (e.g. the traceability pass's terminal "no linked ticket" comment) must be
# excluded there — otherwise it gets mistaken for the file/summary pass's own
# completion marker and short-circuits correctness review.
_PR_LEVEL_PASS_MARKERS = (
    INLINE_REVIEW_MARKER,
    DESIGN_REVIEW_MARKER,
    TRACEABILITY_REVIEW_MARKER,
    REGRET_REVIEW_MARKER,
)

# requirement-traceability-requirements.md §7.2/§11.2: the early-comment window is a
# hardcoded constant this iteration, not a harness.toml field.
TRACEABILITY_COMMENT_WINDOW_SECONDS = 300

# Matches, in priority order: a full GitHub issue URL, an "owner/repo#N" cross-repo
# shorthand, or a bare "#N" same-repo shorthand (requirement-traceability-requirements.md
# §7.2). The negative lookbehind on the bare form stops it from also matching the "#N"
# tail of an owner/repo#N reference that failed to match as such (it won't, in practice,
# since alternation already consumes that case first) or from matching mid-identifier.
_ISSUE_REF_RE = re.compile(
    r"https://github\.com/(?P<owner1>[\w.-]+)/(?P<repo1>[\w.-]+)/issues/(?P<num1>\d+)"
    r"|(?P<owner2>[\w.-]+)/(?P<repo2>[\w.-]+)#(?P<num2>\d+)"
    r"|(?<![\w/])#(?P<num3>\d+)"
)

# The `linear[bot]` GitHub App's marker for the comment it posts on a PR that closes a
# Linear ticket (requirement-traceability-requirements.md §4/G5 update: a repo that uses
# Linear exclusively has no GitHub Issues at all, so neither _resolve_native_linked_tickets
# nor _ISSUE_REF_RE can ever find anything there — this comment is that repo's only signal).
LINEAR_LINKBACK_MARKER = "<!-- linear-linkback -->"

# Matches the bot comment's `<details><summary><a href="...">TICKET-ID Title</a></summary>`
# opening line, from which the ticket id/URL/title are read directly — no Linear API call.
_LINEAR_LINKBACK_RE = re.compile(
    r'<summary><a href="(?P<url>https://linear\.app/\S+?)">'
    r"(?P<ticket_id>[A-Z][A-Z0-9]*-\d+)\s+(?P<title>[^<]*)</a></summary>"
)


class FatalGitError(Exception):
    pass


def log_called_process_output(e: subprocess.CalledProcessError, log_func: Callable[..., None], label: str) -> None:
    if e.stdout:
        log_func("%s stdout: %s", label, e.stdout.decode("utf-8", errors="replace"))
    if e.stderr:
        log_func("%s stderr: %s", label, e.stderr.decode("utf-8", errors="replace"))


def _argv(cmd: list[str] | str, shell: bool) -> list[str]:
    """Map ``Popen``'s ``(args, shell)`` pair onto the exec-style argv the shared runner takes (POSIX)."""
    if shell:
        return ["/bin/sh", "-c", cmd] if isinstance(cmd, str) else ["/bin/sh", "-c", *cmd]
    return [cmd] if isinstance(cmd, str) else list(cmd)


async def run_cmd(
    cmd: list[str] | str,
    cwd: str,
    env: dict,
    timeout: int,
    check: bool = True,
    shell: bool = False,
) -> subprocess.CompletedProcess:
    """Run ``cmd`` through ``dotharness.subprocess_runner.run`` with the old ``subprocess`` contract.

    Process-group kill and the SIGTERM-then-SIGKILL policy live in the shared runner. This wrapper only maps
    its ``RunResult`` back: bytes stdout/stderr, ``TimeoutExpired`` on timeout, ``CalledProcessError`` when
    ``check`` and the exit code is non-zero, ``FileNotFoundError`` when the program cannot be started.
    Nothing is logged, so no argv or env leaks.
    """
    result = await run(_argv(cmd, shell), cwd=cwd, env=env, timeout=timeout)
    if result.not_found:
        raise FileNotFoundError(result.stderr)
    stdout = result.stdout.encode()
    stderr = result.stderr.encode()
    if result.timed_out:
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout or None, stderr=stderr or None)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, stdout, stderr)
    return subprocess.CompletedProcess(cmd, result.returncode, stdout, stderr)


async def git_detach_and_record(cwd: str, env: dict) -> str:
    try:
        await run_cmd(
            ["git", "checkout", "--recurse-submodules", "--detach", "HEAD"], cwd=cwd, env=env, timeout=TIMEOUT_GIT
        )
        result = await run_cmd(["git", "rev-parse", "HEAD"], cwd=cwd, env=env, timeout=TIMEOUT_GIT)
        return result.stdout.decode("utf-8").strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise FatalGitError(f"git detach failed: {e}") from e  # noqa: TRY003


async def git_fetch_and_checkout(branch: str, cwd: str, env: dict) -> None:
    try:
        await run_cmd(["git", "fetch", "origin"], cwd=cwd, env=env, timeout=TIMEOUT_GIT)
        await run_cmd(
            ["git", "checkout", "--recurse-submodules", "-B", branch, f"origin/{branch}"],
            cwd=cwd,
            env=env,
            timeout=TIMEOUT_GIT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        if e.stdout:
            logger.exception("git checkout stdout: %s", e.stdout.decode("utf-8", errors="replace"))
        if e.stderr:
            logger.exception("git checkout stderr: %s", e.stderr.decode("utf-8", errors="replace"))
        raise FatalGitError(f"git checkout {branch} failed: {e}") from e  # noqa: TRY003


async def get_head_sha(wdir: str, env: dict) -> str:
    result = await run_cmd(["git", "rev-parse", "HEAD"], cwd=wdir, env=env, timeout=TIMEOUT_GIT, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8").strip()


async def git_restore(original_sha: str, branch: str, cwd: str, env: dict) -> None:
    """Restore the checkout; cancellation-safe: a CancelledError lets the restore finish, then re-raises."""
    task = asyncio.ensure_future(_git_restore_impl(original_sha, branch, cwd, env))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait([task])
        raise


async def _git_restore_impl(original_sha: str, branch: str, cwd: str, env: dict) -> None:
    preserved = True
    if branch:
        try:
            await _preserve_unpushed_commits(branch, cwd, env)
        except Exception:
            # Preservation failing (timeout, OSError, ...) must not leave the checkout on the
            # PR branch: still attempt the checkout, but keep the branch so no work is lost.
            preserved = False
            logger.exception("git_restore: could not preserve unpushed commits on %s; keeping the local branch", branch)
    try:
        await run_cmd(
            ["git", "checkout", "--recurse-submodules", "-f", original_sha],
            cwd=cwd,
            env=env,
            timeout=TIMEOUT_GIT,
            check=False,
        )
        if branch and preserved:
            await run_cmd(["git", "branch", "-D", branch], cwd=cwd, env=env, timeout=TIMEOUT_GIT, check=False)
    except Exception:
        logger.exception("git restore failed")


async def _preserve_unpushed_commits(branch: str, cwd: str, env: dict) -> None:
    """Before `git_restore` force-checks-out elsewhere and deletes the local `branch`,
    save any commit(s) on it that origin/`branch` doesn't have under a recovery ref.

    Normally every commit on `branch` has already been pushed by the time we get here.
    But a backend run can rewrite history mid-run (see address_comments's fast-forward
    check) or a push can simply fail, leaving local-only commits. Deleting the branch in
    that state would silently destroy that work with no trace it ever existed."""
    head_result = await run_cmd(["git", "rev-parse", branch], cwd=cwd, env=env, timeout=TIMEOUT_GIT, check=False)
    if head_result.returncode != 0:
        return
    branch_sha = head_result.stdout.decode("utf-8", errors="replace").strip()
    is_pushed = await run_cmd(
        ["git", "merge-base", "--is-ancestor", branch_sha, f"origin/{branch}"],
        cwd=cwd,
        env=env,
        timeout=TIMEOUT_GIT,
        check=False,
    )
    if is_pushed.returncode == 0:
        return
    recovery_ref = f"refs/harness-recovery/{branch}-{branch_sha[:12]}"
    save_result = await run_cmd(
        ["git", "update-ref", recovery_ref, branch_sha], cwd=cwd, env=env, timeout=TIMEOUT_GIT, check=False
    )
    if save_result.returncode == 0:
        logger.warning(
            "git_restore: %s (%s) is not on origin/%s; preserved it at %s before deleting the local branch",
            branch,
            branch_sha,
            branch,
            recovery_ref,
        )
    else:
        logger.error(
            "git_restore: %s has unpushed commit(s) at %s that could NOT be preserved under a recovery ref "
            "— they will be lost when the local branch is deleted",
            branch,
            branch_sha,
        )


@dataclass(frozen=True)
class HostScaffold:
    """What every ``_run_locked`` builds once: the host, the resolved repository and the child-process environment."""

    host: CodeHost
    repo: RepositoryRef
    env: dict[str, str]


def host_selection(config: HarnessConfig) -> HostSelection:
    """The ``HostSelection`` for ``config``: host name, options and the effective token command (never empty)."""
    return HostSelection(
        setting="host",
        name=config.harness.host,
        options=config.harness.host_options,
        token_cmd=config.harness.effective_token_cmd,
        working_directory=config.repo.working_dir if config.repo.working_dir.is_dir() else None,
    )


def build_agent_base_env(path_prepend: list[str], env_vars: dict[str, str]) -> dict[str, str]:
    """``os.environ`` with ``path_prepend`` ahead of ``PATH`` and ``[harness] env`` applied; no credential yet.

    Pass the result to ``Session.agent_environment``, which adds the credential variables (and lets them win).
    """
    base = os.environ.copy()
    if path_prepend:
        base["PATH"] = ":".join(path_prepend) + ":" + base.get("PATH", "")
    base.update(env_vars)
    return base


async def agent_env(host: CodeHost, config: HarnessConfig) -> dict[str, str]:
    """Child-process environment for git and agents: base environment plus the host's credential variables.

    Raises ``TokenCommandError``, ``AuthenticationError`` or ``CodeHostTimeoutError`` when the credential cannot be
    resolved; a runner lets them stop the command (nonzero exit, as the old ``RuntimeError`` did).
    """
    base = build_agent_base_env(config.harness.path_prepend, config.harness.env)
    return dict(await host.session.agent_environment(base))


def backend_env(config: HarnessConfig, env: Mapping[str, str]) -> dict[str, str]:
    """``[harness] env`` plus ``GITHUB_TOKEN`` from the scaffold env, only when the scaffold has a non-empty one.

    An absent or empty scaffold token must not overwrite a ``GITHUB_TOKEN`` the user configured in ``[harness.env]``.
    """
    merged = dict(config.harness.env)
    if token := env.get("GITHUB_TOKEN"):
        merged["GITHUB_TOKEN"] = token
    return merged


async def open_host(config: HarnessConfig) -> HostScaffold:
    """Steps (1) to (4) of the runner scaffold, once per command: selection, host, repository, environment.

    A ``ConfigurationError`` from the factory or from ``Repository.resolve`` prints its message to stderr and exits
    with code 1 (the startup message). Credential failures propagate (see ``agent_env``).
    """
    try:
        host = create_code_host(host_selection(config))
        repo = await host.repository.resolve(config.repo.name)
    except ConfigurationError as e:
        sys.stderr.write(f"{e}\n")
        sys.exit(1)
    return HostScaffold(host=host, repo=repo, env=await agent_env(host, config))


async def host_token(host: CodeHost) -> str:
    """The host credential's token. Raises ``TokenCommandError``, ``AuthenticationError``, ``CodeHostTimeoutError``.

    The 30 s limit lives in the host's declaration. Callers must not log the result.
    """
    return (await host.session.credential()).token


async def is_change_request_open(host: CodeHost, repo: RepositoryRef, number: int) -> bool:
    """True when change request ``number`` is open right now. Fails closed: any ``CodeHostError`` gives False."""
    try:
        return await host.change_requests.state(repo, number) is ChangeRequestState.OPEN
    except CodeHostError:
        return False


async def host_current_user(host: CodeHost) -> str:
    """Login of the acting user. Fails open: any ``CodeHostError`` (including ``AuthenticationError``) is logged
    and gives ``""``. The log line is where a revoked token shows; it carries the error's text, which never holds
    the token or the token command."""
    try:
        return await host.session.current_user()
    except CodeHostError as e:
        logger.error("get_current_user: the host could not resolve a login: %s", e)  # noqa: TRY400
        return ""


# -- Unsupported-capability policy (docs/pr_review/configuration.md, "Unsupported-capability policy") ---------------

UnsupportedPolicy = Literal["degrade", "warn", "stop"]


class Site(StrEnum):
    """Where a host call is made: the first half of a policy key. A ``StrEnum``, so a mistyped member is an
    ``AttributeError`` at import time instead of a silent fall back to ``stop``; the values are the names the
    documentation table uses."""

    MERGE = "merge"
    AUTO_APPROVE = "auto_approve"
    AUTO_APPROVE_VIEWED = "auto_approve_viewed"
    CI_FIX = "ci_fix"
    CI_CHECKS_WORKFLOWS = "ci_checks_workflows"
    REQUIRED_CHECKS = "required_checks"
    REVIEWER_REQUEST = "reviewer_request"
    REGRET_REVIEW = "regret_review"
    TRACEABILITY = "traceability"
    CURRENT_USER = "current_user"
    CHANGE_REQUEST_READ = "change_request_read"


_CR_GET = "ChangeRequests.get"

# One table: (site, operation) -> policy. ``Interface.*`` covers every operation of that capability at the site. The
# same rows are documented in docs/pr_review/configuration.md; tests/pr_review/test_policy.py keeps them in sync.
# degrade: skip silently. warn: log the capability name and continue. stop: re-raise (the top-level handler exits
# non-zero). A pair not listed is "stop".
UNSUPPORTED_CAPABILITY_POLICY: Mapping[tuple[Site, str], UnsupportedPolicy] = MappingProxyType({
    (Site.MERGE, "ChangeRequests.squash_merge"): "stop",
    (Site.MERGE, _CR_GET): "stop",
    (Site.AUTO_APPROVE, "ChangeRequests.approve"): "stop",
    (Site.AUTO_APPROVE, "ChangeRequests.reviews"): "stop",
    (Site.AUTO_APPROVE, "ChangeRequests.commits"): "stop",
    (Site.AUTO_APPROVE_VIEWED, "ChangeRequests.viewed_files"): "degrade",
    (Site.AUTO_APPROVE_VIEWED, "ChangeRequests.mark_viewed"): "degrade",
    (Site.CI_FIX, "Checks.*"): "stop",
    (Site.CI_CHECKS_WORKFLOWS, "Checks.list_workflows"): "degrade",
    (Site.REQUIRED_CHECKS, "Checks.list_checks"): "stop",
    (Site.REVIEWER_REQUEST, "ChangeRequests.request_reviewer"): "warn",
    (Site.REVIEWER_REQUEST, _CR_GET): "warn",
    (Site.REGRET_REVIEW, "ChangeRequests.find_by_commit"): "stop",
    (Site.TRACEABILITY, "WorkItems.get"): "stop",
    (Site.TRACEABILITY, "WorkItems.linked_work_items"): "stop",
    (Site.CURRENT_USER, "Session.current_user"): "warn",
})

_T = TypeVar("_T")


def unsupported_policy(site: Site | str, operation: str) -> UnsupportedPolicy:
    """The policy for ``operation`` (``Interface.method``) at ``site``: exact row, then ``Interface.*``, else stop."""
    table = UNSUPPORTED_CAPABILITY_POLICY
    exact = table.get((site, operation))
    if exact is not None:
        return exact
    return table.get((site, f"{operation.split('.', 1)[0]}.*"), "stop")


async def call_with_policy(site: Site | str, operation: str, call: Callable[[], Awaitable[_T]], fallback: _T) -> _T:
    """Await ``call()`` under the unsupported-capability policy of ``(site, operation)``.

    Only ``UnsupportedCapabilityError`` is handled; any other ``CodeHostError`` propagates. stop re-raises, degrade
    returns ``fallback`` silently, warn logs the capability name and returns ``fallback``. The log line holds the
    capability name only, never a token or token command.
    """
    try:
        return await call()
    except UnsupportedCapabilityError as e:
        policy = unsupported_policy(site, operation)
        if policy == "stop":
            raise
        if policy == "warn":
            logger.warning("%s: host capability %s is not supported; continuing without it", site, e.capability)
        return fallback


async def call_or(
    site: Site | str,
    operation: str,
    call: Callable[[], Awaitable[_T]],
    fallback: _T,
    *,
    on_error: Callable[[CodeHostError], None] | None = None,
) -> _T:
    """``call_with_policy`` plus the fallback for every other ``CodeHostError``.

    ``UnsupportedCapabilityError`` follows the policy of ``(site, operation)`` (stop re-raises it). Any other
    ``CodeHostError`` is passed to ``on_error`` (when given, e.g. to log it) and gives ``fallback``. This replaces
    the ``except UnsupportedCapabilityError: raise`` / ``except CodeHostError`` pair at a call site.
    """
    try:
        return await call_with_policy(site, operation, call, fallback)
    except UnsupportedCapabilityError:
        raise
    except CodeHostError as e:
        if on_error is not None:
            on_error(e)
        return fallback


# The plain change-request reads below use ``Site.CHANGE_REQUEST_READ``. It has no policy row, so an unsupported
# capability stops.


async def get_change_request(host: CodeHost, repo: RepositoryRef, number: int) -> ChangeRequest | None:
    """Change request ``number``, or None (logged) when the host could not read it.

    ``UnsupportedCapabilityError`` follows the policy of ``(change_request_read, ChangeRequests.get)`` and stops.
    """
    return await call_or(
        Site.CHANGE_REQUEST_READ,
        _CR_GET,
        lambda: host.change_requests.get(repo, number),
        None,
        on_error=lambda e: logger.error("Failed to fetch PR #%d: %s", number, e),
    )


async def pr_from_url(host: CodeHost, repo: RepositoryRef, url: str) -> ChangeRequest | None:
    """The change request a ``.../<number>`` URL names, or None (logged) when the read failed."""
    number = int(url.rstrip("/").split("/")[-1])
    return await get_change_request(host, repo, number)


async def get_requested_reviewers(host: CodeHost, repo: RepositoryRef, number: int) -> list[str]:
    """Logins of the requested reviewers. A failed read gives ``[]``; an unsupported capability follows the
    ``reviewer_request`` policy (warn)."""

    async def read() -> list[str]:
        return [u.login for u in (await host.change_requests.get(repo, number)).requested_reviewers]

    return await call_or(Site.REVIEWER_REQUEST, _CR_GET, read, [])


async def was_review_requested(host: CodeHost, repo: RepositoryRef, number: int, login: str) -> bool:
    """Whether `login` was already a requested reviewer before processing started.

    Submitting a review via the host's API clears the submitter from a change request's
    requested-reviewers list, which would hide it from any runner's
    "review requested of me" search. Callers check this up front and re-add
    the reviewer with add_reviewer() afterward so the PR stays visible — none of
    these runners ever remove the reviewer themselves; only the human's own
    submitted review (or the host's own bookkeeping around it) does that."""
    return login in await get_requested_reviewers(host, repo, number)


async def add_reviewer(host: CodeHost, repo: RepositoryRef, number: int, login: str) -> None:
    """Request ``login`` as a reviewer. A failure is logged and swallowed: ``preserve_reviewer_request`` calls this
    from a ``finally``. A timeout (``write=True``) is never retried. An unsupported capability follows the
    ``reviewer_request`` policy (warn)."""
    try:
        await call_with_policy(
            Site.REVIEWER_REQUEST,
            "ChangeRequests.request_reviewer",
            lambda: host.change_requests.request_reviewer(repo, number, login),
            None,
        )
    except CodeHostError as e:
        # The display text is resolved defensively: a raise here would escape this handler and replace the
        # in-flight exception of the caller's `finally` without being logged.
        try:
            repo_text = await host.repository.display_name(repo)
        except CodeHostError:
            repo_text = repr(repo)
        # preserve_reviewer_request calls this from a `finally`, so a failure here has
        # nowhere else to surface — without this log line, the reviewer silently stays
        # dropped from the PR with no trace it was ever supposed to be restored.
        logger.error(  # noqa: TRY400
            "add_reviewer: failed to re-add %s as a reviewer on PR #%d (%s): %s",
            login,
            number,
            repo_text,
            e,
        )


@asynccontextmanager
async def preserve_reviewer_request(
    host: CodeHost, repo: RepositoryRef, pr_number: int, login: str
) -> AsyncGenerator[None]:
    """Wrap a PR-processing block that might submit a GitHub review as a side effect
    (e.g. replying to comments via the backend, or vibe_heal posting a static-analysis
    review), re-adding `login` as a requested reviewer afterward if it held that status
    beforehand — regardless of whether the wrapped block succeeds, fails, or raises,
    since none of these runners decide on their own that a human's review is "done".
    None of them ever remove the reviewer themselves: the human is expected to review
    and approve (or GitHub itself clears the request once they submit that review), so
    the runners' only job here is to undo their own side effects, not to conclude
    anything. review_prs, focused_review, address_comments, and review_requested all
    wrap their per-PR work in this.

    A falsy `login` (e.g. address_comments's get_current_user lookup failing) skips the
    check entirely rather than querying GitHub with an empty login, which could never match
    a real requested reviewer anyway."""
    was_requested = bool(login) and await was_review_requested(host, repo, pr_number, login)
    try:
        yield
    finally:
        if was_requested:
            await add_reviewer(host, repo, pr_number, login)


def is_review_summary_comment(body: str) -> bool:
    if any(marker in body for marker in _PR_LEVEL_PASS_MARKERS):
        return False
    lower = body.lower()
    return "review summary" in lower or "dotharness-review" in lower


def _is_author(comment: Comment, login: str) -> bool:
    return comment.author is not None and comment.author.login == login


async def _read_or_none(read: Callable[[], Awaitable[_T]], what: str, pr_number: int) -> _T | None:
    """Result of a host read, or None (logged) when the host could not answer, so a failed read stays distinct
    from an empty result. An unsupported capability stops (no policy row, default "stop")."""
    try:
        return await read()
    except UnsupportedCapabilityError:
        raise
    except CodeHostError as e:
        logger.warning("PR #%d: reading %s failed: %s", pr_number, what, e)
        return None


async def _matching_comments(
    host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str, predicate: Callable[[str], bool]
) -> list[Comment] | None:
    """Returns the current_user's change-request-level comments matching predicate (possibly empty), or None
    if the host call itself failed (e.g. rate limit, transient 5xx) and the result is inconclusive."""
    comments = await _read_or_none(lambda: host.discussion.comments(repo, pr_number), "comments", pr_number)
    if comments is None:
        return None
    return [c for c in comments if _is_author(c, current_user) and predicate(c.body or "")]


async def _matching_inline_comments(
    host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str, predicate: Callable[[str], bool]
) -> list[tuple[Comment, ReviewThread]] | None:
    """The current_user's inline review comments matching predicate, each with its thread (possibly empty),
    or None if the host call itself failed."""
    threads = await _read_or_none(lambda: host.discussion.review_threads(repo, pr_number), "review threads", pr_number)
    if threads is None:
        return None
    return [(c, t) for t in threads for c in t.comments if _is_author(c, current_user) and predicate(c.body or "")]


async def has_review_summary_comment(host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str) -> bool:
    return bool(await check_review_summary_comment_status(host, repo, pr_number, current_user))


async def check_review_summary_comment_status(
    host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str
) -> bool | None:
    """Tri-state version of has_review_summary_comment: None means the check itself was
    inconclusive (API failure), as opposed to a confirmed absence of the comment."""
    matches = await _matching_comments(host, repo, pr_number, current_user, is_review_summary_comment)
    return None if matches is None else bool(matches)


def is_inline_review_comment(body: str) -> bool:
    return INLINE_REVIEW_MARKER in body


async def has_inline_review_comments(host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str) -> bool:
    return bool(await _matching_inline_comments(host, repo, pr_number, current_user, is_inline_review_comment))


async def has_pr_level_pass_comment(
    host: CodeHost, repo: RepositoryRef, marker: str, pr_number: int, current_user: str
) -> bool:
    """Shared by every PR-level pass (design review, requirement-traceability review,
    ...) whose completion signal is "did I already post an issue-level PR comment
    containing this marker" — extracted per requirement-traceability-requirements.md §7.6,
    closing the reuse pointer design-review-requirements.md §11.5 left here. A PR-level
    pass always posts exactly one PR-level comment per successful run (findings, or a
    "nothing to report" quiet path), so checking issue-level comments alone is enough —
    no need to also check the inline review comments the way
    has_inline_review_comments does."""
    return bool(await check_pr_level_pass_comment_status(host, repo, marker, pr_number, current_user))


async def check_pr_level_pass_comment_status(
    host: CodeHost, repo: RepositoryRef, marker: str, pr_number: int, current_user: str
) -> bool | None:
    """Tri-state version of has_pr_level_pass_comment: None means the check itself was
    inconclusive (API failure), as opposed to a confirmed absence of the comment."""
    matches = await _matching_comments(host, repo, pr_number, current_user, lambda body: marker in body)
    return None if matches is None else bool(matches)


def is_design_review_comment(body: str) -> bool:
    return DESIGN_REVIEW_MARKER in body


async def has_design_review_comment(host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str) -> bool:
    """This is the design pass's *only* idempotency signal in review-requested (no
    persisted state there); self-review uses it only as defense-in-depth alongside its
    own persisted design_reviewed_prs state."""
    return await has_pr_level_pass_comment(host, repo, DESIGN_REVIEW_MARKER, pr_number, current_user)


async def check_design_review_comment_status(
    host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str
) -> bool | None:
    """Tri-state version of has_design_review_comment: None means the check itself was
    inconclusive (API failure), as opposed to a confirmed absence of the comment."""
    return await check_pr_level_pass_comment_status(host, repo, DESIGN_REVIEW_MARKER, pr_number, current_user)


def is_traceability_review_comment(body: str) -> bool:
    return TRACEABILITY_REVIEW_MARKER in body


async def has_traceability_review_comment(
    host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str
) -> bool:
    """Mirrors has_design_review_comment exactly (see requirement-traceability-requirements.md
    §7.4/§7.5): review-requested's only idempotency signal for this pass; self-review's
    defense-in-depth check alongside its own persisted traceability_reviewed_prs state.
    Also covers the "no linked ticket found" outcome, which is terminal (§7.3/§11.1) —
    any comment carrying this marker, including that one, counts as done."""
    return await has_pr_level_pass_comment(host, repo, TRACEABILITY_REVIEW_MARKER, pr_number, current_user)


async def check_traceability_review_comment_status(
    host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str
) -> bool | None:
    """Tri-state version of has_traceability_review_comment: None means the check itself
    was inconclusive (API failure), as opposed to a confirmed absence of the comment."""
    return await check_pr_level_pass_comment_status(host, repo, TRACEABILITY_REVIEW_MARKER, pr_number, current_user)


async def has_regret_review_comment(host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str) -> bool:
    """Mirrors has_design_review_comment / has_traceability_review_comment exactly
    (regret-review-requirements.md §7.4/§7.6): review-requested's only idempotency signal
    for this pass (no persisted state there); self-review's defense-in-depth check
    alongside its own persisted regret_reviewed_prs state (§7.3)."""
    return await has_pr_level_pass_comment(host, repo, REGRET_REVIEW_MARKER, pr_number, current_user)


async def check_regret_review_comment_status(
    host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str
) -> bool | None:
    """Tri-state version of has_regret_review_comment: None means the check itself was
    inconclusive (API failure), as opposed to a confirmed absence of the comment."""
    return await check_pr_level_pass_comment_status(host, repo, REGRET_REVIEW_MARKER, pr_number, current_user)


async def get_pr_level_flagged_locations(
    host: CodeHost, repo: RepositoryRef, marker: str, pr_number: int, current_user: str
) -> list[tuple[str, int]]:
    """Shared by every PR-level pass's flagged-locations fetch (design review,
    requirement-traceability review, ...), mirroring has_pr_level_pass_comment's
    extraction (requirement-traceability-requirements.md §7.6). (path, line) pairs
    already flagged by a prior inline comment containing marker on this PR. Reads the
    inline review threads directly and is meant to be called on every invocation, not
    just when deciding whether to skip the pass: a pass posts inline findings and the
    closing PR-level comment as separate calls, so a crash in between leaves the
    PR-level marker comment absent while inline comments already exist. Feeding those
    locations back into the prompt lets a retry avoid re-flagging them. Returns an empty
    list on an inconclusive API failure, rather than blocking the run — worst case a
    retry re-flags an already-posted location instead of the whole pass silently never
    running."""
    matches = await _matching_inline_comments(host, repo, pr_number, current_user, lambda body: marker in body)
    if not matches:
        return []
    return [(t.anchor.path, t.anchor.line) for _c, t in matches if t.anchor is not None and t.anchor.path]


async def get_design_review_flagged_locations(
    host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str
) -> list[tuple[str, int]]:
    return await get_pr_level_flagged_locations(host, repo, DESIGN_REVIEW_MARKER, pr_number, current_user)


async def get_traceability_review_flagged_locations(
    host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str
) -> list[tuple[str, int]]:
    return await get_pr_level_flagged_locations(host, repo, TRACEABILITY_REVIEW_MARKER, pr_number, current_user)


def _window_end(created_at: datetime) -> datetime:
    """``created_at`` + TRACEABILITY_COMMENT_WINDOW_SECONDS, truncated to whole seconds (the host's comment
    timestamps carry no finer resolution), so a comment at exactly the end of the window still counts."""
    return (created_at + timedelta(seconds=TRACEABILITY_COMMENT_WINDOW_SECONDS)).replace(microsecond=0)


async def _fetch_early_window_comments(
    host: CodeHost, repo: RepositoryRef, pr_number: int, pr_created_at: datetime, cache: dict | None = None
) -> list[Comment] | None:
    """All change-request-level comments on pr_number posted at or before the end of the
    early-comment window, regardless of author. Shared by build_early_comment_context and
    resolve_linked_tickets' comment-scan fallback (§7.2). When both are needed for the
    same PR in the same pass invocation, callers pass the same `cache` dict to each call
    so there's exactly one fetch, not two. Returns None on a fetch failure (as
    opposed to a confirmed-empty window), so callers can fail open without confusing "API
    call failed" with "no comments in the window". An unsupported capability stops."""
    if cache is not None and "early_window_comments" in cache:
        return cache["early_window_comments"]
    try:
        comments = list(await host.discussion.comments(repo, pr_number))
    except UnsupportedCapabilityError:
        raise
    except CodeHostError:
        logger.exception("PR #%d: reading the early comments failed", pr_number)
        result = None
    else:
        window_end = _window_end(pr_created_at)
        result = [c for c in comments if c.created_at <= window_end]
    if cache is not None:
        cache["early_window_comments"] = result
    return result


async def build_early_comment_context(
    host: CodeHost, repo: RepositoryRef, pr_number: int, pr_created_at: datetime, cache: dict | None = None
) -> str:
    """Concatenated bodies of every change-request-level comment (any author, including bots)
    posted within TRACEABILITY_COMMENT_WINDOW_SECONDS of pr_created_at
    (requirement-traceability-requirements.md §7.2). Included in the traceability prompt
    regardless of how the linked ticket was resolved — a clarifying comment posted right
    after opening is useful context even for a natively-linked ticket. Returns "" if there
    are none, or if the fetch itself fails (fails open — a missing early-comment fetch
    degrades the pass's context, not its correctness). Pass the same `cache` dict given to
    resolve_linked_tickets to reuse its comment-scan fallback's fetch instead of repeating
    it."""
    comments = await _fetch_early_window_comments(host, repo, pr_number, pr_created_at, cache)
    if not comments:
        return ""
    return "\n\n".join(c.body for c in comments if c.body)


@dataclass(frozen=True)
class LinkedTicket:
    """A ticket a change request answers, as the traceability prompt renders it.

    ``repo`` is display text only (``Repository.display_name`` of a work item's repository, or ``"Linear"``).
    ``source`` is ``"closing_keyword"``, ``"comment"`` or ``"linear_linkback"``.
    """

    number: int | str
    repo: str
    title: str
    body: str
    source: str


async def _ticket_from_work_item(host: CodeHost, item: WorkItem, source: str) -> LinkedTicket:
    return LinkedTicket(
        number=item.number,
        repo=await host.repository.display_name(item.repository),
        title=item.title,
        body=item.body,
        source=source,
    )


async def _resolve_native_linked_tickets(
    host: CodeHost, repo: RepositoryRef, pr: ChangeRequest
) -> list[LinkedTicket] | None:
    """Primary mechanism (§7.2 step 1): the work items the host records as closed by the change request
    (``WorkItems.linked_work_items``; each may live in a different repository from the change request's own).
    Items the host confirms absent are not returned. Returns None — rather than the confirmed-empty `[]` — if the
    read failed inconclusively (rate limit, transient 5xx), so resolve_linked_tickets can tell "no ticket" apart
    from "don't know yet" instead of treating a transient API hiccup as proof there's no linked ticket. An
    unsupported capability stops (policy ``traceability``)."""
    try:
        items = await host.work_items.linked_work_items(repo, pr.number)
    except UnsupportedCapabilityError:
        raise
    except CodeHostError:
        logger.exception("PR #%d: reading the linked work items failed", pr.number)
        return None
    tickets: list[LinkedTicket] = []
    seen: set[tuple[RepositoryRef, int]] = set()
    for item in items:
        key = (item.repository, item.number)
        if key in seen:
            continue
        seen.add(key)
        tickets.append(await _ticket_from_work_item(host, item, "closing_keyword"))
    return tickets


def _match_identity_and_number(match: re.Match) -> tuple[str | None, int | None]:
    """The repository identity text and issue number a reference names. Identity None means the change request's
    own repository (a bare ``#N``)."""
    if match.group("num1"):
        return f"{match.group('owner1')}/{match.group('repo1')}", int(match.group("num1"))
    if match.group("num2"):
        return f"{match.group('owner2')}/{match.group('repo2')}", int(match.group("num2"))
    if match.group("num3"):
        return None, int(match.group("num3"))
    return None, None


def _parse_linear_linkback_comment(body: str) -> LinkedTicket | None:
    """Parses a `linear[bot]` linkback comment (identified by LINEAR_LINKBACK_MARKER) into
    a LinkedTicket shaped like the work-item tickets _resolve_native_linked_tickets/the
    rest of _resolve_comment_linked_tickets produce. No Linear API call is made — the bot
    already embeds the ticket's title and full description directly in the comment body it
    posts to GitHub, which is enough to synthesize the "formal requirement"
    review-traceability.md needs. Returns None if the marker is present but the expected
    `<summary><a href=...>` shape isn't found (unrecognized/changed bot output) — treated
    by the caller the same as "no ticket in this comment", not as an inconclusive lookup,
    since there's no API failure to distinguish from a confirmed absence here."""
    if LINEAR_LINKBACK_MARKER not in body:
        return None
    match = _LINEAR_LINKBACK_RE.search(body)
    if match is None:
        return None
    detail_body = body[match.end() :].split("<!-- linear-review-link -->", 1)[0]
    detail_body = re.sub(r"</?(?:details|p)>", "", detail_body).strip()
    return LinkedTicket(
        number=match.group("ticket_id"),
        repo="Linear",
        title=match.group("title").strip(),
        body=detail_body,
        source="linear_linkback",
    )


async def _resolve_ref_repo(
    host: CodeHost, repo: RepositoryRef, identity: str | None, resolved: dict[str, RepositoryRef | None]
) -> RepositoryRef | None:
    """The repository a reference names, or None when it can't be resolved. Results are cached in ``resolved``
    per identity. ``UnsupportedCapabilityError`` propagates (stop policy); other ``CodeHostError`` means None."""
    if identity is None:
        return repo
    if identity in resolved:
        return resolved[identity]
    try:
        found: RepositoryRef | None = await host.repository.resolve(identity)
    except UnsupportedCapabilityError:
        raise
    except CodeHostError:
        found = None
    resolved[identity] = found
    return found


async def _resolve_comment_issue_refs(
    host: CodeHost, repo: RepositoryRef, body: str, seen: set[tuple[RepositoryRef | str, int | str]]
) -> list[LinkedTicket]:
    """Scans a single comment body for issue references and resolves each via
    ``WorkItems.get``, split out of _resolve_comment_linked_tickets purely to keep that
    function's branching complexity down. A match that is absent, names a pull request rather
    than an issue, or fails to resolve for any other reason (rate limit, transient 5xx) is
    dropped rather than treated as a ticket — unlike the native
    mechanism, a regex match in free-text comments is already a guess, so an API failure
    here doesn't warrant the same inconclusive-vs-absent distinction resolve_linked_tickets
    makes for closing-keyword references. An unsupported capability stops. A reference
    that names another repository is resolved with ``Repository.resolve`` on the text
    of the reference. `seen` is shared and mutated across every
    comment in the scan, so a ticket already found (by this or the Linear-linkback path)
    isn't re-fetched or duplicated."""
    tickets: list[LinkedTicket] = []
    resolved: dict = {}  # per-call cache: one resolve per distinct identity in this body
    for match in _ISSUE_REF_RE.finditer(body):
        identity, number = _match_identity_and_number(match)
        if number is None:
            continue
        ticket_repo = await _resolve_ref_repo(host, repo, identity, resolved)
        if ticket_repo is None:
            continue
        key = (ticket_repo, number)
        if key in seen:
            continue
        try:
            item = await host.work_items.get(ticket_repo, number)
        except UnsupportedCapabilityError:
            raise
        except CodeHostError:
            continue
        if item is None:
            continue
        seen.add(key)
        tickets.append(await _ticket_from_work_item(host, item, "comment"))
    return tickets


async def _resolve_comment_linked_tickets(
    host: CodeHost, repo: RepositoryRef, pr: ChangeRequest, cache: dict | None = None
) -> list[LinkedTicket]:
    """Fallback mechanism (§7.2 step 2), only ever called when native linking finds
    nothing: scan every early-window comment (any author, including bots — §1 decision 4)
    for a Linear linkback (_parse_linear_linkback_comment) or an issue reference
    (_resolve_comment_issue_refs). A comment recognized as a Linear linkback is not also
    scanned for issue references, since it's bot-authored structured output, not
    free text a manual `#N` mention could appear in."""
    comments = await _fetch_early_window_comments(host, repo, pr.number, pr.created_at, cache)
    if not comments:
        return []
    tickets: list[LinkedTicket] = []
    seen: set[tuple[RepositoryRef | str, int | str]] = set()
    for comment in comments:
        body = comment.body or ""
        linear_ticket = _parse_linear_linkback_comment(body)
        if linear_ticket is not None:
            key = (linear_ticket.repo, linear_ticket.number)
            if key not in seen:
                seen.add(key)
                tickets.append(linear_ticket)
            continue
        tickets.extend(await _resolve_comment_issue_refs(host, repo, body, seen))
    return tickets


async def resolve_linked_tickets(
    host: CodeHost, repo: RepositoryRef, pr: ChangeRequest, cache: dict | None = None
) -> list[LinkedTicket] | None:
    """Resolve the ticket(s) this PR answers (requirement-traceability-requirements.md
    §7.2, §4/G5 update): the host's own closing-keyword linking is tried first and, only if
    it finds nothing, an early-comment scan is tried as a fallback — a `linear[bot]`
    linkback comment first, then an issue reference (see
    _resolve_comment_linked_tickets). Returns a possibly-empty list of LinkedTicket,
    `source` being "closing_keyword", "comment", or "linear_linkback" — or None if the
    native mechanism could not read the linked work items due to an API
    failure rather than a confirmed absence (see
    _resolve_native_linked_tickets). Callers must treat None as "inconclusive, retry next
    run" and never as "no linked ticket" — the comment-scan fallback is skipped in that
    case rather than risked as a second source of false negatives. Pass the same `cache`
    dict to a later build_early_comment_context call for this PR so its comment-scan
    fallback's fetch (if it ran) is reused instead of repeated."""
    tickets = await _resolve_native_linked_tickets(host, repo, pr)
    if tickets is None:
        return None
    if tickets:
        return tickets
    return await _resolve_comment_linked_tickets(host, repo, pr, cache)


async def post_no_linked_ticket_comment(host: CodeHost, repo: RepositoryRef, pr_number: int) -> bool:
    """Posts the §7.3 "no linked ticket found" PR-level comment directly — no backend
    invocation, since there's nothing for a model to judge when resolve_linked_tickets
    found nothing via either mechanism. This outcome is terminal (§7.3/§11.1): once
    posted, has_traceability_review_comment treats this PR as done forever. Returns True
    if the comment was posted successfully. A failed post (including a timeout) is logged
    and gives False: a write is never retried and has no fallback post. An unsupported
    capability follows the ``traceability`` policy for ``Discussion.post_comment``
    (unlisted: stop) and propagates."""
    body = (
        "# Requirement Traceability\n"
        "No linked ticket found (no closing-keyword link and no issue reference in the "
        "first 5 minutes of comments) — skipping scope/gap comparison.\n"
        f"{TRACEABILITY_REVIEW_MARKER}"
    )
    try:
        await host.discussion.post_comment(repo, pr_number, body)
    except UnsupportedCapabilityError:
        raise
    except CodeHostError as exc:
        logger.error("PR #%d: failed to post 'no linked ticket found' comment: %s", pr_number, exc)  # noqa: TRY400
        return False
    return True


async def run_pr_level_pass(
    pr_number: int,
    backend: Backend,
    wdir: str,
    is_done: bool,
    build_prompt: Callable[[], Awaitable[str]],
    has_comment_fn: Callable[[], Awaitable[bool | None]],
    label: str,
    short_circuit: Callable[[], Awaitable[bool | None]] | None = None,
) -> bool | None:
    """Shared orchestration skeleton for every PR-level pass (design review, requirement-
    traceability review, ...): noop-if-already-done → optional short-circuit (e.g.
    traceability's "no linked ticket" outcome, which needs no backend invocation) →
    build prompt → run backend → re-verify the completion marker actually landed. Both
    runners' `_run_design_review`/`_run_traceability_review` were structurally identical
    copies of this skeleton (only the instructions file, flagged-locations getter, prompt
    builder, and traceability's ticket short-circuit differed) — extracted here so a 5th
    PR-level pass (dotharness#4) doesn't have to copy it a 5th time.

    `has_comment_fn` may return None for an inconclusive check (API failure) as opposed to
    a confirmed absence — that distinction is preserved in this function's own return value
    (None vs False) rather than collapsed, since self_review.py's callers must not persist
    "done" state on an inconclusive check the same way they must not on a confirmed miss,
    but still want to log the two cases differently. review_requested.py's checkers never
    return None (no persisted state to protect there), so that branch is simply never hit
    for its callers.

    Returns True if the pass is confirmed complete (already done, short-circuited to
    success, or backend ran and the marker was confirmed), False if it's confirmed not
    complete, or None if a post-backend completion check was inconclusive.
    """
    if is_done:
        logger.info("PR #%d: %s already posted, skipping", pr_number, label)
        return True
    if short_circuit is not None:
        outcome = await short_circuit()
        if outcome is not None:
            return outcome
    prompt = await build_prompt()
    try:
        result = await backend.run(prompt, cwd=wdir, context=f"PR #{pr_number} {label}")
        if result.returncode != 0:
            logger.error("PR #%d: %s backend exited %d", pr_number, label, result.returncode)
            return False
    except subprocess.TimeoutExpired:
        logger.exception("PR #%d: %s backend timed out", pr_number, label)
        return False
    comment_status = await has_comment_fn()
    if comment_status is None:
        logger.warning(
            "PR #%d: could not confirm %s comment status (comment check failed) — "
            "treating as unconfirmed rather than assuming it's missing",
            pr_number,
            label,
        )
        return None
    if not comment_status:
        logger.error(
            "PR #%d: %s backend exited 0 but no %s comment found — treating as failure",
            pr_number,
            label,
            label,
        )
        return False
    return True


def _build_prior_findings_section(
    prior_flagged_locations: list[tuple[str, int]], findings_label: str, comment_label: str
) -> str:
    """Shared by every PR-level pass's prompt builder (design review, requirement-
    traceability review, ...): renders prior_flagged_locations as a "## Already-flagged
    ... findings" section telling the backend not to re-flag them, or "" when empty."""
    if not prior_flagged_locations:
        return ""
    locations = "\n".join(f"- {path}:{line}" for path, line in prior_flagged_locations)
    return (
        f"\n\n## Already-flagged {findings_label} findings\n"
        f"The following file/line locations already have an inline {comment_label} comment "
        f"from a previous attempt on this PR. Do not post a new inline comment for any of "
        f"them:\n{locations}"
    )


def _build_pr_metadata_trailer(
    pr_url: str,
    pr_number: int,
    repo_name: str,
    commit_sha: str,
    pr_description: str | None,
    vibe_heal_context: str | None,
) -> str:
    """Shared by every PR-level pass's prompt builder: the PR URL/number/repo/commit
    line, plus the optional description and static-analysis sections that follow it."""
    return (
        f"\n\nPR URL: {pr_url}\nPR number: {pr_number}\n"
        f"Repo: {repo_name}\nCommit: {commit_sha}"
        + (f"\n\n{pr_description}" if pr_description else "")
        + (f"\n\n## Static Analysis\n{vibe_heal_context}" if vibe_heal_context else "")
    )


def build_traceability_review_prompt(
    traceability_instructions: str,
    extra_knowledge: str | None,
    diff_sections: str,
    pr: ChangeRequest,
    pr_number: int,
    repo_name: str,
    commit_sha: str,
    pr_description: str | None,
    vibe_heal_context: str | None,
    prior_flagged_locations: list[tuple[str, int]],
    tickets: list[LinkedTicket],
    early_comment_context: str,
) -> str:
    """Shared by self_review.py and review_requested.py, mirroring
    build_design_review_prompt's shape (requirement-traceability-requirements.md §7.1).
    Only ever called once resolve_linked_tickets has already found at least one ticket —
    callers post the "no linked ticket found" comment directly instead of building this
    prompt when there are none (§7.3). `tickets` and `early_comment_context` become the
    `## Linked Ticket(s)`/`## Early PR Comments` sections review-traceability.md expects
    as input; prior_flagged_locations becomes `## Already-flagged scope-creep findings`,
    same shape as build_design_review_prompt's equivalent section."""
    tickets_section = "\n\n## Linked Ticket(s)\n" + "\n\n".join(
        f"### #{t.number} ({t.repo}) — source: {t.source}\n**Title:** {t.title}\n\n{t.body}" for t in tickets
    )
    early_comments_section = f"\n\n## Early PR Comments\n{early_comment_context}" if early_comment_context else ""
    prior_findings_section = _build_prior_findings_section(
        prior_flagged_locations, "scope-creep", "traceability-review"
    )
    return (
        traceability_instructions
        + (f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else "")
        + tickets_section
        + early_comments_section
        + diff_sections
        + prior_findings_section
        + _build_pr_metadata_trailer(pr.url, pr_number, repo_name, commit_sha, pr_description, vibe_heal_context)
    )


def author_matches(login: str, authors_config: str | list) -> bool:
    if authors_config == "*":
        return True
    return login in authors_config


async def list_open_change_requests_matching_authors(
    host: CodeHost, repo: RepositoryRef, authors_config: str | list
) -> list[ChangeRequest] | None:
    """List open, non-draft change requests whose author matches authors_config, sorted ascending by number.

    Returns None (rather than []) when the host read fails (logged), so callers can distinguish "the list failed"
    from "confirmed zero open change requests" instead of treating a transient hiccup as an authoritative empty
    set. An unsupported capability follows the ``review_prs`` policy for ``ChangeRequests.list`` (unlisted: stop)
    and propagates.
    """

    async def list_open() -> list[ChangeRequest]:
        return list(await host.change_requests.list(repo, state=ChangeRequestState.OPEN))

    try:
        found = await call_with_policy("review_prs", "ChangeRequests.list", list_open, [])
    except UnsupportedCapabilityError:
        raise
    except CodeHostError as e:
        logger.error("Failed to list PRs: %s", e)  # noqa: TRY400
        return None
    eligible = [
        c for c in found if not c.is_draft and author_matches(c.author.login if c.author else "", authors_config)
    ]
    return sorted(eligible, key=lambda c: c.number)


async def get_pr_base_branch(host: CodeHost, repo: RepositoryRef, pr_number: int) -> str:
    """Base branch name, or "" when the read failed (logged)."""
    pr = await get_change_request(host, repo, pr_number)
    return "" if pr is None else pr.base_reference


async def get_pr_head_sha(host: CodeHost, repo: RepositoryRef, pr_number: int) -> str:
    """Head revision, or "" when the read failed (logged)."""
    pr = await get_change_request(host, repo, pr_number)
    return "" if pr is None else pr.head_revision


async def get_pr_description(host: CodeHost, repo: RepositoryRef, pr_number: int) -> str:
    """The prompt's "## PR Description" block (title and body), or "" when the read failed (logged)."""
    pr = await get_change_request(host, repo, pr_number)
    if pr is None:
        return ""
    return f"## PR Description\n**Title:** {pr.title}\n\n{pr.body or ''}".strip()


async def is_ancestor(candidate_sha: str, descendant_sha: str, wdir: str, env: dict) -> bool:
    result = await run_cmd(
        ["git", "merge-base", "--is-ancestor", candidate_sha, descendant_sha],
        cwd=wdir,
        env=env,
        timeout=TIMEOUT_GIT,
        check=False,
    )
    return result.returncode == 0


async def get_changed_files(
    base_branch: str, wdir: str, env: dict, expected_sha: str = "", *, rev_range: str | None = None
) -> list[str]:
    """Names of files changed in the review range. ``rev_range`` overrides the default
    ``origin/{base_branch}...HEAD`` (local-review passes ``<merge_base>..HEAD``)."""
    if expected_sha:
        actual = await get_head_sha(wdir, env)
        if actual and actual != expected_sha:
            logger.warning("get_changed_files: local HEAD %s differs from expected %s", actual, expected_sha)
    result = await run_cmd(
        ["git", "diff", "--name-only", rev_range or f"origin/{base_branch}...HEAD"],
        cwd=wdir,
        env=env,
        timeout=TIMEOUT_GIT,
        check=False,
    )
    if result.returncode != 0:
        logger.error(
            "get_changed_files failed (exit %d): %s",
            result.returncode,
            result.stderr.decode("utf-8", errors="replace")[:500],
        )
        return []
    return [f for f in result.stdout.decode("utf-8").splitlines() if f.strip()]


async def get_file_diff(file_path: str, base_branch: str, wdir: str, env: dict, *, rev_range: str | None = None) -> str:
    """Per-file diff. ``rev_range`` overrides the default ``origin/{base_branch}...HEAD``;
    it is split on whitespace into separate git arguments (local-review passes
    ``"<merge_base> HEAD"``)."""
    range_args = rev_range.split() if rev_range else [f"origin/{base_branch}...HEAD"]
    result = await run_cmd(
        ["git", "diff", *range_args, "--", file_path],
        cwd=wdir,
        env=env,
        timeout=TIMEOUT_GIT,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8")


def count_diff_lines(diff: str) -> int:
    """The number of added + removed lines in a unified diff: every line starting with
    + or -, excluding the +++/--- file headers. The same count build_file_review_section
    uses to decide whether a diff covers most of a file, extracted so consumers that gate
    on changed-line size (regret-review's bugfix-shaped gate) share the rule instead of
    re-implementing it."""
    return sum(1 for line in diff.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))


def build_file_review_section(file: str, diff: str, abs_path: str) -> str:
    """Return the file section for the review prompt.

    If the file is new or the diff covers ≥75% of the file's lines, omit the diff
    and ask the reviewer to look at the full file instead.
    """
    file_ref = f"@{abs_path}" if os.path.exists(abs_path) else ""

    omit = False
    reason = ""

    if file_ref:
        if "--- /dev/null" in diff:
            omit = True
            reason = "This is a new file — the diff is the entire file."
        else:
            diff_lines = count_diff_lines(diff)
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as fh:
                    total_lines = sum(1 for _ in fh)
            except Exception:
                total_lines = 0
            if total_lines > 0 and diff_lines >= total_lines * 3 / 4:
                omit = True
                pct = diff_lines * 100 // total_lines
                reason = f"The diff touches {diff_lines} of {total_lines} lines ({pct}% of the file)."

    if omit:
        note = f"Note: {reason} The diff has been omitted; please review the whole file instead."
        return f"\n\n{file_ref}\n\n## File: {file}\n{note}"
    return f"\n\n{file_ref}\n\n## Diff for {file}\n{diff}"


def build_design_review_prompt(
    design_instructions: str,
    extra_knowledge: str | None,
    diff_sections: str,
    pr: ChangeRequest,
    pr_number: int,
    repo_name: str,
    commit_sha: str,
    pr_description: str | None,
    vibe_heal_context: str | None,
    prior_flagged_locations: list[tuple[str, int]],
) -> str:
    """Shared by self_review.py and review_requested.py so the design pass's prompt
    shape lives in one place rather than being assembled twice.

    prior_flagged_locations (design-review-requirements.md §7.1) is required, not
    optional: callers must fetch it via get_design_review_flagged_locations on every
    invocation, since it's what lets a retry after a partial failure skip re-flagging
    inline findings it already posted."""
    prior_findings_section = _build_prior_findings_section(prior_flagged_locations, "design", "design-review")
    return (
        design_instructions
        + (f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else "")
        + diff_sections
        + prior_findings_section
        + _build_pr_metadata_trailer(pr.url, pr_number, repo_name, commit_sha, pr_description, vibe_heal_context)
    )


def get_vibe_heal_context(subdirs: list[SubDir], working_dir: str, branch: str) -> str:
    if not subdirs:
        return ""
    seen_keys: set[str] = set()
    parts: list[str] = []
    for subdir in subdirs:
        props_path = Path(working_dir) / subdir.path / "sonar-project.properties"
        project_key = _read_sonar_project_key(props_path)
        if not project_key or project_key in seen_keys:
            continue
        seen_keys.add(project_key)
        review_path = Path.home() / ".vibe-heal" / "reviews" / project_key / branch / "review.md"
        cleaned = _read_vibe_heal_review(review_path, project_key)
        if cleaned:
            parts.append(cleaned)
    return "\n\n".join(parts)


def _read_vibe_heal_review(review_path: Path, project_key: str) -> str | None:
    reviews_base = (Path.home() / ".vibe-heal" / "reviews" / project_key).resolve()
    try:
        review_path.resolve().relative_to(reviews_base)
    except ValueError:
        return None
    if not review_path.exists():
        return None
    try:
        cleaned = review_path.read_text(encoding="utf-8")
    except Exception:
        return None
    while True:
        new_cleaned = re.sub(r"<details>.*?</details>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
        if new_cleaned == cleaned:
            break
        cleaned = new_cleaned
    return re.sub(r"</?details>", "", cleaned, flags=re.IGNORECASE).strip()


def _read_sonar_project_key(props_path: Path) -> str | None:
    if not props_path.exists():
        return None
    try:
        for line in props_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("sonar.projectKey="):
                value = stripped.split("=", 1)[1].strip()
                if not value:
                    return None
                if "/" in value or "\\" in value or ".." in value:
                    return None
                return value
    except Exception:  # noqa: S110
        pass
    return None


async def fetch_pr_comments(pr_number: int, script_path: Path, wdir: str, env: dict) -> list[dict]:
    """Run scripts/pr-comments.py fetch for pr_number and return its cached comments.

    Returns a flat list of dicts, each tagged with a "type" key ("inline", "review", or
    "issue"). Bot-authored issue comments are excluded; inline and review comments are not.
    """
    # A per-call temp file (not the shared ~/.harness/cache/pr-<n>-comments.json, which has no repo
    # component) so concurrent runs on different repos with the same PR number can't read each other's data.
    with tempfile.TemporaryDirectory(prefix="harness-pr-comments-") as tmp_dir:
        output_file = Path(tmp_dir) / "comments.json"
        result = await run_cmd(
            ["python3", str(script_path), "fetch", "--pr", str(pr_number), "--output", str(output_file)],
            cwd=wdir,
            env=env,
            timeout=TIMEOUT_FETCH_COMMENTS,
            check=False,
        )
        if result.returncode != 0:
            logger.warning("PR #%d: failed to fetch comments: %s", pr_number, result.stderr)
            return []

        if not output_file.exists():
            logger.warning("PR #%d: comments output file not found after fetch", pr_number)
            return []

        data = json.loads(output_file.read_text(encoding="utf-8"))
    comments: list[dict] = []

    for c in data.get("inline_comments", []):
        comments.append({"type": "inline", **c})

    for c in data.get("review_comments", []):
        comments.append({"type": "review", **c})

    for c in data.get("issue_comments", []):
        author = c.get("author", "")
        if not author.endswith("[bot]"):
            comments.append({"type": "issue", **c})

    logger.debug(
        "PR #%d: cache has %d inline / %d review / %d issue comment(s); %d actionable total",
        pr_number,
        len(data.get("inline_comments", [])),
        len(data.get("review_comments", [])),
        len(data.get("issue_comments", [])),
        len(comments),
    )
    return comments


async def fetch_review_threads(host: CodeHost, repo: RepositoryRef, pr_number: int) -> list[ReviewThread] | None:
    """Every review thread of a PR (all pages, with ids, resolved state and comments), or None if the host
    could not answer. An unsupported capability stops."""
    threads = await _read_or_none(
        lambda: host.discussion.review_threads(repo, pr_number), "review thread state", pr_number
    )
    return None if threads is None else list(threads)


def merge_thread_state(comments: list[dict], threads: Sequence[ReviewThread]) -> None:
    """Add thread_id, thread_resolved and is_minimized to each inline comment that is the
    root comment of one of `threads` (matched on the host comment id, which is the id the
    comments cache carries). Mutates the comment dicts in place. Other comments, and
    threads whose root is not in `comments`, are left alone. No node_id is set: the host
    comment id is not a GraphQL node id, so the prompt shows "?" and skips that mutation."""
    by_root = {t.comments[0].id: (t, t.comments[0]) for t in threads if t.comments}
    for comment in comments:
        if comment.get("type") != "inline":
            continue
        found = by_root.get(str(comment.get("id")))
        if found is None:
            continue
        thread, root = found
        comment["thread_id"] = thread.id
        comment["thread_resolved"] = thread.is_resolved
        comment["is_minimized"] = root.is_hidden


def find_reply_with_marker(comment: dict, marker: str = FOCUSED_REVIEW_MARKER) -> dict | None:
    """Return the first reply on `comment` whose body contains `marker`, or None."""
    for reply in comment.get("replies") or []:
        if marker in reply.get("body", ""):
            return reply
    return None


def find_last_reply_if_marked(comment: dict, marker: str = FOCUSED_REVIEW_MARKER) -> dict | None:
    """Return `comment`'s LAST reply if it carries `marker`, or None.

    Deliberately last-reply-only, unlike `find_reply_with_marker`'s first-match scan: once
    anything is posted after the marker reply (our own completion note, a further human
    comment), the thread is no longer gated — checking the last reply is what lets an
    addressed thread stop re-triggering.
    """
    replies = comment.get("replies") or []
    if replies and marker in replies[-1].get("body", ""):
        return replies[-1]
    return None
