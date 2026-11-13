"""Plain reads of ``common.py``.

Classification: ``get_pr_*`` tests converted to the in-memory host (exact ``gh`` arguments live in
``tests/code_host/test_reference*.py``); the git readers are git-only and kept.
"""

from unittest.mock import MagicMock, patch

import pytest

from dotharness.code_host import Capability, CodeHostError, CodeHostTimeoutError, UnsupportedCapabilityError
from dotharness.pr_review.runners.common import (
    get_changed_files,
    get_file_diff,
    get_pr_base_branch,
    get_pr_description,
    get_pr_head_sha,
)
from tests.pr_review.runners.approve_support import REPO, make_host, seed_pr


def _host(**fields):
    host = make_host()
    seed_pr(host, 1, **fields)
    return host


async def test_get_pr_base_branch_returns_branch_name():
    host = _host(base_reference="main")
    assert await get_pr_base_branch(host.code_host(), REPO, 1) == "main"


async def test_get_pr_base_branch_reads_the_change_request_once():
    host = _host(base_reference="feature")
    await get_pr_base_branch(host.code_host(), REPO, 1)
    assert [c.operation for c in host.calls] == ["ChangeRequests.get"]


async def test_get_pr_base_branch_returns_empty_on_failure(caplog):
    host = _host()
    host.fail_next("ChangeRequests.get", CodeHostError("boom"))
    assert await get_pr_base_branch(host.code_host(), REPO, 1) == ""
    assert "Failed to fetch PR #1" in caplog.text


async def test_get_pr_base_branch_returns_empty_for_unknown_change_request():
    assert await get_pr_base_branch(make_host().code_host(), REPO, 99) == ""


async def test_get_pr_base_branch_unsupported_capability_stops():
    host = make_host(capabilities=frozenset(Capability) - {Capability.CHANGE_REQUESTS})
    with pytest.raises(UnsupportedCapabilityError):
        await get_pr_base_branch(host.code_host(), REPO, 1)


async def test_get_pr_head_sha_returns_sha():
    host = _host(head_revision="abc123def456")
    assert await get_pr_head_sha(host.code_host(), REPO, 1) == "abc123def456"


async def test_get_pr_head_sha_returns_empty_on_failure():
    host = _host()
    host.fail_next("ChangeRequests.get", CodeHostTimeoutError("slow", write=False))
    assert await get_pr_head_sha(host.code_host(), REPO, 1) == ""


async def test_get_pr_description_formats_title_and_body():
    host = _host(title="Fix the thing", body="Details here.")
    assert await get_pr_description(host.code_host(), REPO, 1) == (
        "## PR Description\n**Title:** Fix the thing\n\nDetails here."
    )


async def test_get_pr_description_with_empty_body_is_stripped():
    host = _host(title="Fix", body="")
    assert await get_pr_description(host.code_host(), REPO, 1) == "## PR Description\n**Title:** Fix"


async def test_get_pr_description_strips_trailing_whitespace_of_the_body():
    host = _host(title="Fix", body="Text\n\n")
    assert await get_pr_description(host.code_host(), REPO, 1) == "## PR Description\n**Title:** Fix\n\nText"


async def test_get_pr_description_returns_empty_on_failure():
    host = _host()
    host.fail_next("ChangeRequests.get", CodeHostError("boom"))
    assert await get_pr_description(host.code_host(), REPO, 1) == ""


async def test_get_changed_files_parses_output(tmp_path):
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"src/foo.py\nsrc/bar.py\n")
        result = await get_changed_files("main", str(tmp_path), {})
    assert result == ["src/foo.py", "src/bar.py"]


async def test_get_changed_files_uses_local_git(tmp_path):
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        await get_changed_files("main", str(tmp_path), {})
    cmd = mock_run.call_args.args[0]
    assert cmd == ["git", "diff", "--name-only", "origin/main...HEAD"]


async def test_get_changed_files_returns_empty_list_on_failure(tmp_path):
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"error")
        result = await get_changed_files("main", str(tmp_path), {})
    assert result == []


async def test_get_changed_files_skips_blank_lines(tmp_path):
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"a.py\n\nb.py\n")
        result = await get_changed_files("main", str(tmp_path), {})
    assert result == ["a.py", "b.py"]


async def test_get_changed_files_warns_on_sha_mismatch(tmp_path):
    sha_response = MagicMock(returncode=0, stdout=b"aaa111\n")
    diff_response = MagicMock(returncode=0, stdout=b"src/foo.py\n")
    with (
        patch("dotharness.pr_review.runners.common.run_cmd", side_effect=[sha_response, diff_response]),
        patch("dotharness.pr_review.runners.common.logger") as mock_log,
    ):
        result = await get_changed_files("main", str(tmp_path), {}, expected_sha="bbb222")
    assert result == ["src/foo.py"]
    mock_log.warning.assert_called_once()
    assert "aaa111" in str(mock_log.warning.call_args)
    assert "bbb222" in str(mock_log.warning.call_args)


async def test_get_changed_files_no_sha_check_when_expected_empty(tmp_path):
    diff_response = MagicMock(returncode=0, stdout=b"src/foo.py\n")
    with patch("dotharness.pr_review.runners.common.run_cmd", return_value=diff_response) as mock_run:
        await get_changed_files("main", str(tmp_path), {})
    assert mock_run.call_count == 1


async def test_get_file_diff_constructs_correct_command(tmp_path):
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        await get_file_diff("src/foo.py", "main", str(tmp_path), {})
    cmd = mock_run.call_args.args[0]
    assert cmd == ["git", "diff", "origin/main...HEAD", "--", "src/foo.py"]


async def test_get_file_diff_returns_diff_as_string(tmp_path):
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"@@ -1 +1 @@\n+new line\n")
        result = await get_file_diff("src/foo.py", "main", str(tmp_path), {})
    assert result == "@@ -1 +1 @@\n+new line\n"


async def test_get_file_diff_returns_empty_on_failure(tmp_path):
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"")
        result = await get_file_diff("src/foo.py", "main", str(tmp_path), {})
    assert result == ""
