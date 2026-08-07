from unittest.mock import MagicMock, patch

from harness.runners.common import (
    get_changed_files,
    get_file_diff,
    get_pr_base_branch,
    get_pr_head_sha,
)


def test_get_pr_base_branch_returns_branch_name():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"main\n")
        result = get_pr_base_branch(1, "acme/repo", {})
    assert result == "main"


def test_get_pr_base_branch_uses_correct_command():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"feature\n")
        get_pr_base_branch(42, "acme/repo", {"TOKEN": "x"})
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


def test_get_pr_base_branch_returns_empty_on_failure():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"")
        result = get_pr_base_branch(1, "acme/repo", {})
    assert result == ""


def test_get_pr_head_sha_returns_sha():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"abc123def456\n")
        result = get_pr_head_sha(1, "acme/repo", {})
    assert result == "abc123def456"


def test_get_pr_head_sha_uses_correct_command():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"sha\n")
        get_pr_head_sha(7, "owner/repo", {})
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


def test_get_pr_head_sha_returns_empty_on_failure():
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"")
        result = get_pr_head_sha(1, "acme/repo", {})
    assert result == ""


def test_get_changed_files_parses_output(tmp_path):
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"src/foo.py\nsrc/bar.py\n")
        result = get_changed_files("main", str(tmp_path), {})
    assert result == ["src/foo.py", "src/bar.py"]


def test_get_changed_files_uses_local_git(tmp_path):
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        get_changed_files("main", str(tmp_path), {})
    cmd = mock_run.call_args.args[0]
    assert cmd == ["git", "diff", "--name-only", "origin/main...HEAD"]


def test_get_changed_files_returns_empty_list_on_failure(tmp_path):
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"", stderr=b"error")
        result = get_changed_files("main", str(tmp_path), {})
    assert result == []


def test_get_changed_files_skips_blank_lines(tmp_path):
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"a.py\n\nb.py\n")
        result = get_changed_files("main", str(tmp_path), {})
    assert result == ["a.py", "b.py"]


def test_get_changed_files_warns_on_sha_mismatch(tmp_path):
    sha_response = MagicMock(returncode=0, stdout=b"aaa111\n")
    diff_response = MagicMock(returncode=0, stdout=b"src/foo.py\n")
    with (
        patch("harness.runners.common.run_cmd", side_effect=[sha_response, diff_response]),
        patch("harness.runners.common.logger") as mock_log,
    ):
        result = get_changed_files("main", str(tmp_path), {}, expected_sha="bbb222")
    assert result == ["src/foo.py"]
    mock_log.warning.assert_called_once()
    assert "aaa111" in str(mock_log.warning.call_args)
    assert "bbb222" in str(mock_log.warning.call_args)


def test_get_changed_files_no_sha_check_when_expected_empty(tmp_path):
    diff_response = MagicMock(returncode=0, stdout=b"src/foo.py\n")
    with patch("harness.runners.common.run_cmd", return_value=diff_response) as mock_run:
        get_changed_files("main", str(tmp_path), {})
    assert mock_run.call_count == 1


def test_get_file_diff_constructs_correct_command(tmp_path):
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        get_file_diff("src/foo.py", "main", str(tmp_path), {})
    cmd = mock_run.call_args.args[0]
    assert cmd == ["git", "diff", "origin/main...HEAD", "--", "src/foo.py"]


def test_get_file_diff_returns_diff_as_string(tmp_path):
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"@@ -1 +1 @@\n+new line\n")
        result = get_file_diff("src/foo.py", "main", str(tmp_path), {})
    assert result == "@@ -1 +1 @@\n+new line\n"


def test_get_file_diff_returns_empty_on_failure(tmp_path):
    with patch("harness.runners.common.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"")
        result = get_file_diff("src/foo.py", "main", str(tmp_path), {})
    assert result == ""
