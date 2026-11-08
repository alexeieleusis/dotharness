from unittest.mock import MagicMock, patch

from dotharness.pr_review.runners.common import (
    get_changed_files,
    get_file_diff,
    get_pr_base_branch,
    get_pr_head_sha,
)


async def test_get_pr_base_branch_returns_branch_name():
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"main\n")
        result = await get_pr_base_branch(1, "acme/repo", {})
    assert result == "main"


async def test_get_pr_base_branch_uses_correct_command():
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"feature\n")
        await get_pr_base_branch(42, "acme/repo", {"TOKEN": "x"})
    cmd = mock_run.call_args.args[0]
    assert cmd == [
        "gh",
        "pr",
        "view",
        "42",
        "--repo",
        "acme/repo",
        "--json",
        "baseRefName",
        "--jq",
        ".baseRefName",
    ]


async def test_get_pr_base_branch_returns_empty_on_failure():
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"")
        result = await get_pr_base_branch(1, "acme/repo", {})
    assert result == ""


async def test_get_pr_head_sha_returns_sha():
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"abc123def456\n")
        result = await get_pr_head_sha(1, "acme/repo", {})
    assert result == "abc123def456"


async def test_get_pr_head_sha_uses_correct_command():
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"sha\n")
        await get_pr_head_sha(7, "owner/repo", {})
    cmd = mock_run.call_args.args[0]
    assert cmd == [
        "gh",
        "pr",
        "view",
        "7",
        "--repo",
        "owner/repo",
        "--json",
        "headRefOid",
        "--jq",
        ".headRefOid",
    ]


async def test_get_pr_head_sha_returns_empty_on_failure():
    with patch("dotharness.pr_review.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"")
        result = await get_pr_head_sha(1, "acme/repo", {})
    assert result == ""


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
