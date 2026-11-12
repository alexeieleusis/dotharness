"""Tests for the reference code host (gh). A stubbed subprocess runner is used: no network, no real gh."""

import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dotharness.code_host import CODE_HOST_INTERFACE_VERSION, HostSelection, create_code_host
from dotharness.code_host.declarations import Anchor, Capability
from dotharness.code_host.errors import (
    AuthenticationError,
    CodeHostError,
    CodeHostTimeoutError,
    NoChangeRequestError,
    UnsupportedCapabilityError,
)
from dotharness.code_host.models import ChangeRequestState, InlineComment, RepositoryRef
from dotharness.code_host.reference import (
    _MAX_INLINE_COMMENTS,
    _MAX_REVIEW_BODY_CHARS,
    _POST_TIMEOUT_SECONDS,
    DECLARATION,
    REGISTRATION,
    TokenCommandError,
    _create,
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

    async def test_unimplemented_session_operations_raise_unsupported(self) -> None:
        with pytest.raises(UnsupportedCapabilityError) as info:
            await _host().session.current_user()
        assert info.value.capability == "Session"


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

    async def test_unimplemented_operation_raises_unsupported(self) -> None:
        with pytest.raises(UnsupportedCapabilityError) as info:
            await _host().change_requests.list(REPO)
        assert info.value.capability == "ChangeRequests"


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
# Declaration, registration, absent capabilities
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

    def test_checks_absent_and_work_items_declared(self) -> None:
        assert Capability.CHECKS not in DECLARATION.capabilities
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


class TestAbsentCapabilities:
    @pytest.mark.parametrize(
        ("attr", "call", "capability"),
        [
            ("checks", lambda o: o.list_checks(REPO, 1), "Checks"),
            ("checks", lambda o: o.list_workflows(REPO), "Checks"),
            ("checks", lambda o: o.failed_log(REPO, None), "Checks"),
            ("checks", lambda o: o.get_job(REPO, "1"), "Checks"),
            ("work_items", lambda o: o.get(REPO, 1), "WorkItems"),
            ("work_items", lambda o: o.linked_work_items(REPO, 1), "WorkItems"),
        ],
    )
    async def test_raises_unsupported_and_runs_no_gh(self, mocker, attr, call, capability) -> None:
        mock_run = mocker.patch("dotharness.code_host.reference.run", new_callable=AsyncMock)
        with pytest.raises(UnsupportedCapabilityError) as info:
            await call(getattr(_host(), attr))
        assert info.value.capability == capability
        assert mock_run.await_count == 0

    @pytest.mark.parametrize(
        ("attr", "call"),
        [
            ("session", lambda o: o.current_user()),
            ("session", lambda o: o.agent_environment_names()),
            ("session", lambda o: o.agent_environment()),
            ("repository", lambda o: o.resolve("x")),
            ("repository", lambda o: o.display_name(REPO)),
            ("change_requests", lambda o: o.list(REPO)),
            ("change_requests", lambda o: o.find_by_commit(REPO, "r")),
            ("change_requests", lambda o: o.commits(REPO, 1)),
            ("change_requests", lambda o: o.reviews(REPO, 1)),
            ("change_requests", lambda o: o.squash_merge(REPO, 1, expected_head="h")),
            ("change_requests", lambda o: o.request_reviewer(REPO, 1, "u")),
            ("change_requests", lambda o: o.approve(REPO, 1, head_revision="h")),
            ("change_requests", lambda o: o.viewed_files(REPO, 1)),
            ("change_requests", lambda o: o.mark_viewed(REPO, 1, "p")),
            ("discussion", lambda o: o.comments(REPO, 1)),
            ("discussion", lambda o: o.review_threads(REPO, 1)),
            ("discussion", lambda o: o.post_comment(REPO, 1, "b")),
            ("discussion", lambda o: o.reply(REPO, 1, "t", "b")),
        ],
    )
    async def test_out_of_flow_operations_are_unsupported(self, attr, call) -> None:
        with pytest.raises(UnsupportedCapabilityError):
            await call(getattr(_host(), attr))


# --------------------------------------------------------------------------------------------------------------------
# Phase 2 capabilities: exact gh argv, directory, environment, errors
# --------------------------------------------------------------------------------------------------------------------

SECRET = "s3cr3t-value"  # noqa: S105


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
        assert _argv(mock_run) == ["gh", "pr", "view", "6", "--json", "number,state,headRefOid"]
        assert (cr.number, cr.state, cr.head_revision, cr.repository) == (6, ChangeRequestState.CLOSED, "sha", REPO)

    async def test_state(self, mocker) -> None:
        mock_run = _recorder(mocker, (0, '{"state": "MERGED"}', ""))
        assert await _host().change_requests.state(REPO, 6) is ChangeRequestState.MERGED
        assert _argv(mock_run) == ["gh", "pr", "view", "6", "--json", "state"]

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
        assert _argv(mock_run) == ["gh", "repo", "view", "--json", "defaultBranchRef"]

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

    async def test_no_repo_flag(self, mocker) -> None:
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

    async def test_timeout(self, mocker) -> None:
        _recorder(mocker, RunResult(124, "", "timed out after 30s", timed_out=True))
        with pytest.raises(TokenCommandError) as info:
            await _create(REGISTRATION.options_model(), "mycmd", None).session.credential()
        assert (info.value.exit_code, info.value.detail) == (None, "TimeoutExpired")
        assert str(info.value) == "token command failed to run (TimeoutExpired)"

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
