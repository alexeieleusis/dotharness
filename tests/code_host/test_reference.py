"""Tests for the reference code host (gh). A stubbed subprocess runner is used: no network, no real gh."""

import json
import logging
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from dotharness.code_host import HostSelection, create_code_host
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
    _MAX_OVERFLOW_BODY_LINES,
    _MAX_REVIEW_BODY_CHARS,
    _POST_TIMEOUT_SECONDS,
    DECLARATION,
    REGISTRATION,
    _create,
    _parse_remote_url,
    _post_json,
)
from dotharness.subprocess_runner import RunResult
from dotharness.vibe_heal.review.models import FileReview, ReviewIssue, ReviewResult

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
    preview = await discussion.preview_review(report)
    return await discussion.post_review(
        REPO,
        number,
        body=preview.body,
        inline_comments=preview.inline_comments,
        report=report if with_report else None,
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
            await _host().session.credential()
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

    async def test_read_timeout_raises_plain_code_host_error(self, mocker) -> None:
        """D-8: a read timeout is a CodeHostError, not CodeHostTimeoutError."""

        async def _run(cmd, **kw):
            if "--version" in cmd:
                return GH_OK
            raise TimeoutError

        mocker.patch("dotharness.code_host.reference.run", side_effect=_run)
        with pytest.raises(CodeHostError, match=r"`gh pr` timed out after 30s") as info:
            await _host().change_requests.for_current_branch(REPO)
        assert not isinstance(info.value, CodeHostTimeoutError)

    async def test_unimplemented_operation_raises_unsupported(self) -> None:
        with pytest.raises(UnsupportedCapabilityError) as info:
            await _host().change_requests.get(REPO, 1)
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

    async def test_does_not_rebuild_review_text(self, sample_report, mocker) -> None:
        """post_review posts the given body, not text it builds itself from the report."""
        mock_run = _flow(mocker, (0, "{}", ""))
        await _host().discussion.post_review(REPO, 1, body="given body", inline_comments=[], report=sample_report)
        assert _posts(mock_run)[0]["body"] == "given body"
        assert _posts(mock_run)[0]["comments"] == []


class TestFallback:
    async def test_fallback_comment_for_rejected_lines(self, sample_report, mocker) -> None:
        mock_run = _flow(mocker, (1, '{"message": "Unprocessable Entity"}', ""), (0, "{}", ""))
        result = await _post(sample_report)

        assert result.fell_back is True
        assert result.reason == '{"message": "Unprocessable Entity"}'
        posts = _posts(mock_run)
        assert len(posts) == 2
        assert posts[1]["event"] == "COMMENT"
        assert posts[1]["comments"] == []
        assert "python:S1481" in posts[1]["body"]
        assert "python:S1192" in posts[1]["body"]

    async def test_fallback_without_report_posts_body(self, sample_report, mocker) -> None:
        mock_run = _flow(mocker, (1, "", "line outside diff"), (0, "{}", ""))
        result = await _post(sample_report, with_report=False)

        assert result.fell_back is True
        assert result.reason == "line outside diff"
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
        mock_run = _flow(mocker, (1, '{"message": "Unprocessable Entity"}', ""), (0, "{}", ""))
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
        mock_run = _flow(mocker, (1, "", "bad anchor"), (0, "{}", ""))
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
        mock_run = _flow(mocker, (1, "", "bad"), (0, "{}", ""))
        await _post(report)
        body = _posts(mock_run)[1]["body"]
        assert "**50.0%**" in body
        assert "| 5 |" in body
        assert "| 10 |" in body
        assert "Coverage on changed lines" in body

    async def test_raises_when_fallback_also_fails(self, sample_report, mocker) -> None:
        mock_run = _flow(
            mocker,
            (1, '{"message": "Server error"}', "internal error"),
            (1, '{"message": "Server error"}', "internal error"),
        )
        with pytest.raises(CodeHostError, match=r"^Review posting failed: internal error") as info:
            await _post(sample_report)
        assert info.value.__cause__ is not None
        assert len(_posts(mock_run)) == 2  # nothing else is posted

    async def test_fallback_timeout_is_wrapped(self, sample_report, mocker) -> None:
        results = iter([RunResult(1, "", "bad"), RunResult(124, "", "", timed_out=True)])
        _stub(mocker, lambda cmd, kw: GH_OK if "--version" in cmd else next(results))
        with pytest.raises(CodeHostError, match="Review posting failed"):
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

    async def test_not_found_propagates_without_fallback(self, sample_report, mocker) -> None:
        mock_run = _stub(
            mocker,
            lambda cmd, kw: GH_OK if "--version" in cmd else RunResult(127, "", "no gh", not_found=True),
        )
        with pytest.raises(FileNotFoundError):
            await _post(sample_report)
        assert len(_posts(mock_run)) == 1

    async def test_fallback_logs_warning_with_inline_error(self, sample_report, mocker, caplog) -> None:
        _flow(mocker, (1, "", "validation error: line outside diff"), (0, "{}", ""))
        with caplog.at_level(logging.WARNING, logger="dotharness.code_host.reference"):
            await _post(sample_report)
        assert any("Inline review failed" in r.message for r in caplog.records)
        assert any("falling back" in r.message for r in caplog.records)
        assert any("line outside diff" in r.message for r in caplog.records)

    async def test_host_does_not_print_warning(self, sample_report, mocker, capsys) -> None:
        _flow(mocker, (1, "", "bad"), (0, "{}", ""))
        await _post(sample_report)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


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
# Discussion.preview_review
# --------------------------------------------------------------------------------------------------------------------


class TestPreviewReview:
    @staticmethod
    def _n_issues(n: int) -> ReviewResult:
        return ReviewResult(
            project_key="p",
            branch="feat",
            base_branch="origin/main",
            files=[
                FileReview(
                    file_path=f"src/file_{i}.py",
                    issues=[ReviewIssue(rule="python:S1", message=f"Issue {i}", line=i + 1, on_changed_line=True)],
                )
                for i in range(n)
            ],
        )

    async def test_makes_no_host_call(self, sample_report, mocker) -> None:
        mock_run = _stub(mocker, lambda cmd, kw: GH_OK)
        preview = await _host().discussion.preview_review(sample_report)
        assert mock_run.await_count == 0
        assert len(preview.inline_comments) == 2

    async def test_inline_comments_carry_right_side_and_line(self, sample_report) -> None:
        preview = await _host().discussion.preview_review(sample_report)
        first = preview.inline_comments[0]
        assert first.anchor == Anchor("src/example.py", 10, "RIGHT")
        assert first.body.startswith("**python:S1481**")

    async def test_oversized_bodies_are_truncated(self, mocker) -> None:
        report = ReviewResult(
            project_key="p",
            branch="b",
            base_branch="main",
            files=[
                FileReview(
                    file_path="a.py",
                    issues=[
                        ReviewIssue(rule="python:S1", message="x" * 500, line=i, on_changed_line=False)
                        for i in range(1, 400)
                    ],
                )
            ],
        )
        preview = await _host().discussion.preview_review(report)
        assert len(preview.body) <= _MAX_REVIEW_BODY_CHARS
        assert "review body truncated" in preview.body

        mock_run = _flow(mocker, (1, "", "bad"), (0, "{}", ""))
        await _post(report)
        assert len(_posts(mock_run)[1]["body"]) <= _MAX_REVIEW_BODY_CHARS
        assert "review body truncated" in _posts(mock_run)[1]["body"]

    async def test_nearby_issues_section(self) -> None:
        report = ReviewResult(
            project_key="p",
            branch="b",
            base_branch="main",
            files=[
                FileReview(
                    file_path="a.py",
                    issues=[ReviewIssue(rule="python:S1", message="near", line=7, on_changed_line=False)],
                )
            ],
        )
        preview = await _host().discussion.preview_review(report)
        assert "**Issues near changed lines**" in preview.body
        assert "- **python:S1** (a.py:7) near" in preview.body
        assert "1 near changed lines" in preview.body
        assert preview.inline_comments == []

    async def test_duplication_comments_use_anchor_line(self) -> None:
        from dotharness.vibe_heal.review.models import DuplicationLocation, ResolvedDuplication, ReviewDuplication

        loc = DuplicationLocation(file_path="other.py", from_line=1, to_line=5)
        report = ReviewResult(
            project_key="p",
            branch="b",
            base_branch="origin/main",
            files=[
                FileReview(
                    file_path="a.py",
                    duplications=[
                        ReviewDuplication(from_line=3, to_line=9, other_locations=[loc], anchor_line=4),
                        ReviewDuplication(from_line=20, to_line=29, other_locations=[loc]),
                    ],
                    resolved_duplications=[
                        ResolvedDuplication(main_from_line=1, main_to_line=4, anchor_new_line=2, other_locations=[loc])
                    ],
                )
            ],
        )
        preview = await _host().discussion.preview_review(report)
        assert [c.anchor.line for c in preview.inline_comments] == [4, 20, 2]
        assert "**Duplication detected** (lines 3-9)" in preview.inline_comments[0].body
        assert "`other.py` lines 1-5" in preview.inline_comments[0].body
        assert "`origin/main`" in preview.inline_comments[2].body

    async def test_comments_capped_at_max(self) -> None:
        preview = await _host().discussion.preview_review(self._n_issues(_MAX_INLINE_COMMENTS + 10))
        assert len(preview.inline_comments) == _MAX_INLINE_COMMENTS

    async def test_overflow_appears_in_body(self) -> None:
        preview = await _host().discussion.preview_review(self._n_issues(_MAX_INLINE_COMMENTS + 5))
        assert "5 finding(s) over the" in preview.body
        assert "capped" in preview.body
        assert "src/file_50.py" in preview.body

    async def test_overflow_lines_are_limited(self) -> None:
        preview = await _host().discussion.preview_review(
            self._n_issues(_MAX_INLINE_COMMENTS + _MAX_OVERFLOW_BODY_LINES + 7)
        )
        assert "_...and 7 more not shown._" in preview.body

    async def test_summary_counts_overflow(self) -> None:
        total = _MAX_INLINE_COMMENTS + 3
        preview = await _host().discussion.preview_review(self._n_issues(total))
        assert f"{total} finding(s)" in preview.body

    async def test_under_cap_posts_all_inline(self) -> None:
        preview = await _host().discussion.preview_review(self._n_issues(5))
        assert len(preview.inline_comments) == 5
        assert "capped" not in preview.body


class TestPreviewCoverage:
    @staticmethod
    def _report(**file_kwargs) -> ReviewResult:
        return ReviewResult(
            project_key="p",
            branch="feature/x",
            base_branch="origin/main",
            files=[FileReview(file_path="src/f.py", **file_kwargs)],
        )

    async def test_coverage_included_in_body_when_present(self) -> None:
        preview = await _host().discussion.preview_review(
            self._report(coverage_pct=72.0, covered_lines=18, instrumented_changed_lines=25)
        )
        assert "**72.0%**" in preview.body  # below 80, so bold
        assert "| 18 |" in preview.body
        assert "| 25 |" in preview.body
        assert "Coverage on changed lines" in preview.body

    async def test_coverage_not_bold_at_or_above_80(self) -> None:
        preview = await _host().discussion.preview_review(
            self._report(coverage_pct=90.0, covered_lines=9, instrumented_changed_lines=10)
        )
        assert "| 90.0% |" in preview.body

    async def test_coverage_absent_from_body_when_none(self) -> None:
        preview = await _host().discussion.preview_review(self._report())
        assert "Coverage on changed lines" not in preview.body


class TestPreviewProjectLabelAndSha:
    @staticmethod
    def _report(project_label: str = "", head_sha: str = "", files: list[FileReview] | None = None) -> ReviewResult:
        return ReviewResult(
            project_key="p",
            project_label=project_label,
            branch="feature/x",
            head_sha=head_sha,
            base_branch="origin/main",
            files=files or [],
        )

    @staticmethod
    def _one_issue() -> list[FileReview]:
        return [
            FileReview(
                file_path="src/f.py",
                issues=[ReviewIssue(rule="python:S1481", message="Remove unused variable", line=10)],
            )
        ]

    async def test_label_included_in_no_findings_summary(self) -> None:
        preview = await _host().discussion.preview_review(self._report(project_label="services/api"))
        assert "SonarQube [services/api]: no findings on or near changed lines." in preview.body

    async def test_label_included_in_findings_summary(self) -> None:
        preview = await _host().discussion.preview_review(
            self._report(project_label="services/api", files=self._one_issue())
        )
        assert "SonarQube [services/api]: 1 finding(s)" in preview.body

    async def test_no_brackets_when_label_absent(self) -> None:
        preview = await _host().discussion.preview_review(self._report())
        assert "SonarQube: no findings on or near changed lines." in preview.body
        assert "[" not in preview.body.splitlines()[0]

    async def test_commit_included_in_no_findings_summary(self) -> None:
        preview = await _host().discussion.preview_review(self._report(head_sha="abcdef1234567890"))
        assert "SonarQube: no findings on or near changed lines. (commit abcdef1)" in preview.body

    async def test_commit_included_in_findings_summary(self) -> None:
        preview = await _host().discussion.preview_review(
            self._report(head_sha="abcdef1234567890", files=self._one_issue())
        )
        assert preview.body.splitlines()[0].endswith("(commit abcdef1)")

    async def test_no_commit_suffix_when_head_sha_absent(self) -> None:
        preview = await _host().discussion.preview_review(self._report())
        assert "SonarQube: no findings on or near changed lines." in preview.body
        assert "(commit" not in preview.body

    async def test_commit_included_in_fallback_body(self, mocker) -> None:
        mock_run = _flow(mocker, (1, "", "bad"), (0, "{}", ""))
        await _post(self._report(head_sha="abcdef1234567890", files=self._one_issue()))
        assert "(commit abcdef1)" in _posts(mock_run)[1]["body"]


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
        assert _MAX_OVERFLOW_BODY_LINES == 100
        assert _POST_TIMEOUT_SECONDS == 60

    def test_anchor_rules(self) -> None:
        anchors = DECLARATION.anchors
        assert anchors.inline_supported is True
        assert anchors.sides == frozenset({"RIGHT"})
        assert anchors.requires_line_in_diff is True
        assert anchors.supports_ranges is False
        assert anchors.fallback == "top_level_comment"

    def test_checks_and_work_items_are_absent(self) -> None:
        assert Capability.CHECKS not in DECLARATION.capabilities
        assert Capability.WORK_ITEMS not in DECLARATION.capabilities
        assert Capability.DISCUSSION in DECLARATION.capabilities


class TestRegistration:
    def test_registration_values(self) -> None:
        assert REGISTRATION.interface_version == 1
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
            ("work_items", lambda o: o.list_open(REPO), "WorkItems"),
            ("work_items", lambda o: o.remove_label(REPO, 1, "x"), "WorkItems"),
            ("work_items", lambda o: o.closing_change_requests(REPO, 1), "WorkItems"),
            ("work_items", lambda o: o.open_change_request_bodies(REPO), "WorkItems"),
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
            ("repository", lambda o: o.default_branch(REPO)),
            ("repository", lambda o: o.display_name(REPO)),
            ("change_requests", lambda o: o.state(REPO, 1)),
            ("change_requests", lambda o: o.find_by_branch(REPO, "b")),
            ("change_requests", lambda o: o.list(REPO)),
            ("change_requests", lambda o: o.find_by_commit(REPO, "r")),
            ("change_requests", lambda o: o.commits(REPO, 1)),
            ("change_requests", lambda o: o.reviews(REPO, 1)),
            ("change_requests", lambda o: o.create_draft(REPO, head="h", base="b", title="t", body="b")),
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
