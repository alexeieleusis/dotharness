import json
import subprocess
from unittest.mock import Mock

import pytest

from spec_prism_flow.build import gh_ops
from spec_prism_flow.build.errors import OrchestrationError
from spec_prism_flow.build.gh_ops import GhCommandError, PRHandle, PRNotMergeableError, PRStatus


def _ok(stdout: str = "") -> Mock:
    return Mock(returncode=0, stdout=stdout, stderr="")


def test_pr_create_returns_pr_handle_parsed_from_url(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok(stdout="https://github.com/alexeieleusis/dotharness/pull/42\n"))
    monkeypatch.setattr("subprocess.run", run_mock)

    handle = gh_ops.pr_create(tmp_path, "phase-08-x", "My Title", "My Body")

    assert handle == PRHandle(number=42, url="https://github.com/alexeieleusis/dotharness/pull/42")


def test_pr_create_passes_title_and_body_as_argv_elements_not_shell_string(tmp_path, monkeypatch):
    run_mock = Mock(return_value=_ok(stdout="https://github.com/alexeieleusis/dotharness/pull/1\n"))
    monkeypatch.setattr("subprocess.run", run_mock)

    title = "`rm -rf /` $(whoami) title"
    body = "line1\nline2 with 'quotes' and \"double quotes\" and $(cmd)"
    gh_ops.pr_create(tmp_path, "phase-08-x", title, body)

    call_kwargs = run_mock.call_args
    argv = call_kwargs.args[0]
    assert argv == ["gh", "pr", "create", "--head", "phase-08-x", "--title", title, "--body", body]
    assert call_kwargs.kwargs["cwd"] == tmp_path


def test_pr_create_raises_gh_command_error_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("subprocess.run", Mock(return_value=Mock(returncode=1, stdout="", stderr="not a repo")))

    with pytest.raises(GhCommandError) as exc_info:
        gh_ops.pr_create(tmp_path, "phase-08-x", "t", "b")

    assert exc_info.value.returncode == 1
    assert exc_info.value.stderr == "not a repo"


def test_pr_view_parses_status_json(monkeypatch):
    run_mock = Mock(
        return_value=_ok(stdout=json.dumps({"state": "OPEN", "mergeable": "MERGEABLE", "reviewDecision": "APPROVED"}))
    )
    monkeypatch.setattr("subprocess.run", run_mock)

    status = gh_ops.pr_view(42)

    assert status == PRStatus(state="OPEN", mergeable="MERGEABLE", review_decision="APPROVED")
    argv = run_mock.call_args.args[0]
    assert argv == ["gh", "pr", "view", "42", "--json", "state,mergeable,reviewDecision"]
    assert run_mock.call_args.kwargs["cwd"] is None


def test_pr_view_defaults_review_decision_to_empty_string_when_absent(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run", Mock(return_value=_ok(stdout=json.dumps({"state": "OPEN", "mergeable": "UNKNOWN"})))
    )

    status = gh_ops.pr_view(42)

    assert status.review_decision == ""


def test_pr_view_raises_gh_command_error_on_failure(monkeypatch):
    monkeypatch.setattr("subprocess.run", Mock(return_value=Mock(returncode=1, stdout="", stderr="no such pr")))

    with pytest.raises(GhCommandError):
        gh_ops.pr_view(999)


def _threads_response(nodes: list[dict], has_next_page: bool, end_cursor: str | None) -> str:
    return json.dumps({
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
                    }
                }
            }
        }
    })


def test_unresolved_thread_count_single_page(monkeypatch):
    responses = [
        _ok(stdout=json.dumps({"nameWithOwner": "alexeieleusis/dotharness"})),
        _ok(
            stdout=_threads_response(
                [{"isResolved": True}, {"isResolved": False}, {"isResolved": False}],
                has_next_page=False,
                end_cursor=None,
            )
        ),
    ]
    monkeypatch.setattr("subprocess.run", Mock(side_effect=responses))

    assert gh_ops.unresolved_thread_count(42) == 2


def test_unresolved_thread_count_paginates_across_multiple_pages(monkeypatch):
    responses = [
        _ok(stdout=json.dumps({"nameWithOwner": "alexeieleusis/dotharness"})),
        _ok(
            stdout=_threads_response(
                [{"isResolved": False}, {"isResolved": False}], has_next_page=True, end_cursor="cursor-1"
            )
        ),
        _ok(
            stdout=_threads_response(
                [{"isResolved": True}, {"isResolved": False}], has_next_page=True, end_cursor="cursor-2"
            )
        ),
        _ok(stdout=_threads_response([{"isResolved": False}], has_next_page=False, end_cursor=None)),
    ]
    run_mock = Mock(side_effect=responses)
    monkeypatch.setattr("subprocess.run", run_mock)

    total = gh_ops.unresolved_thread_count(42)

    assert total == 4
    assert run_mock.call_count == 4
    graphql_calls = run_mock.call_args_list[1:]
    assert "cursor=cursor-1" not in " ".join(graphql_calls[0].args[0])
    assert "cursor=cursor-1" in " ".join(graphql_calls[1].args[0])
    assert "cursor=cursor-2" in " ".join(graphql_calls[2].args[0])


def test_unresolved_thread_count_raises_gh_command_error_on_graphql_failure(monkeypatch):
    responses = [
        _ok(stdout=json.dumps({"nameWithOwner": "alexeieleusis/dotharness"})),
        Mock(returncode=1, stdout="", stderr="bad query"),
    ]
    monkeypatch.setattr("subprocess.run", Mock(side_effect=responses))

    with pytest.raises(GhCommandError):
        gh_ops.unresolved_thread_count(42)


def test_pr_merge_succeeds_on_zero_exit(monkeypatch):
    run_mock = Mock(return_value=_ok())
    monkeypatch.setattr("subprocess.run", run_mock)

    gh_ops.pr_merge(42)

    assert run_mock.call_args.args[0] == ["gh", "pr", "merge", "42", "--squash"]
    assert run_mock.call_args.kwargs["cwd"] is None


def test_pr_merge_raises_pr_not_mergeable_error_with_next_command_on_failure(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run", Mock(return_value=Mock(returncode=1, stdout="", stderr="not mergeable: conflicts"))
    )

    with pytest.raises(PRNotMergeableError) as exc_info:
        gh_ops.pr_merge(42)

    err = exc_info.value
    assert err.returncode == 1
    assert err.stderr == "not mergeable: conflicts"
    assert err.next_command == "gh pr view 42"
    assert "not mergeable: conflicts" in str(err)


def test_pr_not_mergeable_error_is_also_an_orchestration_error(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run", Mock(return_value=Mock(returncode=1, stdout="", stderr="not mergeable: conflicts"))
    )

    with pytest.raises(PRNotMergeableError) as exc_info:
        gh_ops.pr_merge(42)

    err = exc_info.value
    assert isinstance(err, OrchestrationError)
    assert err.exit_code == 15
    assert err.next_command == "gh pr view 42"
    err.with_context(phase_number=11, phase_name="merge")
    assert err.phase_number == 11
    assert err.phase_name == "merge"


def test_pr_merge_does_not_retry_on_failure(monkeypatch):
    run_mock = Mock(return_value=Mock(returncode=1, stdout="", stderr="conflict"))
    monkeypatch.setattr("subprocess.run", run_mock)

    with pytest.raises(PRNotMergeableError):
        gh_ops.pr_merge(42)

    assert run_mock.call_count == 1


def test_pr_merge_raises_pr_not_mergeable_error_with_next_command_on_timeout(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        Mock(
            side_effect=subprocess.TimeoutExpired(
                cmd=["gh", "pr", "merge", "42", "--squash"], timeout=gh_ops._GH_TIMEOUT_SECONDS
            )
        ),
    )

    with pytest.raises(PRNotMergeableError) as exc_info:
        gh_ops.pr_merge(42)

    assert exc_info.value.next_command == "gh pr view 42"


def test_run_raises_gh_command_error_instead_of_hanging_on_timeout(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        Mock(side_effect=subprocess.TimeoutExpired(cmd=["gh", "pr", "view", "1"], timeout=gh_ops._GH_TIMEOUT_SECONDS)),
    )

    with pytest.raises(GhCommandError) as exc_info:
        gh_ops.pr_view(1)

    assert str(gh_ops._GH_TIMEOUT_SECONDS) in exc_info.value.stderr
