"""GitHub Review Client for posting SonarQube reviews to GitHub PRs."""

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import urlparse

from dotharness.subprocess_runner import gh_argv
from dotharness.vibe_heal.ai_tools.utils import CommandResult, run_command
from dotharness.vibe_heal.output import warn
from dotharness.vibe_heal.review.models import FileReview, ReviewIssue, ReviewResult
from dotharness.vibe_heal.review.reporter import format_coverage_table

logger = logging.getLogger(__name__)


class GitHubReviewError(Exception):
    """Error during GitHub review operations."""


class NoOpenPrError(GitHubReviewError):
    """Raised when there is no open PR for the current branch."""


class GitHubTimeoutError(GitHubReviewError):
    """Raised when a gh API call times out.

    The request may already have been processed by GitHub, so callers must not
    blindly retry (e.g. via a fallback post) as that could duplicate the review.
    """


_MAX_INLINE_COMMENTS = 50
_MAX_REVIEW_BODY_CHARS = 60_000  # GitHub's limit is ~65536; leave headroom
_TRUNCATION_NOTICE = "\n\n_...review body truncated to fit GitHub's size limit._"


def _truncate_body(body: str) -> str:
    """Truncate a review body so it fits within GitHub's size limit."""
    if len(body) <= _MAX_REVIEW_BODY_CHARS:
        return body
    return body[: _MAX_REVIEW_BODY_CHARS - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE


_MAX_OVERFLOW_BODY_LINES = 100


@asynccontextmanager
async def _deadline(cmd: list[str], seconds: float) -> AsyncGenerator[None]:
    """Bound the enclosed block by a deadline, converting expiry into GitHubReviewError.

    The caller owns the timeout; run_command kills the child on timeout/cancellation,
    so nothing is orphaned.
    """
    try:
        async with asyncio.timeout(seconds):
            yield
    except TimeoutError:
        msg = f"`{cmd[0]} {cmd[1] if len(cmd) > 1 else ''}`".strip() + f" timed out after {seconds:g}s"
        raise GitHubReviewError(msg) from None


async def _run_gh(cmd: list[str], seconds: float) -> CommandResult:
    """Run a command under a caller-chosen deadline."""
    async with _deadline(cmd, seconds):
        return await run_command(cmd)


class GitHubReviewClient:
    """Posts SonarQube review results as inline comments on GitHub PRs.

    Uses the `gh` CLI for all GitHub API interactions.
    """

    async def validate_installed(self) -> None:
        """Check that the gh CLI is installed and accessible.

        Raises:
            OSError: If gh is not installed or not in PATH.
        """
        try:
            result = await _run_gh(gh_argv("--version"), 10)
        except OSError as exc:
            msg = "gh CLI is not installed or not in PATH. Install it from https://cli.github.com/"
            raise OSError(msg) from exc
        if not result.success:
            msg = "gh CLI is not installed or not in PATH. Install it from https://cli.github.com/"
            raise OSError(msg)

    async def _view_pr_field(self, field: str) -> CommandResult:
        """Run `gh pr view --json <field>` for the current branch's PR."""
        return await _run_gh(gh_argv("pr", "view", "--json", field), 30)

    async def detect_pr(self, pr_number: int | None = None) -> int:
        """Get the PR number for review posting.

        Args:
            pr_number: Explicit PR number. If provided, returned directly.

        Returns:
            The PR number.

        Raises:
            OSError: If gh CLI is not installed.
            NoOpenPrError: If no open PR exists for the current branch.
            GitHubReviewError: If auto-detection fails for other reasons.
        """
        if pr_number is not None:
            return pr_number
        await self.validate_installed()

        result = await self._view_pr_field("number")
        if not result.success:
            stderr = result.stderr.strip()
            if "no pull requests found" in stderr.lower():
                msg = "No open pull request found for the current branch"
                raise NoOpenPrError(msg)
            msg = f"Failed to detect PR: {stderr or 'not authenticated'}"
            raise GitHubReviewError(msg)

        data: dict[str, object] = json.loads(result.stdout)
        return cast(int, data["number"])

    async def detect_pr_base_branch(self) -> str | None:
        """Detect the base branch of the open PR for the current branch.

        Returns None when gh is not installed, the call fails, there is no
        open PR for the current branch, or the response is malformed.
        Unlike detect_pr(), this method never raises — callers should treat
        None as "fall back to whatever default base branch they already use."
        This asymmetry is deliberate: detect_pr() is used when posting,
        where "no PR" is genuinely actionable; base-branch detection is a
        best-effort enhancement that must silently degrade.
        """
        try:
            result = await self._view_pr_field("baseRefName")
        except Exception:
            return None

        if not result.success:
            return None

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict):
            return None

        base_ref = data.get("baseRefName")
        return cast(str, base_ref) if isinstance(base_ref, str) else None

    async def post_review(self, pr_number: int, report: ReviewResult) -> None:
        """Post a review with inline comments on a GitHub PR.

        Builds a single GitHub review payload with per-file inline comments.
        If the inline review fails (e.g., lines outside the diff), falls back
        to posting a top-level comment with all issues.

        Args:
            pr_number: The PR number to post the review on.
            report: The SonarQube review result containing issues.

        Raises:
            OSError: If gh CLI is not installed.
            GitHubTimeoutError: If the inline post times out (no fallback, to avoid duplicates).
            GitHubReviewError: If both the inline review and fallback fail.
        """
        await self.validate_installed()
        owner_repo = await self._get_owner_repo()
        payload = self.build_payload(report)

        try:
            await self._post_json(
                gh_argv(
                    "api",
                    "--method",
                    "POST",
                    f"/repos/{owner_repo}/pulls/{pr_number}/reviews",
                    "--input",
                    "-",
                ),
                payload,
            )
        except GitHubTimeoutError:
            # The review may already exist on GitHub; falling back would risk a duplicate.
            raise
        except GitHubReviewError as inline_err:
            logger.warning(
                "Inline review failed (%s) — falling back to top-level comment",
                inline_err,
            )
            warn(f"Warning: inline review rejected by GitHub ({inline_err}) — posting as a top-level comment instead.")
            fallback = self._build_fallback_payload(report)
            try:
                await self._post_json(
                    gh_argv(
                        "api",
                        "--method",
                        "POST",
                        f"/repos/{owner_repo}/pulls/{pr_number}/reviews",
                        "--input",
                        "-",
                    ),
                    fallback,
                )
            except GitHubReviewError as e:
                msg = f"Review posting failed: {e}"
                raise GitHubReviewError(msg) from e

    def build_payload(self, report: ReviewResult) -> dict[str, Any]:
        """Build the GitHub review payload with inline comments."""
        comments: list[dict[str, Any]] = []
        nearby_lines: list[str] = []
        for file_review in report.files:
            self._collect_file_comments(file_review, report.base_branch, comments, nearby_lines)

        comments, overflow = comments[:_MAX_INLINE_COMMENTS], comments[_MAX_INLINE_COMMENTS:]

        body_parts = [self._build_summary(report, len(comments), len(overflow), len(nearby_lines))]
        if nearby_lines:
            body_parts.append(
                "**Issues near changed lines** (outside diff — shown here instead of inline):\n"
                + "\n".join(nearby_lines)
            )
        if overflow:
            body_parts.append(self._build_overflow_section(overflow))
        files_with_coverage = [fr for fr in report.files if fr.coverage_pct is not None]
        if files_with_coverage:
            body_parts.append("**Coverage on changed lines:**\n\n" + format_coverage_table(files_with_coverage))
        return {"event": "COMMENT", "body": _truncate_body("\n\n".join(body_parts)), "comments": comments}

    @staticmethod
    def _build_summary(report: ReviewResult, inline: int, capped: int, nearby: int) -> str:
        label_suffix = f" [{report.project_label}]" if report.project_label else ""
        sha_suffix = f" (commit {report.head_sha[:7]})" if report.head_sha else ""
        total = inline + capped + nearby
        if not total:
            return f"SonarQube{label_suffix}: no findings on or near changed lines.{sha_suffix}"
        counts = [(inline, "inline"), (capped, "capped"), (nearby, "near changed lines")]
        breakdown = ", ".join(f"{n} {name}" for n, name in counts if n)
        return f"SonarQube{label_suffix}: {total} finding(s) ({breakdown}).{sha_suffix}"

    @staticmethod
    def _build_overflow_section(overflow: list[dict[str, Any]]) -> str:
        visible = overflow[:_MAX_OVERFLOW_BODY_LINES]
        overflow_lines = [f"- **{c['path']}:{c['line']}** {c['body'].splitlines()[0]}" for c in visible]
        extra = len(overflow) - len(visible)
        suffix = f"\n_...and {extra} more not shown._" if extra else ""
        return (
            f"**{len(overflow)} finding(s) over the {_MAX_INLINE_COMMENTS}-comment inline limit**"
            f" (capped to avoid GitHub rate limits):\n" + "\n".join(overflow_lines) + suffix
        )

    def _build_issue_body(self, issue: ReviewIssue) -> str:
        body = f"**{issue.rule}** {issue.message}"
        if issue.doc_url:
            body += f"\n\n{issue.doc_url}"
        if issue.root_cause:
            body += (
                f"\n\n<details>\n\n<summary>{issue.rule} — why this matters</summary>"
                f"\n\n{issue.root_cause}\n\n</details>"
            )
        return body

    def _collect_file_comments(
        self,
        file_review: FileReview,
        base_branch: str,
        comments: list[dict[str, Any]],
        nearby_lines: list[str],
    ) -> None:
        for issue in file_review.issues:
            body = self._build_issue_body(issue)
            if issue.on_changed_line:
                comments.append({"path": file_review.file_path, "line": issue.line, "side": "RIGHT", "body": body})
            else:
                nearby_lines.append(f"- **{issue.rule}** ({file_review.file_path}:{issue.line}) {issue.message}")
        for dup in file_review.duplications:
            locations = ", ".join(
                f"`{loc.file_path}` lines {loc.from_line}-{loc.to_line}" for loc in dup.other_locations
            )
            body = (
                f"**Duplication detected** (lines {dup.from_line}-{dup.to_line})\n\n"
                f"This block is duplicated in: {locations}"
            )
            # Use anchor_line (a changed line in the diff) so the GitHub API
            # accepts the comment; fall back to from_line for older reports.
            anchor = dup.anchor_line if dup.anchor_line is not None else dup.from_line
            comments.append({"path": file_review.file_path, "line": anchor, "side": "RIGHT", "body": body})
        for res in file_review.resolved_duplications:
            other = "\n".join(f"- `{loc.file_path}` lines {loc.from_line}-{loc.to_line}" for loc in res.other_locations)
            body = (
                f"**Possible missed update** - lines {res.main_from_line}-{res.main_to_line} in `{base_branch}` were duplicated.\n\n"
                "You modified this region. The duplication may be resolved here, but check the other instances:\n\n"
                f"{other}"
            )
            comments.append({"path": file_review.file_path, "line": res.anchor_new_line, "side": "RIGHT", "body": body})

    def _build_fallback_payload(self, report: ReviewResult) -> dict[str, Any]:
        """Build a fallback payload with a top-level summary comment."""
        sha_suffix = f" (commit {report.head_sha[:7]})" if report.head_sha else ""
        lines: list[str] = [f"SonarQube fallback review{sha_suffix}"] if sha_suffix else []
        seen_rules: set[str] = set()
        for file_review in report.files:
            for issue in file_review.issues:
                lines.append(
                    f"- **{issue.rule}** ({file_review.file_path}:{issue.line}) {issue.message}",
                )
                if issue.root_cause and issue.rule not in seen_rules:
                    seen_rules.add(issue.rule)
                    lines.append(
                        f"\n<details>\n\n<summary>{issue.rule} — why this matters</summary>"
                        f"\n\n{issue.root_cause}\n\n</details>\n"
                    )
            for dup in file_review.duplications:
                lines.append(
                    f"- **Duplication** ({file_review.file_path} lines {dup.from_line}-{dup.to_line}) "
                    f"duplicated in {len(dup.other_locations)} other location(s)",
                )
            for res in file_review.resolved_duplications:
                lines.append(
                    f"- **Possible missed update** ({file_review.file_path}) - "
                    f"lines {res.main_from_line}-{res.main_to_line} in main were duplicated; "
                    f"{len(res.other_locations)} other instance(s) may need updating",
                )
        files_with_coverage = [fr for fr in report.files if fr.coverage_pct is not None]
        if files_with_coverage:
            lines.extend(["", "**Coverage on changed lines:**\n\n" + format_coverage_table(files_with_coverage)])
        return {
            "event": "COMMENT",
            "body": _truncate_body("\n".join(lines)),
            "comments": [],
        }

    async def _post_json(
        self,
        cmd: list[str],
        payload: dict[str, Any],
    ) -> None:
        """Post JSON data to a gh API endpoint via stdin.

        Args:
            cmd: Command to run (must accept JSON on stdin).
            payload: JSON-serialisable payload to send.

        Raises:
            GitHubReviewError: If the command fails or times out.
        """
        stdin_data = json.dumps(payload).encode()
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(60):
                stdout_bytes, stderr_bytes = await process.communicate(stdin_data)
        except TimeoutError:
            msg = "gh API call timed out after 60s"
            raise GitHubTimeoutError(msg) from None
        finally:
            # Covers timeout and cancellation (Ctrl-C, outer asyncio.timeout): never leave gh running.
            if process.returncode is None:
                process.kill()
                await asyncio.shield(process.wait())
        if process.returncode != 0:
            stderr_msg = stderr_bytes.decode().strip()
            stdout_msg = stdout_bytes.decode().strip()
            parts = [p for p in (stderr_msg, stdout_msg) if p]
            error_msg = " — ".join(parts) if parts else "Unknown error"
            raise GitHubReviewError(error_msg)

    async def _get_owner_repo(self) -> str:
        """Detect the GitHub owner/repo from the current repository.

        Tries gh CLI first, falls back to parsing the git remote URL.

        Returns:
            String in 'owner/repo' format.
        """
        result = await _run_gh(
            gh_argv("repo", "view", "--json", "owner,name", "--jq", '"\\(.owner.login)/\\(.name)"'),
            15,
        )
        if result.success and result.stdout.strip():
            return result.stdout.strip()

        try:
            git_result = await _run_gh(["git", "remote", "get-url", "origin"], 10)
        except (OSError, GitHubReviewError):
            git_result = None
        if git_result is not None and git_result.success:
            owner_repo = self._parse_remote_url(git_result.stdout.strip())
            if owner_repo:
                return owner_repo

        msg = "Could not detect GitHub owner/repo"
        raise GitHubReviewError(msg)

    @staticmethod
    def _parse_remote_url(remote_url: str) -> str | None:
        """Parse owner/repo from a git remote URL.

        Handles both HTTPS and SSH formats:
        - https://github.com/owner/repo.git
        - git@github.com:owner/repo.git

        Args:
            remote_url: Raw remote URL string.

        Returns:
            'owner/repo' string or None if unparseable.
        """
        # SSH format: git@host:owner/repo.git
        # The repo name may contain dots (e.g. org/my.repo), so we only strip
        # a trailing .git suffix rather than excluding dots from the capture.
        if remote_url.startswith("git@"):
            match = re.search(r":([^/]+)/(.+?)(?:\.git)?$", remote_url)
            if match:
                return f"{match.group(1)}/{match.group(2)}"
            return None

        # HTTPS format: https://host/owner/repo.git
        # Only parse GitHub-hosted remotes — for other forges the subsequent
        # gh api call would fail regardless, and we don't want to accidentally
        # resolve an owner/repo pair against the wrong host.
        parsed = urlparse(remote_url)
        if "github" not in parsed.netloc:
            return None
        path = parsed.path
        # Remove leading slash and trailing .git
        path = path.strip("/").removesuffix(".git")
        parts = path.split("/")
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[0]}/{parts[1]}"
        return None
