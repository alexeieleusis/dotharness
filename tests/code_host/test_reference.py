"""Tests for the reference code host (gh). A stubbed subprocess runner is used: no network, no real gh."""

import contextlib
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest

from dotharness.code_host import CODE_HOST_INTERFACE_VERSION, HostSelection, create_code_host
from dotharness.code_host.declarations import Anchor, Capability
from dotharness.code_host.errors import (
    AuthenticationError,
    CodeHostError,
    CodeHostTimeoutError,
    ConfigurationError,
    NoChangeRequestError,
    TokenCommandError,
    UnsupportedCapabilityError,
)
from dotharness.code_host.models import (
    ChangeRequestState,
    Check,
    CheckStatus,
    InlineComment,
    MergeableState,
    RepositoryRef,
    ReviewDecision,
    ReviewState,
    WorkItemState,
)
from dotharness.code_host.reference import (
    _FILES_QUERY,
    _MARK_VIEWED_MUTATION,
    _MAX_INLINE_COMMENTS,
    _MAX_REVIEW_BODY_CHARS,
    _NODE_ID_QUERY,
    _PAGE_ATTEMPTS,
    _PAGE_RETRY_DELAY,
    _POST_TIMEOUT_SECONDS,
    _PR_FIELDS,
    _PR_FULL_FIELDS,
    _REPLY_MUTATION,
    _STDERR_TAIL,
    _THREADS_QUERY,
    DECLARATION,
    REGISTRATION,
    _create,
    _Gh,
    _Outcome,
    _parse_remote_url,
    _post_json,
)
from dotharness.subprocess_runner import RunResult
from dotharness.vibe_heal.review.models import FileReview, ReviewIssue, ReviewResult
from dotharness.vibe_heal.review.render import build_fallback_body, build_review

REPO = RepositoryRef("myorg/myrepo")
GH_OK = RunResult(0, "gh version 2.52.0", "")


def _host():
    return _create(REGISTRATION.options_model(), None)


def _stub(mocker, handler) -> AsyncMock:
    """Patch the runner; handler(cmd, kwargs) returns a RunResult (or raises)."""

    async def _run(cmd, **kwargs):
        return handler(cmd, kwargs)

    return mocker.patch("dotharness.code_host.reference.run", side_effect=_run)


def _posts(mock_run) -> list[dict]:
    """Payloads of the review posts, in order."""
    return [json.loads(c.kwargs["stdin"]) for c in mock_run.call_args_list if "stdin" in c.kwargs]


def _flow(mocker, *post_results: tuple[int, str, str], repo_view: RunResult | None = None) -> AsyncMock:
    """gh --version succeeds; each review post returns the next result."""
    posts = iter(RunResult(*r) for r in post_results)

    def handler(cmd, kwargs):
        if "--version" in cmd:
            return GH_OK
        if "api" in cmd:
            return next(posts)
        if "get-url" in cmd:
            return RunResult(0, "git@github.com:myorg/myrepo.git\n", "")
        if "repo" in cmd:
            return repo_view or RunResult(0, "myorg/myrepo\n", "")
        return RunResult(1, "", "unexpected")

    return _stub(mocker, handler)


async def _post(report, *, number: int = 42, with_report: bool = True):
    discussion = _host().discussion
    body, inline_comments = build_review(report, _MAX_INLINE_COMMENTS)
    return await discussion.post_review(
        REPO,
        number,
        body=body,
        inline_comments=inline_comments,
        fallback_body=build_fallback_body(report) if with_report else None,
    )


@pytest.fixture
def sample_report() -> ReviewResult:
    return ReviewResult(
        project_key="test-project",
        branch="feature/test",
        base_branch="origin/main",
        files=[
            FileReview(
                file_path="src/example.py",
                issues=[
                    ReviewIssue(
                        rule="python:S1481",
                        message="Remove unused variable",
                        line=10,
                        severity="MAJOR",
                        doc_url="https://rules.example.com/S1481",
                    ),
                    ReviewIssue(rule="python:S1192", message="Useless import", line=25, severity="MINOR"),
                ],
            ),
        ],
    )


# --------------------------------------------------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------------------------------------------------


class TestSession:
    async def test_available_true(self, mocker) -> None:
        mock_run = _stub(mocker, lambda cmd, kw: GH_OK)
        assert await _host().session.available() is True
        assert mock_run.call_args.args[0] == ["gh", "--version"]

    async def test_available_false_on_nonzero_exit(self, mocker) -> None:
        _stub(mocker, lambda cmd, kw: RunResult(1, "", "boom"))
        assert await _host().session.available() is False

    async def test_available_false_on_oserror(self, mocker) -> None:
        mocker.patch("dotharness.code_host.reference.run", side_effect=FileNotFoundError("gh"))
        assert await _host().session.available() is False

    async def test_available_false_when_not_found(self, mocker) -> None:
        _stub(mocker, lambda cmd, kw: RunResult(127, "", "no gh", not_found=True))
        assert await _host().session.available() is False

    async def test_available_false_on_deadline(self, mocker) -> None:
        async def _hang(cmd, **kw):
            raise TimeoutError

        mocker.patch("dotharness.code_host.reference.run", side_effect=_hang)
        assert await _host().session.available() is False

    async def test_raises_when_gh_not_installed(self, mocker) -> None:
        """A call that needs gh raises OSError when gh is not installed."""
        _stub(mocker, lambda cmd, kw: RunResult(1, "", ""))
        with pytest.raises(OSError, match="gh CLI"):
            await _host().change_requests.for_current_branch(REPO)

    async def test_raises_when_gh_missing_from_path(self, mocker) -> None:
        mocker.patch("dotharness.code_host.reference.run", side_effect=FileNotFoundError("gh"))
        with pytest.raises(OSError, match=r"https://cli\.github\.com/"):
            await _post_review_call()


async def _post_review_call():
    return await _host().discussion.post_review(REPO, 1, body="b", inline_comments=[])


# --------------------------------------------------------------------------------------------------------------------
# ChangeRequests.for_current_branch
# --------------------------------------------------------------------------------------------------------------------


class TestForCurrentBranch:
    async def test_auto_detects_from_current_branch(self, mocker) -> None:
        mock_run = _stub(
            mocker,
            lambda cmd, kw: GH_OK if "--version" in cmd else RunResult(0, '{"number": 73, "baseRefName": "main"}', ""),
        )
        cr = await _host().change_requests.for_current_branch(REPO)
        assert cr.number == 73
        assert cr.base_reference == "main"
        assert cr.repository == REPO
        view = mock_run.call_args_list[1].args[0]
        assert view[:3] == ["gh", "pr", "view"]
        assert view[3] == "--json"
        assert len(view) == 5  # no branch argument
        assert mock_run.call_args_list[1].kwargs["cwd"] is not None

    async def test_full_fields_are_mapped(self, mocker) -> None:
        payload = {
            "number": 5,
            "baseRefName": "feature/lower-in-stack",
            "title": "T",
            "body": "B",
            "url": "https://example.test/pr/5",
            "state": "OPEN",
            "isDraft": True,
            "headRefName": "feat",
            "headRefOid": "abc",
            "isCrossRepository": True,
            "createdAt": "2026-01-02T03:04:05Z",
            "author": {"login": "someone"},
        }
        _stub(mocker, lambda cmd, kw: GH_OK if "--version" in cmd else RunResult(0, json.dumps(payload), ""))
        cr = await _host().change_requests.for_current_branch(REPO)
        assert cr.base_reference == "feature/lower-in-stack"
        assert (cr.title, cr.body, cr.head_reference, cr.head_revision) == ("T", "B", "feat", "abc")
        assert cr.state is ChangeRequestState.OPEN
        assert cr.is_draft is True
        assert cr.is_cross_repository is True
        assert cr.author is not None
        assert cr.author.login == "someone"
        assert cr.created_at.year == 2026

    async def test_no_pull_request_raises_no_change_request(self, mocker) -> None:
        _stub(
            mocker,
            lambda cmd, kw: GH_OK if "--version" in cmd else RunResult(1, "", "no pull requests found for branch x"),
        )
        with pytest.raises(NoChangeRequestError, match="No open pull request found for the current branch"):
            await _host().change_requests.for_current_branch(REPO)

    async def test_no_pull_request_match_is_case_insensitive(self, mocker) -> None:
        _stub(mocker, lambda cmd, kw: GH_OK if "--version" in cmd else RunResult(1, "", "No Pull Requests Found"))
        with pytest.raises(NoChangeRequestError):
            await _host().change_requests.for_current_branch(REPO)

    async def test_raises_when_not_authenticated(self, mocker) -> None:
        _stub(
            mocker,
            lambda cmd, kw: GH_OK if "--version" in cmd else RunResult(1, "", "error: you are not authenticated"),
        )
        with pytest.raises(CodeHostError, match="Failed to detect PR: error: you are not authenticated") as info:
            await _host().change_requests.for_current_branch(REPO)
        assert not isinstance(info.value, NoChangeRequestError)

    async def test_empty_stderr_reports_not_authenticated(self, mocker) -> None:
        _stub(mocker, lambda cmd, kw: GH_OK if "--version" in cmd else RunResult(1, "", ""))
        with pytest.raises(CodeHostError, match="Failed to detect PR: not authenticated"):
            await _host().change_requests.for_current_branch(REPO)

    @pytest.mark.parametrize("stdout", ["not json", "null", "{}", '{"number": "x"}'])
    async def test_malformed_response_raises(self, mocker, stdout) -> None:
        _stub(mocker, lambda cmd, kw: GH_OK if "--version" in cmd else RunResult(0, stdout, ""))
        with pytest.raises(CodeHostError, match="Failed to detect PR"):
            await _host().change_requests.for_current_branch(REPO)

    async def test_sparse_response_uses_defaults(self, mocker) -> None:
        _stub(
            mocker,
            lambda cmd, kw: (
                GH_OK
                if "--version" in cmd
                else RunResult(0, '{"number": 1, "state": "weird", "createdAt": "bad", "author": null}', "")
            ),
        )
        cr = await _host().change_requests.for_current_branch(REPO)
        assert cr.state is ChangeRequestState.OPEN
        assert cr.author is None
        assert cr.base_reference == ""

    async def test_read_timeout_raises_read_timeout_error(self, mocker) -> None:
        """Error model R3: a read timeout is CodeHostTimeoutError with write=False."""

        async def _run(cmd, **kw):
            if "--version" in cmd:
                return GH_OK
            raise TimeoutError

        mocker.patch("dotharness.code_host.reference.run", side_effect=_run)
        with pytest.raises(CodeHostTimeoutError, match=r"`gh pr` timed out after 30s") as info:
            await _host().change_requests.for_current_branch(REPO)
        assert info.value.write is False


# --------------------------------------------------------------------------------------------------------------------
# Repository.current
# --------------------------------------------------------------------------------------------------------------------


class TestRepositoryCurrent:
    async def test_uses_gh_repo_view(self, mocker) -> None:
        mock_run = _stub(mocker, lambda cmd, kw: RunResult(0, "ghorg/ghrepo\n", ""))
        ref = await _host().repository.current()
        assert ref == RepositoryRef("ghorg/ghrepo")
        cmd = mock_run.call_args.args[0]
        assert cmd[:3] == ["gh", "repo", "view"]
        assert "owner,name" in cmd
        assert mock_run.await_count == 1

    async def test_falls_back_to_git_remote(self, mocker) -> None:
        def handler(cmd, kw):
            if "get-url" in cmd:
                return RunResult(0, "git@github.com:myorg/my.repo.git\n", "")
            return RunResult(1, "", "")

        mock_run = _stub(mocker, handler)
        assert (await _host().repository.current()).value == "myorg/my.repo"
        assert mock_run.call_args.args[0] == ["git", "remote", "get-url", "origin"]

    async def test_empty_gh_output_falls_back(self, mocker) -> None:
        def handler(cmd, kw):
            if "get-url" in cmd:
                return RunResult(0, "https://github.com/a/b.git\n", "")
            return RunResult(0, "  \n", "")

        _stub(mocker, handler)
        assert (await _host().repository.current()).value == "a/b"

    async def test_raises_when_undetectable(self, mocker) -> None:
        _stub(mocker, lambda cmd, kw: RunResult(1, "", ""))
        with pytest.raises(CodeHostError, match="Could not detect GitHub owner/repo"):
            await _host().repository.current()

    async def test_raises_when_remote_is_not_github(self, mocker) -> None:
        def handler(cmd, kw):
            if "get-url" in cmd:
                return RunResult(0, "https://gitlab.com/a/b.git\n", "")
            return RunResult(1, "", "")

        _stub(mocker, handler)
        with pytest.raises(CodeHostError, match="Could not detect GitHub owner/repo"):
            await _host().repository.current()

    async def test_git_failure_is_swallowed(self, mocker) -> None:
        def handler(cmd, kw):
            if "get-url" in cmd:
                raise FileNotFoundError("git")
            return RunResult(1, "", "")

        _stub(mocker, handler)
        with pytest.raises(CodeHostError, match="Could not detect"):
            await _host().repository.current()


class TestParseRemoteUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/owner/repo.git", "owner/repo"),
            ("https://github.com/owner/repo", "owner/repo"),
            ("https://github.example.com/owner/repo.git", "owner/repo"),
            ("git@github.com:owner/repo.git", "owner/repo"),
            ("git@github.com:owner/repo", "owner/repo"),
            ("git@github.com:owner/my.repo.git", "owner/my.repo"),
            ("git@gitlab.com:owner/repo.git", "owner/repo"),  # SSH is accepted for any host
            ("https://gitlab.com/owner/repo.git", None),  # HTTPS only when the host contains 'github'
            ("https://github.com/owner", None),
            ("https://github.com/a/b/c", None),
            ("git@github.com:onlyone", None),
            ("", None),
        ],
    )
    def test_cases(self, url, expected) -> None:
        assert _parse_remote_url(url) == expected


# --------------------------------------------------------------------------------------------------------------------
# Discussion.post_review
# --------------------------------------------------------------------------------------------------------------------


class TestPostReview:
    async def test_single_api_call(self, sample_report, mocker) -> None:
        mock_run = _flow(mocker, (0, '{"id": 123}', ""))
        result = await _post(sample_report)

        assert result.fell_back is False
        assert result.reason is None
        assert len(_posts(mock_run)) == 1
        payload = _posts(mock_run)[0]
        assert payload["event"] == "COMMENT"
        assert len(payload["comments"]) == 2

    async def test_inline_comment_format(self, sample_report, mocker) -> None:
        mock_run = _flow(mocker, (0, "{}", ""))
        await _post(sample_report)

        comments = _posts(mock_run)[0]["comments"]
        assert comments[0]["body"] == "**python:S1481** Remove unused variable\n\nhttps://rules.example.com/S1481"
        assert comments[0]["path"] == "src/example.py"
        assert comments[0]["line"] == 10
        assert comments[0]["side"] == "RIGHT"
        assert set(comments[0]) == {"path", "line", "side", "body"}
        assert comments[1]["body"] == "**python:S1192** Useless import"

    async def test_builds_correct_api_endpoint(self, sample_report, mocker) -> None:
        mock_run = _flow(mocker, (0, "{}", ""))
        await _post(sample_report)

        api_call = next(c for c in mock_run.call_args_list if "api" in c.args[0])
        args = api_call.args[0]
        assert args[0] == "gh"
        assert "api" in args
        assert args[args.index("--method") + 1] == "POST"
        assert "--input" in args
        assert "-" in args
        assert "/repos/myorg/myrepo/pulls/42/reviews" in args
        assert api_call.kwargs["timeout"] == _POST_TIMEOUT_SECONDS
        assert "stdin" in api_call.kwargs

    async def test_endpoint_comes_from_the_repository_ref(self, mocker) -> None:
        mock_run = _flow(mocker, (0, "{}", ""))
        await _host().discussion.post_review(RepositoryRef("ghorg/ghrepo"), 99, body="b", inline_comments=[])
        api_args = next(c.args[0] for c in mock_run.call_args_list if "api" in c.args[0])
        assert "/repos/ghorg/ghrepo/pulls/99/reviews" in api_args

    async def test_no_token_in_arguments(self, sample_report, mocker) -> None:
        mock_run = _flow(mocker, (0, "{}", ""))
        await _post(sample_report)
        for call in mock_run.call_args_list:
            assert not any("token" in str(a).lower() for a in call.args[0])

    async def test_posts_body_and_comments_as_given(self, mocker) -> None:
        mock_run = _flow(mocker, (0, "{}", ""))
        ic = InlineComment("hello", Anchor("a.py", 3, "RIGHT"))
        await _host().discussion.post_review(REPO, 1, body="my body", inline_comments=[ic])
        assert _posts(mock_run) == [
            {
                "event": "COMMENT",
                "body": "my body",
                "comments": [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "hello"}],
            }
        ]

    async def test_empty_report_posts_empty_review(self, mocker) -> None:
        empty = ReviewResult(project_key="test", branch="main", base_branch="origin/main", files=[])
        mock_run = _flow(mocker, (0, "{}", ""))
        await _post(empty)
        payload = _posts(mock_run)[0]
        assert payload["event"] == "COMMENT"
        assert payload["comments"] == []

    async def test_inline_payload_includes_nonempty_body(self, sample_report, mocker) -> None:
        mock_run = _flow(mocker, (0, "{}", ""))
        await _post(sample_report)
        assert _posts(mock_run)[0]["body"]

    async def test_multiple_files_aggregated(self, mocker) -> None:
        report = ReviewResult(
            project_key="test",
            branch="feat",
            base_branch="origin/main",
            files=[
                FileReview(file_path="src/a.py", issues=[ReviewIssue(rule="python:S1", message="Issue A1", line=1)]),
                FileReview(
                    file_path="src/b.py",
                    issues=[
                        ReviewIssue(rule="python:S2", message="Issue B1", line=10),
                        ReviewIssue(rule="python:S3", message="Issue B2", line=20),
                    ],
                ),
            ],
        )
        mock_run = _flow(mocker, (0, "{}", ""))
        await _post(report)
        comments = _posts(mock_run)[0]["comments"]
        assert len(comments) == 3
        assert {c["path"] for c in comments} == {"src/a.py", "src/b.py"}

    async def test_root_cause_in_inline_comment_and_review_body(self, mocker) -> None:
        report = ReviewResult(
            project_key="test",
            branch="feat",
            base_branch="origin/main",
            files=[
                FileReview(
                    file_path="src/example.py",
                    issues=[
                        ReviewIssue(
                            rule="python:S1481",
                            message="Remove unused variable",
                            line=10,
                            severity="MAJOR",
                            root_cause="<p>Unused variables waste memory.</p>",
                        ),
                        ReviewIssue(
                            rule="python:S1481",
                            message="Another unused variable",
                            line=20,
                            severity="MAJOR",
                            root_cause="<p>Unused variables waste memory.</p>",
                        ),
                    ],
                ),
            ],
        )
        mock_run = _flow(mocker, (0, "{}", ""))
        await _post(report)
        payload = _posts(mock_run)[0]
        for comment in payload["comments"]:
            assert "<details>" in comment["body"]
            assert "why this matters" in comment["body"]
        assert "<details>" not in payload["body"]
        assert "SonarQube:" in payload["body"]

    async def test_posts_given_body(self, mocker) -> None:
        mock_run = _flow(mocker, (0, "{}", ""))
        await _host().discussion.post_review(REPO, 1, body="given body", inline_comments=[])
        assert _posts(mock_run)[0]["body"] == "given body"
        assert _posts(mock_run)[0]["comments"] == []


class TestFallback:
    async def test_fallback_comment_for_rejected_lines(self, sample_report, mocker) -> None:
        mock_run = _flow(mocker, (1, "", "HTTP 422 Unprocessable Entity"), (0, "{}", ""))
        result = await _post(sample_report)

        assert result.fell_back is True
        assert result.reason == "HTTP 422 Unprocessable Entity"
        posts = _posts(mock_run)
        assert len(posts) == 2
        assert posts[1]["event"] == "COMMENT"
        assert posts[1]["comments"] == []
        assert "python:S1481" in posts[1]["body"]
        assert "python:S1192" in posts[1]["body"]

    async def test_fallback_without_report_posts_body(self, sample_report, mocker) -> None:
        mock_run = _flow(mocker, (1, "", "HTTP 422 line outside diff"), (0, "{}", ""))
        result = await _post(sample_report, with_report=False)

        assert result.fell_back is True
        assert result.reason == "HTTP 422 line outside diff"
        posts = _posts(mock_run)
        assert posts[1]["comments"] == []
        assert posts[1]["body"] == posts[0]["body"]

    async def test_fallback_includes_root_cause_details(self, mocker) -> None:
        report = ReviewResult(
            project_key="test",
            branch="feat",
            base_branch="origin/main",
            files=[
                FileReview(
                    file_path="src/example.py",
                    issues=[
                        ReviewIssue(
                            rule="python:S1481",
                            message="Remove unused variable",
                            line=10,
                            severity="MAJOR",
                            root_cause="<p>Unused variables clutter the code.</p>",
                        ),
                        ReviewIssue(
                            rule="python:S1481",
                            message="Remove another unused variable",
                            line=20,
                            severity="MAJOR",
                            root_cause="<p>Unused variables clutter the code.</p>",
                        ),
                        ReviewIssue(rule="python:S1192", message="No root cause here", line=30, severity="MINOR"),
                    ],
                ),
            ],
        )
        mock_run = _flow(mocker, (1, "", "HTTP 422 Unprocessable Entity"), (0, "{}", ""))
        await _post(report)

        body = _posts(mock_run)[1]["body"]
        assert "<details>" in body
        assert "python:S1481 — why this matters" in body
        assert "<p>Unused variables clutter the code.</p>" in body
        assert body.count("<details>") == 1
        assert "python:S1192" in body
        assert "python:S1192 — why this matters" not in body

    async def test_fallback_lists_duplications_and_resolved(self, mocker) -> None:
        from dotharness.vibe_heal.review.models import DuplicationLocation, ResolvedDuplication, ReviewDuplication

        loc = DuplicationLocation(file_path="other.py", from_line=1, to_line=5)
        report = ReviewResult(
            project_key="p",
            branch="b",
            base_branch="main",
            head_sha="abcdef1234567890",
            files=[
                FileReview(
                    file_path="a.py",
                    duplications=[ReviewDuplication(from_line=3, to_line=9, other_locations=[loc])],
                    resolved_duplications=[
                        ResolvedDuplication(
                            main_from_line=1, main_to_line=4, anchor_new_line=2, other_locations=[loc, loc]
                        )
                    ],
                )
            ],
        )
        mock_run = _flow(mocker, (1, "", "HTTP 422 bad anchor"), (0, "{}", ""))
        await _post(report)

        body = _posts(mock_run)[1]["body"]
        assert "SonarQube fallback review (commit abcdef1)" in body
        assert "- **Duplication** (a.py lines 3-9) duplicated in 1 other location(s)" in body
        assert "- **Possible missed update** (a.py) - lines 1-4 in main were duplicated; 2 other instance(s)" in body

    async def test_fallback_includes_coverage_table(self, mocker) -> None:
        report = ReviewResult(
            project_key="p",
            branch="b",
            base_branch="main",
            files=[FileReview(file_path="a.py", coverage_pct=50.0, covered_lines=5, instrumented_changed_lines=10)],
        )
        mock_run = _flow(mocker, (1, "", "HTTP 422 bad"), (0, "{}", ""))
        await _post(report)
        body = _posts(mock_run)[1]["body"]
        assert "**50.0%**" in body
        assert "| 5 |" in body
        assert "| 10 |" in body
        assert "Coverage on changed lines" in body

    async def test_raises_when_fallback_also_fails(self, sample_report, mocker) -> None:
        mock_run = _flow(
            mocker,
            (1, "", "HTTP 422 internal error"),
            (1, "", "HTTP 422 internal error"),
        )
        with pytest.raises(CodeHostError, match=r"^Review posting failed: HTTP 422 internal error") as info:
            await _post(sample_report)
        assert info.value.__cause__ is not None
        assert len(_posts(mock_run)) == 2  # nothing else is posted

    async def test_fallback_timeout_stays_a_timeout(self, sample_report, mocker) -> None:
        results = iter([RunResult(1, "", "HTTP 422 bad"), RunResult(124, "", "", timed_out=True)])
        _stub(mocker, lambda cmd, kw: GH_OK if "--version" in cmd else next(results))
        with pytest.raises(CodeHostTimeoutError, match="timed out after 60s"):
            await _post(sample_report)

    async def test_timeout_does_not_trigger_fallback(self, sample_report, mocker) -> None:
        mock_run = _stub(
            mocker,
            lambda cmd, kw: GH_OK if "--version" in cmd else RunResult(124, "", "timed out after 60s", timed_out=True),
        )
        with pytest.raises(CodeHostTimeoutError, match="timed out after 60s"):
            await _post(sample_report)
        assert len(_posts(mock_run)) == 1

    @pytest.mark.parametrize("exc", [AuthenticationError("denied"), UnsupportedCapabilityError("no", capability="X")])
    async def test_other_errors_do_not_trigger_fallback(self, sample_report, mocker, exc) -> None:
        _flow(mocker)
        post = mocker.patch("dotharness.code_host.reference._post_json", new_callable=AsyncMock, side_effect=exc)
        with pytest.raises(type(exc)):
            await _post(sample_report)
        assert post.await_count == 1

    @pytest.mark.parametrize(
        ("stderr", "error"),
        [
            ("gh: Bad credentials (HTTP 401)", AuthenticationError),
            ("HTTP 403 Forbidden", AuthenticationError),
            ("HTTP 502 Bad Gateway", CodeHostError),
            ("HTTP 504", CodeHostError),
            ("connection reset by peer", CodeHostError),
        ],
    )
    async def test_non_validation_failures_post_once(self, sample_report, mocker, stderr, error) -> None:
        mock_run = _flow(mocker, (1, "", stderr), (0, "{}", ""))
        with pytest.raises(error):
            await _post(sample_report)
        assert len(_posts(mock_run)) == 1

    async def test_not_found_propagates_without_fallback(self, sample_report, mocker) -> None:
        mock_run = _stub(
            mocker,
            lambda cmd, kw: GH_OK if "--version" in cmd else RunResult(127, "", "no gh", not_found=True),
        )
        with pytest.raises(FileNotFoundError):
            await _post(sample_report)
        assert len(_posts(mock_run)) == 1

    async def test_fallback_logs_warning_with_inline_error(self, sample_report, mocker, caplog) -> None:
        _flow(mocker, (1, "", "HTTP 422 validation error: line outside diff"), (0, "{}", ""))
        with caplog.at_level(logging.WARNING, logger="dotharness.code_host.reference"):
            await _post(sample_report)
        assert any("Inline review failed" in r.message for r in caplog.records)
        assert any("falling back" in r.message for r in caplog.records)
        assert any("line outside diff" in r.message for r in caplog.records)

    async def test_host_does_not_print_warning(self, sample_report, mocker, capsys) -> None:
        _flow(mocker, (1, "", "HTTP 422 bad"), (0, "{}", ""))
        await _post(sample_report)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestSizeLimits:
    async def test_body_and_inline_comments_are_capped(self, mocker) -> None:
        mock_run = _flow(mocker, (0, "{}", ""))
        comments = [InlineComment("c", Anchor("a.py", i + 1, "RIGHT")) for i in range(_MAX_INLINE_COMMENTS + 5)]
        await _host().discussion.post_review(
            REPO, 1, body="x" * (_MAX_REVIEW_BODY_CHARS + 10), inline_comments=comments
        )
        (payload,) = _posts(mock_run)
        assert len(payload["comments"]) == _MAX_INLINE_COMMENTS
        assert len(payload["body"]) <= _MAX_REVIEW_BODY_CHARS
        assert "review body truncated" in payload["body"]

    async def test_fallback_body_is_posted_and_capped(self, mocker) -> None:
        mock_run = _flow(mocker, (1, "", "HTTP 422 bad"), (0, "{}", ""))
        result = await _host().discussion.post_review(
            REPO, 1, body="b", inline_comments=[], fallback_body="y" * (_MAX_REVIEW_BODY_CHARS + 10)
        )
        assert result.fell_back is True
        fallback = _posts(mock_run)[1]["body"]
        assert len(fallback) <= _MAX_REVIEW_BODY_CHARS
        assert fallback.startswith("yyy")


class TestPostJson:
    async def test_maps_runner_flags(self, mocker) -> None:
        mocker.patch(
            "dotharness.code_host.reference.run",
            new_callable=AsyncMock,
            side_effect=[
                RunResult(124, "", "timed out after 60s", timed_out=True),
                RunResult(127, "", "no gh", not_found=True),
            ],
        )
        with pytest.raises(CodeHostTimeoutError, match="timed out after 60s"):
            await _post_json(["gh"], {})
        with pytest.raises(FileNotFoundError):
            await _post_json(["gh"], {})

    async def test_nonzero_exit_joins_stderr_and_stdout(self, mocker) -> None:
        mocker.patch(
            "dotharness.code_host.reference.run",
            new_callable=AsyncMock,
            side_effect=[RunResult(1, " out ", " err "), RunResult(1, "", "")],
        )
        with pytest.raises(CodeHostError, match=r"^err — out$"):
            await _post_json(["gh"], {})
        with pytest.raises(CodeHostError, match=r"^Unknown error$"):
            await _post_json(["gh"], {})

    async def test_json_on_stdin_with_post_timeout(self, mocker) -> None:
        mock_run = mocker.patch(
            "dotharness.code_host.reference.run", new_callable=AsyncMock, return_value=RunResult(0, "", "")
        )
        await _post_json(["gh"], {"a": 1})
        assert mock_run.call_args.kwargs == {"timeout": 60, "stdin": '{"a": 1}'}


# --------------------------------------------------------------------------------------------------------------------
# Declaration and registration
# --------------------------------------------------------------------------------------------------------------------


class TestDeclaration:
    def test_limits(self) -> None:
        limits = DECLARATION.limits
        assert limits.max_inline_comments_per_review == 50
        assert limits.max_review_body_chars == 60000
        assert limits.timeouts.default_seconds == 30
        assert limits.timeouts.per_operation["Session.available"] == 10
        assert limits.timeouts.per_operation["ChangeRequests.find_by_branch"] == 30
        assert limits.timeouts.per_operation["Discussion.post_review"] == 60

    def test_constants(self) -> None:
        assert _MAX_INLINE_COMMENTS == 50
        assert _MAX_REVIEW_BODY_CHARS == 60_000
        assert _POST_TIMEOUT_SECONDS == 60

    def test_anchor_rules(self) -> None:
        anchors = DECLARATION.anchors
        assert anchors.inline_supported is True
        assert anchors.sides == frozenset({"RIGHT"})
        assert anchors.requires_line_in_diff is True
        assert anchors.supports_ranges is False
        assert anchors.fallback == "top_level_comment"

    def test_every_capability_is_declared(self) -> None:
        assert DECLARATION.capabilities == frozenset(Capability)
        assert Capability.CHECKS in DECLARATION.capabilities
        assert Capability.WORK_ITEMS in DECLARATION.capabilities
        assert Capability.DISCUSSION in DECLARATION.capabilities


class TestRegistration:
    def test_registration_values(self) -> None:
        assert REGISTRATION.interface_version == CODE_HOST_INTERFACE_VERSION
        assert REGISTRATION.options_model() is not None
        assert callable(REGISTRATION.factory)

    def test_name_reference_resolves(self) -> None:
        host = create_code_host(HostSelection(setting="s", name="reference", options={}, token_cmd=None))
        assert host.declaration is DECLARATION

    def test_unset_name_resolves_to_reference(self) -> None:
        host = create_code_host(HostSelection(setting="s", name=None, options={}, token_cmd=None))
        assert host.declaration is DECLARATION

    def test_creation_runs_no_command(self, mocker) -> None:
        mock_run = mocker.patch("dotharness.code_host.reference.run", new_callable=AsyncMock)
        create_code_host(HostSelection(setting="s", name="reference", options={}, token_cmd="secret-cmd"))  # noqa: S106
        assert mock_run.await_count == 0

    def test_options_are_empty(self) -> None:
        with pytest.raises(Exception, match="options are not valid"):
            create_code_host(HostSelection(setting="s", name="reference", options={"x": 1}, token_cmd=None))

    def test_importing_loads_no_vibe_heal(self) -> None:
        code = "import sys, dotharness.code_host.reference; sys.exit(1 if [m for m in sys.modules if 'vibe_heal' in m] else 0)"
        assert subprocess.run([sys.executable, "-I", "-c", code], check=False).returncode == 0


# --------------------------------------------------------------------------------------------------------------------
# Phase 2 capabilities: exact gh argv, directory, environment, errors
# --------------------------------------------------------------------------------------------------------------------

SECRET = "s3cr3t-value"  # noqa: S105
INHERITED = "inherited-value"


def _recorder(mocker, *results: RunResult | tuple[int, str, str]) -> AsyncMock:
    queue = [r if isinstance(r, RunResult) else RunResult(*r) for r in results]
    return _stub(mocker, lambda cmd, kw: queue.pop(0))


def _argv(mock_run, index: int = 0) -> list[str]:
    return mock_run.call_args_list[index].args[0]


class TestArgv:
    """R-7.1.2: every argv equals today's sisyphus argv, element for element, with no extra call."""

    async def test_list_open(self, mocker) -> None:
        rows = [
            {
                "number": 7,
                "title": "T",
                "createdAt": "2024-01-02T03:04:05Z",
                "assignees": [{"login": "bob"}],
                "labels": [{"name": "ready"}],
            }
        ]
        mock_run = _recorder(mocker, (0, json.dumps(rows), ""))
        items = await _host().work_items.list_open(REPO, assignee="bob", label="ready")
        assert mock_run.await_count == 1
        assert _argv(mock_run) == [
            "gh", "issue", "list", "--state", "open", "--assignee", "bob", "--label", "ready",
            "--limit", "100", "--json", "number,title,createdAt,assignees,labels",
        ]  # fmt: skip
        (item,) = items
        assert (item.number, item.title, item.labels, item.assignees[0].login) == (7, "T", ("ready",), "bob")
        assert item.created_at.isoformat() == "2024-01-02T03:04:05+00:00"
        assert item.repository == REPO

    async def test_list_open_keeps_gh_order(self, mocker) -> None:
        rows = [{"number": n} for n in (9, 3, 5)]
        _recorder(mocker, (0, json.dumps(rows), ""))
        assert [i.number for i in await _host().work_items.list_open(REPO, assignee="a", label="l")] == [9, 3, 5]

    async def test_list_open_omits_unset_filters(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "[]", ""))
        assert await _host().work_items.list_open(REPO) == []
        assert "--assignee" not in _argv(mock_run)
        assert "--label" not in _argv(mock_run)

    async def test_list_open_unread_fields_take_defaults(self, mocker) -> None:
        _recorder(mocker, (0, '[{"number": 1, "createdAt": "garbage"}]', ""))
        (item,) = await _host().work_items.list_open(REPO, assignee="a", label="l")
        assert (item.title, item.body, item.labels, item.assignees) == ("", "", (), ())
        assert item.created_at.timestamp() == 0

    async def test_remove_label(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "", ""))
        await _host().work_items.remove_label(REPO, 12, "ready")
        assert mock_run.await_count == 1
        assert _argv(mock_run) == ["gh", "issue", "edit", "12", "--remove-label", "ready"]

    async def test_closing_change_requests(self, mocker) -> None:
        mock_run = _recorder(
            mocker,
            (0, '{"closedByPullRequestsReferences": [{"number": 4}, {"number": 8}]}', ""),
            (0, '{"number": 4, "state": "OPEN"}', ""),
            (0, '{"number": 8, "state": "MERGED"}', ""),
        )
        found = await _host().work_items.closing_change_requests(REPO, 3)
        assert [_argv(mock_run, i) for i in range(3)] == [
            ["gh", "issue", "view", "3", "--json", "closedByPullRequestsReferences"],
            ["gh", "pr", "view", "4", "--json", "number,state"],
            ["gh", "pr", "view", "8", "--json", "number,state"],
        ]
        assert [(c.number, c.state) for c in found] == [
            (4, ChangeRequestState.OPEN),
            (8, ChangeRequestState.MERGED),
        ]

    async def test_closing_change_requests_none(self, mocker) -> None:
        _recorder(mocker, (0, '{"closedByPullRequestsReferences": []}', ""))
        assert await _host().work_items.closing_change_requests(REPO, 3) == []

    async def test_open_change_request_bodies(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, '[{"body": "Fixes #3"}, {"body": null}]', ""))
        found = await _host().work_items.open_change_request_bodies(REPO)
        assert _argv(mock_run) == ["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", "body"]
        assert [c.body for c in found] == ["Fixes #3", ""]

    async def test_find_by_branch_open(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, '[{"number": 5, "url": "https://h/pull/5"}]', ""))
        (cr,) = await _host().change_requests.find_by_branch(REPO, "feat/x")
        assert _argv(mock_run) == ["gh", "pr", "list", "--head", "feat/x", "--state", "open", "--json", "number,url"]
        assert (cr.number, cr.url, cr.state, cr.head_reference) == (
            5,
            "https://h/pull/5",
            ChangeRequestState.OPEN,
            "feat/x",
        )

    async def test_find_by_branch_other_state(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "[]", ""))
        assert await _host().change_requests.find_by_branch(REPO, "b", state=ChangeRequestState.MERGED) == []
        assert _argv(mock_run)[5:7] == ["--state", "merged"]

    async def test_find_by_branch_any_state_is_pr_view(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, '{"number": 9, "state": "MERGED", "headRefOid": "abc"}', ""))
        (cr,) = await _host().change_requests.find_by_branch(REPO, "feat/x", state=None)
        assert _argv(mock_run) == ["gh", "pr", "view", "feat/x", "--json", "number,state,headRefOid"]
        assert (cr.number, cr.state, cr.head_revision) == (9, ChangeRequestState.MERGED, "abc")

    async def test_find_by_branch_any_state_none_found_is_empty(self, mocker) -> None:
        _recorder(mocker, (1, "", "no pull requests found for branch"))
        assert await _host().change_requests.find_by_branch(REPO, "b", state=None) == []

    async def test_find_by_branch_any_state_other_failure_raises(self, mocker) -> None:
        _recorder(mocker, (1, "", "boom"))
        with pytest.raises(CodeHostError, match=r"gh pr view failed \(exit 1\): boom"):
            await _host().change_requests.find_by_branch(REPO, "b", state=None)

    async def test_get(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, '{"number": 6, "state": "CLOSED", "headRefOid": "sha"}', ""))
        cr = await _host().change_requests.get(REPO, 6)
        assert _argv(mock_run) == ["gh", "pr", "view", "6", "--repo", "myorg/myrepo", "--json", _PR_FULL_FIELDS]
        assert (cr.number, cr.state, cr.head_revision, cr.repository) == (6, ChangeRequestState.CLOSED, "sha", REPO)

    async def test_state(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, '{"state": "MERGED"}', ""))
        assert await _host().change_requests.state(REPO, 6) is ChangeRequestState.MERGED
        assert _argv(mock_run) == ["gh", "pr", "view", "6", "--repo", "myorg/myrepo", "--json", "state"]

    async def test_state_missing_raises(self, mocker) -> None:
        _recorder(mocker, (0, "{}", ""))
        with pytest.raises(CodeHostError, match="no state"):
            await _host().change_requests.state(REPO, 6)

    async def test_create_draft(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "Creating draft\nhttps://h/o/r/pull/31\n", ""))
        cr = await _host().change_requests.create_draft(REPO, head="feat", base="main", title="T", body="B")
        assert mock_run.await_count == 1
        assert _argv(mock_run) == [
            "gh", "pr", "create", "--draft", "--head", "feat", "--base", "main", "--title", "T", "--body", "B",
        ]  # fmt: skip
        assert (cr.number, cr.url, cr.is_draft, cr.head_reference, cr.base_reference) == (
            31, "https://h/o/r/pull/31", True, "feat", "main",
        )  # fmt: skip

    async def test_create_draft_without_url_raises(self, mocker) -> None:
        _recorder(mocker, (0, "no url here", ""))
        with pytest.raises(CodeHostError, match=r"gh pr create failed \(exit 0\)"):
            await _host().change_requests.create_draft(REPO, head="h", base="b", title="t", body="b")

    async def test_create_draft_nonzero_raises_with_exit_code(self, mocker) -> None:
        _recorder(mocker, (1, "", "already exists"))
        with pytest.raises(CodeHostError, match=r"gh pr create failed \(exit 1\): already exists"):
            await _host().change_requests.create_draft(REPO, head="h", base="b", title="t", body="b")

    async def test_default_branch(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, '{"defaultBranchRef": {"name": "trunk"}}', ""))
        assert await _host().repository.default_branch(REPO) == "trunk"
        assert mock_run.await_count == 1
        assert _argv(mock_run) == ["gh", "repo", "view", "myorg/myrepo", "--json", "defaultBranchRef"]

    @pytest.mark.parametrize("payload", ["{}", '{"defaultBranchRef": null}', '{"defaultBranchRef": {"name": ""}}'])
    async def test_default_branch_missing_raises(self, mocker, payload) -> None:
        _recorder(mocker, (0, payload, ""))
        with pytest.raises(CodeHostError, match="no default branch"):
            await _host().repository.default_branch(REPO)

    async def test_read_failure_carries_exit_code(self, mocker) -> None:
        _recorder(mocker, (4, "", "x" * 400))
        with pytest.raises(CodeHostError, match=r"gh issue list failed \(exit 4\): x{300}$"):
            await _host().work_items.list_open(REPO, assignee="a", label="l")

    async def test_invalid_json_raises(self, mocker) -> None:
        _recorder(mocker, (0, "not json", ""))
        with pytest.raises(CodeHostError, match="invalid JSON"):
            await _host().work_items.list_open(REPO, assignee="a", label="l")

    async def test_auth_failure_is_authentication_error(self, mocker) -> None:
        _recorder(mocker, (1, "", "To get started with GitHub CLI, please run: gh auth login"))
        with pytest.raises(AuthenticationError):
            await _host().repository.default_branch(REPO)


class TestRemoveLabel:
    async def test_nonzero_exit_raises(self, mocker) -> None:
        """R-7.1.4: a label that does not exist in the repository exits non-zero and raises."""
        _recorder(mocker, (1, "", "'nope' not found"))
        with pytest.raises(CodeHostError, match=r"gh issue edit failed \(exit 1\): 'nope' not found"):
            await _host().work_items.remove_label(REPO, 1, "nope")

    async def test_timeout_is_a_write_timeout_at_10s(self, mocker) -> None:
        async def _hang(cmd, **kw):
            raise TimeoutError

        mocker.patch("dotharness.code_host.reference.run", side_effect=_hang)
        with pytest.raises(CodeHostTimeoutError, match="timed out after 10s") as info:
            await _host().work_items.remove_label(REPO, 1, "x")
        assert info.value.write is True

    async def test_declared_timeout_is_10s(self) -> None:
        assert DECLARATION.limits.timeouts.per_operation["WorkItems.remove_label"] == 10.0


class TestTimeouts:
    @pytest.mark.parametrize(
        ("call", "seconds"),
        [
            (lambda h: h.change_requests.state(REPO, 1), "10s"),
            (lambda h: h.work_items.list_open(REPO, assignee="a", label="l"), "30s"),
            (lambda h: h.change_requests.get(REPO, 1), "30s"),
            (lambda h: h.repository.default_branch(REPO), "30s"),
        ],
    )
    async def test_read_timeout_uses_declared_seconds(self, mocker, call, seconds) -> None:
        async def _hang(cmd, **kw):
            raise TimeoutError

        mocker.patch("dotharness.code_host.reference.run", side_effect=_hang)
        with pytest.raises(CodeHostTimeoutError, match=f"timed out after {seconds}") as info:
            await call(_host())
        assert info.value.write is False

    async def test_create_draft_timeout_is_a_write_timeout(self, mocker) -> None:
        async def _hang(cmd, **kw):
            raise TimeoutError

        mocker.patch("dotharness.code_host.reference.run", side_effect=_hang)
        with pytest.raises(CodeHostTimeoutError) as info:
            await _host().change_requests.create_draft(REPO, head="h", base="b", title="t", body="b")
        assert info.value.write is True


class TestWorkingDirectoryAndEnvironment:
    async def test_gh_runs_in_working_directory(self, mocker, tmp_path: Path) -> None:
        mock_run = _recorder(mocker, (0, "[]", ""), (0, '{"state": "OPEN"}', ""), (0, "", ""))
        host = _create(REGISTRATION.options_model(), None, tmp_path)
        await host.work_items.list_open(REPO, assignee="a", label="l")
        await host.change_requests.state(REPO, 1)
        await host.work_items.remove_label(REPO, 1, "x")
        assert [c.kwargs["cwd"] for c in mock_run.call_args_list] == [tmp_path] * 3

    async def test_default_directory_is_process_cwd(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "[]", ""))
        await _host().work_items.list_open(REPO)
        assert mock_run.call_args.kwargs["cwd"] == Path.cwd()

    async def test_no_repo_flag_on_list_open(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "[]", ""))
        await _host().work_items.list_open(REPO, assignee="a", label="l")
        assert "--repo" not in _argv(mock_run)

    async def test_no_token_command_inherits_environment(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "[]", ""))
        await _host().work_items.list_open(REPO)
        assert mock_run.call_args.kwargs["env"] is None

    async def test_token_is_gh_token_in_gh_env_only(self, mocker, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "inherited")
        monkeypatch.setenv("KEEP", "me")
        mock_run = _recorder(mocker, (0, f"{SECRET}\n", ""), (0, "[]", ""), (0, "[]", ""))
        host = _create(REGISTRATION.options_model(), "tokencmd --flag 'a b'", None)
        await host.work_items.list_open(REPO)
        await host.work_items.list_open(REPO)
        token_call, first, second = mock_run.call_args_list
        assert token_call.args[0] == ["tokencmd", "--flag", "a b"]  # shlex.split, no shell
        assert token_call.kwargs["timeout"] == 30
        assert "env" not in token_call.kwargs
        for call in (first, second):
            env = call.kwargs["env"]
            assert env["GH_TOKEN"] == SECRET
            assert env["GITHUB_TOKEN"] == "inherited"  # noqa: S105  # inherited untouched, never set from the token
            assert env["KEEP"] == "me"
            assert SECRET not in " ".join(call.args[0])
        assert mock_run.await_count == 3  # the command ran once

    async def test_credential_returns_token_hidden_from_repr(self, mocker) -> None:
        _recorder(mocker, (0, f"  {SECRET}  \n", ""))
        host = _create(REGISTRATION.options_model(), ["tokencmd"], None)
        credential = await host.session.credential()
        assert credential.token == SECRET
        assert SECRET not in repr(credential)

    async def test_credential_without_command_raises_authentication_error(self) -> None:
        with pytest.raises(AuthenticationError):
            await _host().session.credential()

    async def test_gh_failure_message_never_holds_the_token(self, mocker) -> None:
        _recorder(mocker, (0, f"{SECRET}\n", ""), (1, "", f"bad {SECRET} here"))
        host = _create(REGISTRATION.options_model(), "tokencmd", None)
        with pytest.raises(CodeHostError) as info:
            await host.work_items.list_open(REPO)
        assert SECRET not in str(info.value)

    async def test_factory_runs_no_command(self, mocker) -> None:
        mock_run = mocker.patch("dotharness.code_host.reference.run", new_callable=AsyncMock)
        create_code_host(HostSelection(setting="s", name="reference", options={}, token_cmd="tokencmd"))  # noqa: S106
        assert mock_run.await_count == 0

    async def test_factory_passes_working_directory(self, mocker, tmp_path: Path) -> None:
        mock_run = _recorder(mocker, (0, "[]", ""))
        host = create_code_host(
            HostSelection(setting="s", name="reference", options={}, token_cmd=None, working_directory=tmp_path)
        )
        await host.work_items.list_open(REPO)
        assert mock_run.call_args.kwargs["cwd"] == tmp_path


class TestTokenCommand:
    """R-7.2.2: shlex.split, no shell, 30 s, no argv or stderr in any message."""

    async def test_nonzero_exit_carries_exit_code_as_data(self, mocker) -> None:
        _recorder(mocker, (3, "", f"secret stderr {SECRET}"))
        host = _create(REGISTRATION.options_model(), f"mycmd {SECRET}", None)
        with pytest.raises(TokenCommandError) as info:
            await host.session.credential()
        assert info.value.exit_code == 3
        assert str(info.value) == "token command failed (exit 3) or printed no token"
        for text in (str(info.value), repr(info.value), str(info.value.__cause__)):
            assert SECRET not in text
            assert "mycmd" not in text

    async def test_empty_output_raises(self, mocker) -> None:
        _recorder(mocker, (0, "  \n", ""))
        with pytest.raises(TokenCommandError) as info:
            await _create(REGISTRATION.options_model(), "mycmd", None).session.credential()
        assert info.value.exit_code == 0

    async def test_timeout_is_a_read_timeout(self, mocker) -> None:
        mock_run = _recorder(mocker, RunResult(124, "", "timed out after 30s", timed_out=True))
        host = _create(REGISTRATION.options_model(), f"mycmd {SECRET}", None)
        with pytest.raises(CodeHostTimeoutError) as info:
            await host.session.credential()
        assert info.value.write is False
        assert str(info.value) == "token command timed out after 30s"
        assert mock_run.call_args.kwargs["timeout"] == 30
        assert SECRET not in repr(info.value)

    async def test_not_found(self, mocker) -> None:
        _recorder(mocker, RunResult(127, "", f"No such file: {SECRET}", not_found=True))
        with pytest.raises(TokenCommandError) as info:
            await _create(REGISTRATION.options_model(), "mycmd", None).session.credential()
        assert info.value.detail == "OSError"
        assert SECRET not in str(info.value)

    @pytest.mark.parametrize("cmd", ["'unterminated", "   "])
    async def test_unparsable_command(self, mocker, cmd) -> None:
        mock_run = mocker.patch("dotharness.code_host.reference.run", new_callable=AsyncMock)
        with pytest.raises(TokenCommandError) as info:
            await _create(REGISTRATION.options_model(), cmd, None).session.credential()
        assert info.value.detail == "ValueError"
        assert mock_run.await_count == 0

    async def test_failed_command_stops_the_gh_call(self, mocker) -> None:
        mock_run = _recorder(mocker, (1, "", ""))
        host = _create(REGISTRATION.options_model(), "mycmd", None)
        with pytest.raises(AuthenticationError):
            await host.work_items.list_open(REPO)
        assert mock_run.await_count == 1  # gh never ran


def test_failure_redacts_token_cut_by_stderr_tail() -> None:
    """The token is redacted before the tail is cut, so a token straddling the tail boundary never leaks."""
    token = "t0k3n" + "abcdefghij" * 3 + "XYZ12"
    gh = _Gh("echo", None)
    from dotharness.code_host.models import Credential

    gh._credential = Credential(token=token)
    stderr = "x" * 100 + token + "y" * (_STDERR_TAIL - 10)
    error = gh.failure(["pr", "view"], _Outcome(success=False, stdout="", stderr=stderr, returncode=1))
    assert token not in str(error)
    assert token[-10:] not in str(error)


# --------------------------------------------------------------------------------------------------------------------
# Phase 3 operations (A-1-a): exact gh argv, environment, success, failure and timeout
# --------------------------------------------------------------------------------------------------------------------

R = "myorg/myrepo"
PR_SAMPLE = {
    "number": 7,
    "title": "T",
    "body": "B",
    "url": "https://examplehost.test/myorg/myrepo/pull/7",
    "state": "OPEN",
    "isDraft": True,
    "author": {"login": "ann", "is_bot": False},
    "headRefName": "feature/x",
    "headRefOid": "abc",
    "baseRefName": "main",
    "isCrossRepository": True,
    "createdAt": "2026-01-02T03:04:05Z",
    "mergeable": "CONFLICTING",
    "autoMergeRequest": {"enabledAt": "x"},
    "reviewDecision": "CHANGES_REQUESTED",
    "reviewRequests": [{"login": "bob"}, {"name": "team"}],
    "additions": 5,
    "deletions": 2,
}


def _pages(mocker, *results: RunResult | tuple[int, str, str]) -> AsyncMock:
    mocker.patch("dotharness.code_host.reference.asyncio.sleep", new_callable=AsyncMock)
    return _recorder(mocker, *results)


def _ok(payload: object) -> tuple[int, str, str]:
    return (0, json.dumps(payload), "")


def _page_argv(path: str, page: int = 1) -> list[str]:
    return ["gh", "api", "--method", "GET", path, "-F", "per_page=100", "-F", f"page={page}"]


async def _hang(cmd, **kw):
    raise TimeoutError


READS = [
    ("list_checks", lambda h: h.checks.list_checks(REPO, 1)),
    ("list_workflows", lambda h: h.checks.list_workflows(REPO)),
    ("failed_log", lambda h: h.checks.failed_log(REPO, Check("c", CheckStatus.FAIL, None, None, None, None, "9"))),
    ("get_job", lambda h: h.checks.get_job(REPO, "9")),
    ("current_user", lambda h: h.session.current_user()),
    ("get", lambda h: h.change_requests.get(REPO, 1)),
    ("list", lambda h: h.change_requests.list(REPO)),
    ("find_by_commit", lambda h: h.change_requests.find_by_commit(REPO, "sha")),
    ("commits", lambda h: h.change_requests.commits(REPO, 1)),
    ("reviews", lambda h: h.change_requests.reviews(REPO, 1)),
    ("viewed_files", lambda h: h.change_requests.viewed_files(REPO, 1)),
    ("comments", lambda h: h.discussion.comments(REPO, 1)),
    ("review_threads", lambda h: h.discussion.review_threads(REPO, 1)),
    ("work_item", lambda h: h.work_items.get(REPO, 1)),
    ("linked", lambda h: h.work_items.linked_work_items(REPO, 1)),
]
WRITES = [
    ("request_reviewer", lambda h: h.change_requests.request_reviewer(REPO, 1, "bob")),
    ("approve", lambda h: h.change_requests.approve(REPO, 1, head_revision="abc")),
    ("squash_merge", lambda h: h.change_requests.squash_merge(REPO, 1, expected_head="abc")),
    ("mark_viewed_mutation", None),
    ("post_comment", lambda h: h.discussion.post_comment(REPO, 1, "hi")),
    ("reply", lambda h: h.discussion.reply(REPO, 1, "T1", "hi")),
]


class TestSessionOperations:
    async def test_current_user(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "ann\n", ""))
        assert await _host().session.current_user() == "ann"
        assert _argv(mock_run) == ["gh", "api", "user", "--jq", ".login"]

    async def test_current_user_blank_is_authentication_error(self, mocker) -> None:
        _recorder(mocker, (0, "\n", ""))
        with pytest.raises(AuthenticationError):
            await _host().session.current_user()

    async def test_current_user_failure_and_auth_failure(self, mocker) -> None:
        _recorder(mocker, (1, "", "boom"), (1, "", "HTTP 401: Bad credentials"))
        with pytest.raises(CodeHostError, match=r"gh api user failed \(exit 1\): boom"):
            await _host().session.current_user()
        with pytest.raises(AuthenticationError):
            await _host().session.current_user()

    async def test_current_user_runs_with_gh_token_only(self, mocker, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        mock_run = _recorder(mocker, (0, f"{SECRET}\n", ""), (0, "ann\n", ""))
        host = _create(REGISTRATION.options_model(), "tokencmd", None)
        await host.session.current_user()
        env = mock_run.call_args.kwargs["env"]
        assert env["GH_TOKEN"] == SECRET
        assert "GITHUB_TOKEN" not in env

    async def test_agent_environment_names_are_data(self) -> None:
        assert tuple(await _host().session.agent_environment_names()) == ("GITHUB_TOKEN", "GH_TOKEN")

    async def test_agent_environment_adds_both_variables_and_credential_wins(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, f"{SECRET}\n", ""))
        host = _create(REGISTRATION.options_model(), ["tokencmd", "--x"], None)
        env = await host.session.agent_environment({"KEEP": "me", "GH_TOKEN": "old", "GITHUB_TOKEN": "old"})
        assert env == {"KEEP": "me", "GH_TOKEN": SECRET, "GITHUB_TOKEN": SECRET}
        assert _argv(mock_run) == ["tokencmd", "--x"]
        assert mock_run.await_count == 1  # no gh call
        assert await host.session.agent_environment() == {"GH_TOKEN": SECRET, "GITHUB_TOKEN": SECRET}
        assert mock_run.await_count == 1  # the token command runs once

    async def test_agent_environment_without_token_command_raises(self) -> None:
        with pytest.raises(AuthenticationError):
            await _host().session.agent_environment({"A": "b"})

    async def test_agent_environment_token_command_failure_never_shows_the_command(self, mocker) -> None:
        _recorder(mocker, (2, "", SECRET))
        host = _create(REGISTRATION.options_model(), f"mycmd {SECRET}", None)
        with pytest.raises(TokenCommandError) as info:
            await host.session.agent_environment()
        assert SECRET not in str(info.value)

    async def test_agent_environment_token_timeout(self, mocker) -> None:
        _recorder(mocker, RunResult(124, "", "", timed_out=True))
        host = _create(REGISTRATION.options_model(), "mycmd", None)
        with pytest.raises(CodeHostTimeoutError) as info:
            await host.session.agent_environment()
        assert info.value.write is False


class TestRepositoryOperations:
    async def test_resolve_runs_no_command(self, mocker) -> None:
        mock_run = mocker.patch("dotharness.code_host.reference.run", new_callable=AsyncMock)
        assert await _host().repository.resolve("  org/repo ") == RepositoryRef("org/repo")
        assert await _host().repository.resolve("org/repo") == await _host().repository.resolve("org/repo")
        assert mock_run.await_count == 0

    @pytest.mark.parametrize("identity", ["", "org", "a/b/c", "a b/c", "/repo", "org/"])
    async def test_resolve_rejects_other_shapes(self, identity) -> None:
        with pytest.raises(ConfigurationError):
            await _host().repository.resolve(identity)

    async def test_display_name(self) -> None:
        assert await _host().repository.display_name(REPO) == R


class TestChangeRequestReads:
    async def test_get_maps_every_field(self, mocker) -> None:
        mock_run = _recorder(mocker, _ok(PR_SAMPLE))
        cr = await _host().change_requests.get(REPO, 7)
        assert _argv(mock_run) == ["gh", "pr", "view", "7", "--repo", R, "--json", _PR_FULL_FIELDS]
        assert {"mergeable", "autoMergeRequest", "reviewRequests", "additions", "deletions"} <= set(
            _PR_FULL_FIELDS.split(",")
        )
        assert cr.state is ChangeRequestState.OPEN
        assert (cr.title, cr.body, cr.url, cr.is_draft) == ("T", "B", PR_SAMPLE["url"], True)
        assert (cr.author.login, cr.author.is_bot) == ("ann", False)
        assert (cr.head_reference, cr.head_revision, cr.base_reference) == ("feature/x", "abc", "main")
        assert cr.is_cross_repository is True
        assert cr.created_at.year == 2026
        assert cr.mergeable is MergeableState.CONFLICTING
        assert cr.auto_merge_enabled is True
        assert cr.review_decision is ReviewDecision.CHANGES_REQUESTED
        assert [u.login for u in cr.requested_reviewers] == ["bob"]  # a team request has no login
        assert (cr.additions, cr.deletions) == (5, 2)

    async def test_get_sparse_response(self, mocker) -> None:
        _recorder(mocker, _ok({"state": "MERGED", "reviewDecision": ""}))
        cr = await _host().change_requests.get(REPO, 9)
        assert (cr.number, cr.state, cr.mergeable, cr.review_decision) == (9, ChangeRequestState.MERGED, None, None)
        assert (cr.auto_merge_enabled, cr.requested_reviewers, cr.additions) == (False, (), None)

    async def test_get_failure_and_bad_payload(self, mocker) -> None:
        _recorder(mocker, (1, "", "Could not resolve to a PullRequest"), (0, "[]", ""))
        with pytest.raises(CodeHostError, match="Could not resolve"):
            await _host().change_requests.get(REPO, 1)
        with pytest.raises(CodeHostError, match="unexpected response"):
            await _host().change_requests.get(REPO, 1)

    async def test_list_default_argv_and_mapping(self, mocker) -> None:
        mock_run = _recorder(mocker, _ok([{"number": 1, "headRefName": "a", "isDraft": True}, "junk"]))
        found = await _host().change_requests.list(REPO)
        assert _argv(mock_run) == [
            *("gh", "pr", "list", "--repo", R, "--state", "open"),
            *("--limit", "1001", "--json", _PR_FIELDS),
        ]
        assert [(c.number, c.head_reference, c.is_draft) for c in found] == [(1, "a", True)]

    async def test_list_filters_are_logins(self, mocker) -> None:
        mock_run = _recorder(mocker, _ok([]), _ok([]))
        cr = _host().change_requests
        await cr.list(REPO, state=ChangeRequestState.MERGED, author="ann", assignee="bob", requested_reviewer="cy")
        assert _argv(mock_run) == [
            *("gh", "pr", "list", "--repo", R, "--state", "merged"),
            *("--author", "ann", "--assignee", "bob", "--search", "user-review-requested:cy"),
            *("--limit", "1001", "--json", _PR_FIELDS),
        ]
        await cr.list(REPO, state=ChangeRequestState.CLOSED)
        assert "@me" not in " ".join(_argv(mock_run, 1))
        assert _argv(mock_run, 1)[5:7] == ["--state", "closed"]

    async def test_list_failure(self, mocker) -> None:
        _recorder(mocker, (1, "", "boom"))
        with pytest.raises(CodeHostError, match="boom"):
            await _host().change_requests.list(REPO)

    async def test_list_over_bound_raises_instead_of_truncating(self, mocker) -> None:
        rows = [{"number": n, "headRefName": "a"} for n in range(1, 1002)]
        _recorder(mocker, _ok(rows))
        with pytest.raises(CodeHostError, match="more than the declared bound of 1000"):
            await _host().change_requests.list(REPO)

    async def test_find_by_commit(self, mocker) -> None:
        rest = {
            "number": 3,
            "title": "t",
            "body": "b",
            "html_url": "u",
            "state": "closed",
            "merged_at": "2026-01-01T00:00:00Z",
            "draft": False,
            "user": {"login": "dep[bot]", "type": "Bot"},
            "head": {"ref": "h", "sha": "s", "repo": {"full_name": "fork/repo"}},
            "base": {"ref": "main", "repo": {"full_name": R}},
            "created_at": "2026-01-01T00:00:00Z",
        }
        mock_run = _pages(mocker, _ok([rest]))
        found = await _host().change_requests.find_by_commit(REPO, "deadbeef")
        assert _argv(mock_run) == _page_argv(f"repos/{R}/commits/deadbeef/pulls")
        (cr,) = found
        assert (cr.number, cr.state, cr.head_reference, cr.head_revision, cr.base_reference) == (
            3,
            ChangeRequestState.MERGED,
            "h",
            "s",
            "main",
        )
        assert cr.is_cross_repository is True
        assert cr.author.is_bot is True
        assert cr.url == "u"

    async def test_find_by_commit_open_pull_request(self, mocker) -> None:
        _pages(mocker, _ok([{"number": 4, "state": "open"}]))
        (cr,) = await _host().change_requests.find_by_commit(REPO, "x")
        assert (cr.state, cr.author, cr.is_cross_repository) == (ChangeRequestState.OPEN, None, False)

    async def test_find_by_commit_over_the_declared_bound_raises(self, mocker) -> None:
        page = [{"number": i} for i in range(100)]
        _pages(mocker, _ok(page), _ok([{"number": 100}]))
        with pytest.raises(CodeHostError, match="declared bound of 100"):
            await _host().change_requests.find_by_commit(REPO, "x")

    async def test_commits(self, mocker) -> None:
        rows = [
            {
                "sha": "a1",
                "commit": {"message": "m", "committer": {"date": "2026-01-01T00:00:00Z"}},
                "author": {"login": "ann", "type": "User"},
            },
            {"sha": "b2", "commit": {}},
        ]
        mock_run = _pages(mocker, _ok(rows))
        commits = await _host().change_requests.commits(REPO, 5)
        assert _argv(mock_run) == _page_argv(f"repos/{R}/pulls/5/commits")
        assert [c.revision for c in commits] == ["a1", "b2"]
        assert (commits[0].message, [u.login for u in commits[0].authors]) == ("m", ["ann"])
        assert commits[0].committed_at is not None
        assert (commits[1].message, commits[1].authors, commits[1].committed_at) == (None, (), None)

    async def test_reviews_are_never_truncated(self, mocker) -> None:
        first = [{"id": i, "state": "COMMENTED"} for i in range(100)]
        last = [{"id": 100, "state": "APPROVED", "user": {"login": "ann"}, "commit_id": "abc", "body": "ok"}]
        mock_run = _pages(mocker, _ok(first), _ok(last))
        reviews = await _host().change_requests.reviews(REPO, 5)
        assert [_argv(mock_run, i) for i in range(2)] == [
            _page_argv(f"repos/{R}/pulls/5/reviews", 1),
            _page_argv(f"repos/{R}/pulls/5/reviews", 2),
        ]
        assert len(reviews) == 101
        assert (reviews[-1].state, reviews[-1].author.login, reviews[-1].revision) == (
            ReviewState.APPROVED,
            "ann",
            "abc",
        )
        assert (reviews[0].revision, reviews[0].author, reviews[0].submitted_at) == (None, None, None)

    async def test_reviews_unknown_state_is_commented(self, mocker) -> None:
        _pages(mocker, _ok([{"id": 1, "state": "WEIRD", "submitted_at": "2026-01-01T00:00:00Z"}]))
        (review,) = await _host().change_requests.reviews(REPO, 5)
        assert review.state is ReviewState.COMMENTED
        assert review.submitted_at is not None


class TestPagingRetries:
    async def test_declared_values_are_the_reference_values(self) -> None:
        assert (DECLARATION.limits.page_attempts, DECLARATION.limits.page_retry_delay_seconds) == (3, 2.0)
        assert (_PAGE_ATTEMPTS, _PAGE_RETRY_DELAY) == (3, 2.0)

    async def test_a_failed_page_is_retried_with_growing_delay(self, mocker) -> None:
        mock_run = _pages(mocker, (1, "", "5xx"), (1, "", "5xx"), _ok([{"id": 1, "state": "APPROVED"}]))
        sleep = mocker.patch("dotharness.code_host.reference.asyncio.sleep", new_callable=AsyncMock)
        assert len(await _host().change_requests.reviews(REPO, 5)) == 1
        assert mock_run.await_count == 3
        assert [c.args for c in sleep.await_args_list] == [(2.0,), (4.0,)]

    async def test_all_attempts_failing_raises(self, mocker, caplog) -> None:
        mock_run = _pages(mocker, (1, "", "5xx"), (1, "", "5xx"), (1, "", "5xx"))
        sleep = mocker.patch("dotharness.code_host.reference.asyncio.sleep", new_callable=AsyncMock)
        with caplog.at_level(logging.WARNING), pytest.raises(CodeHostError, match="5xx"):
            await _host().change_requests.reviews(REPO, 5)
        assert mock_run.await_count == 3
        assert sleep.await_count == 2
        assert "attempt 1/3" in caplog.text

    async def test_a_later_page_failure_raises(self, mocker) -> None:
        _pages(mocker, _ok([{"id": i} for i in range(100)]), (1, "", "x"), (1, "", "x"), (1, "", "x"))
        with pytest.raises(CodeHostError):
            await _host().change_requests.reviews(REPO, 5)

    async def test_authentication_failure_is_not_retried(self, mocker) -> None:
        mock_run = _pages(mocker, (1, "", "HTTP 401: Bad credentials"))
        with pytest.raises(AuthenticationError):
            await _host().change_requests.reviews(REPO, 5)
        assert mock_run.await_count == 1

    async def test_timeout_is_not_retried(self, mocker) -> None:
        mocker.patch("dotharness.code_host.reference.run", side_effect=_hang)
        with pytest.raises(CodeHostTimeoutError) as info:
            await _host().change_requests.reviews(REPO, 5)
        assert info.value.write is False

    @pytest.mark.parametrize("out", ["nope", '{"a": 1}'])
    async def test_bad_payload_raises(self, mocker, out) -> None:
        mock_run = _pages(mocker, (0, out, ""))
        with pytest.raises(CodeHostError, match="gh api returned"):
            await _host().change_requests.reviews(REPO, 5)
        assert mock_run.await_count == 1

    async def test_token_never_in_logged_failures(self, mocker, caplog) -> None:
        results = [(0, f"{SECRET}\n", ""), *[(1, "", f"bad {SECRET}")] * 3]
        _pages(mocker, *results)
        host = _create(REGISTRATION.options_model(), "tokencmd", None)
        with caplog.at_level(logging.WARNING), pytest.raises(CodeHostError) as info:
            await host.change_requests.reviews(REPO, 1)
        assert SECRET not in caplog.text
        assert SECRET not in str(info.value)

    async def test_unbounded_when_declared_none(self) -> None:
        for op in ("Discussion.comments", "Discussion.review_threads", "ChangeRequests.reviews"):
            assert DECLARATION.limits.list_bounds.per_operation[op] is None
        assert DECLARATION.limits.list_bounds.per_operation["ChangeRequests.list"] == 1000


class TestChangeRequestWrites:
    async def test_request_reviewer(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "", ""))
        await _host().change_requests.request_reviewer(REPO, 7, "bob")
        assert _argv(mock_run) == ["gh", "pr", "edit", "7", "--repo", R, "--add-reviewer", "bob"]

    async def test_approve_pins_the_revision(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "{}", ""))
        await _host().change_requests.approve(REPO, 7, head_revision="abc")
        assert _argv(mock_run) == [
            *("gh", "api", "--method", "POST", f"repos/{R}/pulls/7/reviews"),
            *("-f", "event=APPROVE", "-f", "commit_id=abc"),
        ]

    async def test_squash_merge_pins_the_head(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "", ""))
        await _host().change_requests.squash_merge(REPO, 7, expected_head="abc")
        assert _argv(mock_run) == [
            *("gh", "pr", "merge", "7", "--repo", R, "--squash", "--match-head-commit", "abc"),
        ]

    @pytest.mark.parametrize(
        "call",
        [
            lambda h: h.change_requests.request_reviewer(REPO, 7, "bob"),
            lambda h: h.change_requests.approve(REPO, 7, head_revision="abc"),
            lambda h: h.change_requests.squash_merge(REPO, 7, expected_head="abc"),
        ],
    )
    async def test_failure_carries_exit_code(self, mocker, call) -> None:
        _recorder(mocker, (1, "", "head branch was modified"))
        with pytest.raises(CodeHostError, match=r"failed \(exit 1\): head branch was modified"):
            await call(_host())

    async def test_viewed_files_pages_and_flags(self, mocker) -> None:
        def page(nodes, nxt):
            return _ok({
                "data": {
                    "repository": {
                        "pullRequest": {
                            "files": {"nodes": nodes, "pageInfo": {"hasNextPage": bool(nxt), "endCursor": nxt}}
                        }
                    }
                }
            })

        mock_run = _recorder(
            mocker,
            page([{"path": "a.py", "viewerViewedState": "VIEWED"}, "junk"], "C1"),
            page(
                [{"path": "b.py", "viewerViewedState": "UNVIEWED"}, {"path": "c.py", "viewerViewedState": "DISMISSED"}],
                None,
            ),
        )
        viewed = await _host().change_requests.viewed_files(REPO, 7)
        assert viewed == {"a.py": True, "b.py": False, "c.py": False}
        base = ["gh", "api", "graphql", "-f", f"query={_FILES_QUERY}", "-f", "owner=myorg", "-f", "repo=myrepo"]
        assert _argv(mock_run, 0) == [*base, "-F", "number=7"]
        assert _argv(mock_run, 1) == [*base, "-F", "number=7", "-f", "cursor=C1"]

    async def test_viewed_files_failures(self, mocker) -> None:
        more = _ok({
            "data": {"repository": {"pullRequest": {"files": {"nodes": [], "pageInfo": {"hasNextPage": True}}}}}
        })
        shape = _ok({"data": {"repository": {"pullRequest": {"files": {"nodes": {}}}}}})
        _recorder(
            mocker,
            more,
            shape,
            _ok({"data": {"repository": None}}),
            (1, "", "boom"),
            (0, "x", ""),
            _ok({"errors": [{}]}),
        )
        cr = _host().change_requests
        with pytest.raises(CodeHostError, match="no cursor"):
            await cr.viewed_files(REPO, 7)
        with pytest.raises(CodeHostError, match="unexpected files response"):
            await cr.viewed_files(REPO, 7)
        with pytest.raises(CodeHostError, match="unexpected response"):
            await cr.viewed_files(REPO, 7)
        with pytest.raises(CodeHostError, match="boom"):
            await cr.viewed_files(REPO, 7)
        with pytest.raises(CodeHostError, match="errors or no data"):
            await cr.viewed_files(REPO, 7)
        with pytest.raises(CodeHostError, match="errors or no data"):
            await cr.viewed_files(REPO, 7)

    async def test_bad_identity_raises_before_any_call(self, mocker) -> None:
        mock_run = mocker.patch("dotharness.code_host.reference.run", new_callable=AsyncMock)
        with pytest.raises(CodeHostError, match="owner/repo"):
            await _host().change_requests.viewed_files(RepositoryRef("nope"), 1)
        assert mock_run.await_count == 0

    async def test_mark_viewed_reads_the_node_id_then_writes(self, mocker) -> None:
        mock_run = _recorder(
            mocker,
            _ok({"data": {"repository": {"pullRequest": {"id": "PR_1"}}}}),
            _ok({"data": {"markFileAsViewed": {"clientMutationId": None}}}),
        )
        await _host().change_requests.mark_viewed(REPO, 7, "src/a.py")
        assert _argv(mock_run, 0) == [
            *("gh", "api", "graphql", "-f", f"query={_NODE_ID_QUERY}", "-f", "owner=myorg", "-f", "repo=myrepo"),
            *("-F", "number=7"),
        ]
        assert _argv(mock_run, 1) == [
            *("gh", "api", "graphql", "-f", f"query={_MARK_VIEWED_MUTATION}"),
            *("-f", "pullRequestId=PR_1", "-f", "path=src/a.py"),
        ]

    async def test_mark_viewed_failures(self, mocker) -> None:
        _recorder(
            mocker,
            _ok({"data": {"repository": {"pullRequest": {"id": ""}}}}),
            _ok({"data": {"repository": {"pullRequest": {"id": "PR_1"}}}}),
            (1, "", "boom"),
        )
        with pytest.raises(CodeHostError, match="no pull request id"):
            await _host().change_requests.mark_viewed(REPO, 7, "p")
        with pytest.raises(CodeHostError, match="boom"):
            await _host().change_requests.mark_viewed(REPO, 7, "p")


def _thread_page(nodes, next_cursor=None, *, has_next=None):
    return _ok({
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": nodes,
                        "pageInfo": {
                            "hasNextPage": bool(next_cursor) if has_next is None else has_next,
                            "endCursor": next_cursor,
                        },
                    }
                }
            }
        }
    })


def _thread(tid="T1", **extra):
    node = {
        "id": tid,
        "isResolved": True,
        "path": "src/a.py",
        "line": 12,
        "originalLine": 10,
        "diffSide": "LEFT",
        "comments": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "id": "NODE1",
                    "databaseId": 99,
                    "isMinimized": True,
                    "body": "fix",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "url": "u",
                    "diffHunk": "@@",
                    "author": {"login": "bot[bot]", "__typename": "Bot"},
                },
                {"id": "NODE2", "body": "b", "author": None, "diffHunk": ""},
            ],
        },
    }
    node.update(extra)
    return node


class TestDiscussionOperations:
    async def test_comments(self, mocker) -> None:
        rows = [
            {
                "id": 5,
                "user": {"login": "ann", "type": "User"},
                "body": "b",
                "created_at": "2026-01-01T00:00:00Z",
                "html_url": "u",
            },
            {"id": 6, "user": {"login": "x[bot]", "type": "Bot"}, "body": "c"},
        ]
        mock_run = _pages(mocker, _ok(rows))
        comments = await _host().discussion.comments(REPO, 3)
        assert _argv(mock_run) == _page_argv(f"repos/{R}/issues/3/comments")
        assert [(c.id, c.author.login, c.author.is_bot, c.body) for c in comments] == [
            ("5", "ann", False, "b"),
            ("6", "x[bot]", True, "c"),
        ]
        assert (comments[0].url, comments[1].url, comments[0].is_hidden) == ("u", None, False)

    async def test_comments_are_never_truncated(self, mocker) -> None:
        _pages(mocker, _ok([{"id": i} for i in range(100)]), _ok([{"id": 100}]))
        assert len(await _host().discussion.comments(REPO, 3)) == 101

    async def test_review_threads_paged(self, mocker) -> None:
        mock_run = _recorder(mocker, _thread_page([_thread("T1")], "CUR"), _thread_page([_thread("T2", path=None)]))
        threads = await _host().discussion.review_threads(REPO, 3)
        base = [*("gh", "api", "graphql", "-f", f"query={_THREADS_QUERY}", "-f", "owner=myorg", "-f", "repo=myrepo")]
        assert _argv(mock_run, 0) == [*base, "-F", "number=3"]
        assert _argv(mock_run, 1) == [*base, "-F", "number=3", "-f", "cursor=CUR"]
        first, second = threads
        assert (first.id, first.is_resolved, first.anchor) == ("T1", True, Anchor("src/a.py", 12, "LEFT"))
        assert second.anchor is None
        root, other = first.comments
        assert (root.id, root.is_hidden, root.diff_hunk, root.url) == ("99", True, "@@", "u")
        assert (root.author.login, root.author.is_bot) == ("bot[bot]", True)
        assert (other.id, other.author, other.diff_hunk) == ("NODE2", None, None)

    async def test_review_thread_anchor_falls_back_to_original_line(self, mocker) -> None:
        _recorder(mocker, _thread_page([_thread(line=None, diffSide=None)]))
        (thread,) = await _host().discussion.review_threads(REPO, 3)
        assert thread.anchor == Anchor("src/a.py", 10, "RIGHT")

    async def test_review_threads_failures(self, mocker) -> None:
        overflow = _thread()
        overflow["comments"] = {"pageInfo": {"hasNextPage": True}, "nodes": []}
        _recorder(
            mocker,
            _thread_page([_thread()], has_next=True),
            _thread_page([overflow]),
            _ok({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": 1}}}}}),
            (1, "", "boom"),
        )
        d = _host().discussion
        with pytest.raises(CodeHostError, match="no cursor"):
            await d.review_threads(REPO, 3)
        with pytest.raises(CodeHostError, match="more comments"):
            await d.review_threads(REPO, 3)
        with pytest.raises(CodeHostError, match="unexpected review thread"):
            await d.review_threads(REPO, 3)
        with pytest.raises(CodeHostError, match="boom"):
            await d.review_threads(REPO, 3)

    async def test_post_comment(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "https://examplehost.test/myorg/myrepo/pull/3#issuecomment-123\n", ""))
        comment = await _host().discussion.post_comment(REPO, 3, "hello")
        assert _argv(mock_run) == ["gh", "pr", "comment", "3", "--repo", R, "--body", "hello"]
        assert (comment.id, comment.body, comment.url) == (
            "123",
            "hello",
            "https://examplehost.test/myorg/myrepo/pull/3#issuecomment-123",
        )

    async def test_post_comment_without_url(self, mocker) -> None:
        _recorder(mocker, (0, "", ""))
        comment = await _host().discussion.post_comment(REPO, 3, "hello")
        assert (comment.id, comment.url) == ("", None)

    async def test_post_comment_failure(self, mocker) -> None:
        _recorder(mocker, (1, "", "nope"))
        with pytest.raises(CodeHostError, match="nope"):
            await _host().discussion.post_comment(REPO, 3, "hello")

    async def test_reply(self, mocker) -> None:
        reply = {"id": "N", "databaseId": 55, "body": "hi", "author": {"login": "me", "__typename": "User"}}
        mock_run = _recorder(mocker, _ok({"data": {"addPullRequestReviewThreadReply": {"comment": reply}}}))
        comment = await _host().discussion.reply(REPO, 3, "T1", "hi")
        assert _argv(mock_run) == [
            *("gh", "api", "graphql", "-f", f"query={_REPLY_MUTATION}"),
            *("-f", "threadId=T1", "-f", "body=hi"),
        ]
        assert (comment.id, comment.body, comment.author.login) == ("55", "hi", "me")

    async def test_reply_failures(self, mocker) -> None:
        _recorder(mocker, (1, "", "boom"), _ok({"data": {"addPullRequestReviewThreadReply": None}}))
        with pytest.raises(CodeHostError, match="boom"):
            await _host().discussion.reply(REPO, 3, "T1", "hi")
        with pytest.raises(CodeHostError, match="unexpected response"):
            await _host().discussion.reply(REPO, 3, "T1", "hi")


class TestWorkItemOperations:
    ROW: ClassVar[dict] = {
        "number": 4,
        "title": "T",
        "body": "B",
        "labels": [{"name": "bug"}],
        "assignees": [{"login": "ann"}],
        "createdAt": "2026-01-01T00:00:00Z",
        "state": "CLOSED",
    }

    async def test_get(self, mocker) -> None:
        mock_run = _recorder(mocker, _ok(self.ROW))
        item = await _host().work_items.get(REPO, 4)
        assert _argv(mock_run) == [
            *("gh", "issue", "view", "4", "--repo", R),
            *("--json", "number,title,body,labels,assignees,createdAt,state"),
        ]
        assert item is not None
        assert (item.number, item.title, item.body, item.labels, item.state) == (
            4,
            "T",
            "B",
            ("bug",),
            WorkItemState.CLOSED,
        )
        assert [u.login for u in item.assignees] == ["ann"]

    async def test_get_confirmed_absence_is_none(self, mocker) -> None:
        _recorder(mocker, (1, "", "GraphQL: Could not resolve to an issue with the number of 9."))
        assert await _host().work_items.get(REPO, 9) is None

    @pytest.mark.parametrize(
        ("result", "match"),
        [
            ((1, "", "HTTP 502: bad gateway"), "bad gateway"),
            ((1, "", "HTTP 401: Bad credentials"), "Bad credentials"),
            ((0, "not json", ""), "invalid JSON"),
            ((0, "[]", ""), "unexpected response"),
        ],
    )
    async def test_get_inconclusive_failure_raises(self, mocker, result, match) -> None:
        _recorder(mocker, result)
        with pytest.raises(CodeHostError, match=match):
            await _host().work_items.get(REPO, 9)

    async def test_linked_work_items(self, mocker) -> None:
        refs = {
            "closingIssuesReferences": [
                {"number": 4, "repository": {"name": "myrepo", "owner": {"login": "myorg"}}},
                {"number": 4, "repository": {"nameWithOwner": "MyOrg/MyRepo"}},  # same reference twice: one read
                {"number": 8, "repository": {"nameWithOwner": "other/repo"}},
                {"number": 9},
                {"number": 0},
                "junk",
            ]
        }
        mock_run = _recorder(
            mocker,
            _ok(refs),
            _ok(self.ROW),
            _ok({**self.ROW, "number": 8}),
            (1, "", "Could not resolve to an issue with the number of 9"),
        )
        items = await _host().work_items.linked_work_items(REPO, 3)
        assert _argv(mock_run, 0) == ["gh", "pr", "view", "3", "--repo", R, "--json", "closingIssuesReferences"]
        assert [_argv(mock_run, i)[3:6] for i in (1, 2, 3)] == [
            ["4", "--repo", R],
            ["8", "--repo", "other/repo"],
            ["9", "--repo", R],
        ]
        assert [(i.repository.value, i.number) for i in items] == [(R, 4), ("other/repo", 8)]

    async def test_linked_work_items_none(self, mocker) -> None:
        _recorder(mocker, _ok({"closingIssuesReferences": []}), _ok({}))
        assert list(await _host().work_items.linked_work_items(REPO, 3)) == []
        assert list(await _host().work_items.linked_work_items(REPO, 3)) == []

    async def test_linked_work_items_inconclusive_failure_raises(self, mocker) -> None:
        _recorder(mocker, _ok({"closingIssuesReferences": [{"number": 4}]}), (1, "", "HTTP 502"))
        with pytest.raises(CodeHostError, match="HTTP 502"):
            await _host().work_items.linked_work_items(REPO, 3)


class TestPhase3Timeouts:
    @pytest.mark.parametrize("call", [c for _, c in READS], ids=[n for n, _ in READS])
    async def test_reads_time_out_as_read_timeouts(self, mocker, call) -> None:
        mocker.patch("dotharness.code_host.reference.run", side_effect=_hang)
        with pytest.raises(CodeHostTimeoutError, match="timed out after 30s") as info:
            await call(_host())
        assert info.value.write is False

    @pytest.mark.parametrize("call", [c for _, c in WRITES if c is not None], ids=[n for n, c in WRITES if c])
    async def test_writes_time_out_as_write_timeouts(self, mocker, call) -> None:
        mocker.patch("dotharness.code_host.reference.run", side_effect=_hang)
        with pytest.raises(CodeHostTimeoutError, match="timed out after 30s") as info:
            await call(_host())
        assert info.value.write is True

    async def test_mark_viewed_mutation_times_out_as_a_write(self, mocker) -> None:
        calls = iter([_ok({"data": {"repository": {"pullRequest": {"id": "PR_1"}}}})])

        async def _run(cmd, **kw):
            try:
                return RunResult(*next(calls))
            except StopIteration:
                raise TimeoutError from None

        mocker.patch("dotharness.code_host.reference.run", side_effect=_run)
        with pytest.raises(CodeHostTimeoutError) as info:
            await _host().change_requests.mark_viewed(REPO, 1, "p")
        assert info.value.write is True

    async def test_mark_viewed_node_id_read_times_out_as_a_read(self, mocker) -> None:
        mocker.patch("dotharness.code_host.reference.run", side_effect=_hang)
        with pytest.raises(CodeHostTimeoutError) as info:
            await _host().change_requests.mark_viewed(REPO, 1, "p")
        assert info.value.write is False

    def test_no_new_per_operation_key(self) -> None:
        assert set(DECLARATION.limits.timeouts.per_operation) == {
            "ChangeRequests.state",
            "Session.available",
            "ChangeRequests.find_by_branch",
            "WorkItems.remove_label",
            "Discussion.post_review",
        }
        assert DECLARATION.limits.timeouts.default_seconds == 30


class TestPhase3Environment:
    @pytest.mark.parametrize("call", [c for _, c in READS[1:]], ids=[n for n, _ in READS[1:]])
    async def test_gh_children_get_gh_token_only(self, mocker, monkeypatch, call) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", INHERITED)
        mock_run = _stub(
            mocker,
            lambda cmd, kw: RunResult(0, f"{SECRET}\n", "") if cmd == ["tokencmd"] else RunResult(0, "[]", ""),
        )
        host = _create(REGISTRATION.options_model(), "tokencmd", None)
        with contextlib.suppress(CodeHostError):  # an empty payload may be unusable. Only the environment matters here.
            await call(host)
        gh_env = mock_run.call_args_list[-1].kwargs["env"]
        assert gh_env["GH_TOKEN"] == SECRET
        assert gh_env["GITHUB_TOKEN"] == INHERITED
        assert SECRET not in " ".join(c.args[0][0] + " ".join(c.args[0]) for c in mock_run.call_args_list[1:])


# --------------------------------------------------------------------------------------------------------------------
# Checks: exact gh argv, mapping, exit codes, environment, errors
# --------------------------------------------------------------------------------------------------------------------

_CHECK_JSON = "name,bucket,link,workflow,startedAt,completedAt"
_RUN_URL = "https://examplehost.test/myorg/myrepo/actions/runs/55/job/77"


def _check_row(**over) -> dict:
    row = {
        "name": "build",
        "bucket": "pass",
        "link": _RUN_URL,
        "workflow": "CI",
        "startedAt": "2024-01-02T03:04:05Z",
        "completedAt": "2024-01-02T03:10:05Z",
    }
    return {**row, **over}


def _check(
    *,
    details_url: str | None = None,
    job_id: str | None = None,
    name: str = "c",
    status: CheckStatus = CheckStatus.FAIL,
) -> Check:
    return Check(name, status, details_url, None, None, None, job_id)


class TestChecks:
    async def test_list_checks_argv_and_mapping(self, mocker) -> None:
        mock_run = _recorder(mocker, _ok([_check_row()]))
        (check,) = await _host().checks.list_checks(REPO, 4)
        assert mock_run.await_count == 1
        assert _argv(mock_run) == ["gh", "pr", "checks", "4", "--repo", "myorg/myrepo", "--json", _CHECK_JSON]
        assert (check.name, check.status, check.details_url, check.workflow_name, check.job_id) == (
            "build",
            CheckStatus.PASS,
            _RUN_URL,
            "CI",
            "77",
        )
        assert check.started_at.isoformat() == "2024-01-02T03:04:05+00:00"
        assert check.completed_at.isoformat() == "2024-01-02T03:10:05+00:00"

    async def test_list_checks_required_only_adds_the_flag(self, mocker) -> None:
        mock_run = _recorder(mocker, _ok([]))
        assert await _host().checks.list_checks(REPO, 4, required_only=True) == []
        assert _argv(mock_run) == [
            "gh", "pr", "checks", "4", "--repo", "myorg/myrepo", "--json", _CHECK_JSON, "--required",
        ]  # fmt: skip

    @pytest.mark.parametrize(
        ("bucket", "status"),
        [
            ("pass", CheckStatus.PASS),
            ("fail", CheckStatus.FAIL),
            ("pending", CheckStatus.PENDING),
            ("skipping", CheckStatus.SKIPPING),
            ("cancel", CheckStatus.CANCEL),
        ],
    )
    async def test_every_bucket_maps(self, mocker, bucket, status) -> None:
        _recorder(mocker, _ok([_check_row(bucket=bucket)]))
        assert (await _host().checks.list_checks(REPO, 1))[0].status is status

    async def test_zero_year_completed_at_is_none(self, mocker) -> None:
        _recorder(mocker, _ok([_check_row(bucket="pending", completedAt="0001-01-01T00:00:00Z")]))
        (check,) = await _host().checks.list_checks(REPO, 1)
        assert check.completed_at is None
        assert check.started_at is not None

    async def test_missing_optional_fields_are_none(self, mocker) -> None:
        _recorder(mocker, _ok([{"bucket": "fail", "startedAt": "garbage", "completedAt": "", "link": ""}]))
        (check,) = await _host().checks.list_checks(REPO, 1)
        assert (check.name, check.details_url, check.workflow_name, check.job_id) == ("<unnamed>", None, None, None)
        assert (check.started_at, check.completed_at) == (None, None)

    async def test_naive_timestamp_gets_utc(self, mocker) -> None:
        _recorder(mocker, _ok([_check_row(startedAt="2024-01-02T03:04:05")]))
        (check,) = await _host().checks.list_checks(REPO, 1)
        assert check.started_at.isoformat() == "2024-01-02T03:04:05+00:00"

    async def test_all_runs_are_kept_in_order(self, mocker) -> None:
        _recorder(mocker, _ok([_check_row(bucket="fail"), _check_row(bucket="pass")]))
        got = await _host().checks.list_checks(REPO, 1)
        assert [c.status for c in got] == [CheckStatus.FAIL, CheckStatus.PASS]

    async def test_pending_exit_with_json_is_not_a_failure(self, mocker) -> None:
        _recorder(mocker, (8, json.dumps([_check_row(bucket="pending")]), ""))
        (check,) = await _host().checks.list_checks(REPO, 1)
        assert check.status is CheckStatus.PENDING

    async def test_pending_exit_without_json_raises(self, mocker) -> None:
        _recorder(mocker, (8, " ", "pending"))
        with pytest.raises(CodeHostError, match=r"gh pr checks failed \(exit 8\)"):
            await _host().checks.list_checks(REPO, 1)

    async def test_no_checks_reported_is_empty(self, mocker) -> None:
        _recorder(mocker, (1, "", "no checks reported on the 'x' branch"))
        assert await _host().checks.list_checks(REPO, 1) == []

    async def test_no_required_checks_reported_is_empty_only_when_required(self, mocker) -> None:
        _recorder(mocker, (1, "", "no required checks reported on the 'x' branch"))
        assert await _host().checks.list_checks(REPO, 1, required_only=True) == []
        _recorder(mocker, (1, "", "no required checks reported on the 'x' branch"))
        with pytest.raises(CodeHostError):
            await _host().checks.list_checks(REPO, 1)

    async def test_no_checks_text_does_not_hide_a_required_read_failure(self, mocker) -> None:
        _recorder(mocker, (1, "", "no checks reported"))
        with pytest.raises(CodeHostError):
            await _host().checks.list_checks(REPO, 1, required_only=True)

    async def test_failure_raises_with_exit_code_and_auth_is_distinct(self, mocker) -> None:
        _recorder(mocker, (1, "", "boom"), (1, "", "HTTP 401: Bad credentials"), (4, "", "x"))
        with pytest.raises(CodeHostError, match=r"gh pr checks failed \(exit 1\): boom"):
            await _host().checks.list_checks(REPO, 1)
        with pytest.raises(AuthenticationError):
            await _host().checks.list_checks(REPO, 1)
        with pytest.raises(CodeHostError, match="exit 4"):
            await _host().checks.list_checks(REPO, 1)

    @pytest.mark.parametrize("stdout", ["not json", "{}", "[1]", '[{"bucket": "weird"}]', '[{"name": "a"}]'])
    async def test_unusable_output_raises(self, mocker, stdout) -> None:
        _recorder(mocker, (0, stdout, ""))
        with pytest.raises(CodeHostError):
            await _host().checks.list_checks(REPO, 1)

    async def test_list_workflows_argv_and_mapping(self, mocker) -> None:
        payload = {
            "total_count": 3,
            "workflows": [
                {"id": 1, "name": "CI", "state": "active"},
                {"id": 2, "name": "Old", "state": "disabled_manually"},
                {"id": 3, "name": "Lint", "state": "active"},
            ],
        }
        mock_run = _recorder(mocker, _ok(payload))
        got = await _host().checks.list_workflows(REPO)
        assert _argv(mock_run) == ["gh", "api", "repos/myorg/myrepo/actions/workflows?per_page=100"]
        assert [(w.id, w.name) for w in got] == [("1", "CI"), ("3", "Lint")]

    async def test_list_workflows_none_is_empty(self, mocker) -> None:
        _recorder(mocker, _ok({"total_count": 0, "workflows": []}))
        assert await _host().checks.list_workflows(REPO) == []

    async def test_list_workflows_over_the_bound_raises(self, mocker) -> None:
        _recorder(
            mocker,
            _ok({"total_count": 150, "workflows": [{"id": i, "name": "w", "state": "active"} for i in range(100)]}),
        )
        with pytest.raises(CodeHostError, match="more than the declared bound of 100"):
            await _host().checks.list_workflows(REPO)

    async def test_list_workflows_failure_is_not_empty(self, mocker) -> None:
        _recorder(mocker, (1, "", "boom"), (0, "[]", ""), (0, "nope", ""))
        with pytest.raises(CodeHostError, match="exit 1"):
            await _host().checks.list_workflows(REPO)
        with pytest.raises(CodeHostError, match="unexpected response"):
            await _host().checks.list_workflows(REPO)
        with pytest.raises(CodeHostError, match="invalid JSON"):
            await _host().checks.list_workflows(REPO)

    async def test_failed_log_by_job(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "job\tstep\tboom\n", ""))
        log = await _host().checks.failed_log(REPO, _check(details_url=_RUN_URL, job_id="77"))
        assert log == "job\tstep\tboom\n"
        assert _argv(mock_run) == ["gh", "run", "view", "--job", "77", "--repo", "myorg/myrepo", "--log-failed"]

    async def test_failed_log_by_run_when_no_job(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, "log", ""))
        url = "https://examplehost.test/myorg/myrepo/actions/runs/55"
        assert await _host().checks.failed_log(REPO, _check(details_url=url)) == "log"
        assert _argv(mock_run) == ["gh", "run", "view", "55", "--repo", "myorg/myrepo", "--log-failed"]

    @pytest.mark.parametrize("url", [None, "https://examplehost.test/elsewhere"])
    async def test_failed_log_without_job_or_run_raises_and_runs_nothing(self, mocker, url) -> None:
        mock_run = _recorder(mocker)
        with pytest.raises(CodeHostError, match="no job or run"):
            await _host().checks.failed_log(REPO, _check(details_url=url))
        assert mock_run.await_count == 0

    async def test_failed_log_failure_raises(self, mocker) -> None:
        _recorder(mocker, (1, "", "gone"))
        with pytest.raises(CodeHostError, match=r"gh run view failed \(exit 1\): gone"):
            await _host().checks.failed_log(REPO, _check(job_id="1"))

    async def test_get_job_first_failed_step(self, mocker) -> None:
        steps = [
            {"name": "Set up", "conclusion": "success"},
            {"name": "Test", "conclusion": "failure"},
            {"name": "Later", "conclusion": "failure"},
        ]
        mock_run = _recorder(mocker, _ok({"steps": steps}))
        job = await _host().checks.get_job(REPO, "77")
        assert _argv(mock_run) == ["gh", "api", "repos/myorg/myrepo/actions/jobs/77"]
        assert (job.id, job.failed_step) == ("77", "Test")

    @pytest.mark.parametrize("payload", [{"steps": [{"name": "a", "conclusion": "success"}]}, {}, {"steps": None}])
    async def test_get_job_without_a_failed_step(self, mocker, payload) -> None:
        _recorder(mocker, _ok(payload))
        assert (await _host().checks.get_job(REPO, "77")).failed_step is None

    async def test_get_job_failure_and_bad_payload_raise(self, mocker) -> None:
        _recorder(mocker, (1, "", "Not Found"), _ok([]))
        with pytest.raises(CodeHostError, match="exit 1"):
            await _host().checks.get_job(REPO, "1")
        with pytest.raises(CodeHostError, match="unexpected response"):
            await _host().checks.get_job(REPO, "1")

    async def test_token_is_gh_token_only_and_never_in_argv_or_errors(self, mocker) -> None:
        seen: list[tuple[list[str], dict | None]] = []

        def handler(cmd, kw):
            seen.append((cmd, kw.get("env")))
            if cmd[0] != "gh":
                return RunResult(0, SECRET, "")
            return RunResult(1, "", f"denied {SECRET}")

        _stub(mocker, handler)
        host = _create(REGISTRATION.options_model(), ["echo", "x"])
        with pytest.raises(CodeHostError) as info:
            await host.checks.list_checks(REPO, 1)
        assert SECRET not in str(info.value)
        assert SECRET not in repr(info.value)
        gh_calls = [(c, e) for c, e in seen if c[0] == "gh"]
        assert gh_calls
        for cmd, env in gh_calls:
            assert SECRET not in " ".join(cmd)
            assert env is not None
            assert env["GH_TOKEN"] == SECRET
            assert env.get("GITHUB_TOKEN") != SECRET
