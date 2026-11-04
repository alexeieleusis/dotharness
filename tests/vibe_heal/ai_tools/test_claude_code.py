"""Tests for Claude Code AI tool."""

import asyncio
import tempfile
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from dotharness.vibe_heal.ai_tools import ClaudeCodeTool
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue


@pytest.fixture
def sample_issue() -> SonarQubeIssue:
    """Create a sample issue for testing."""
    return SonarQubeIssue(
        key="test-123",
        rule="python:S1481",
        severity="MAJOR",
        message="Remove unused variable",
        component="project:src/test.py",
        line=10,
        status="OPEN",
        type="CODE_SMELL",
    )


class TestClaudeCodeTool:
    """Tests for ClaudeCodeTool."""

    def test_is_available_when_claude_exists(self, mocker: MockerFixture) -> None:
        """Test is_available returns True when claude is installed."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/claude")

        tool = ClaudeCodeTool()
        assert tool.is_available() is True

    def test_is_available_when_claude_missing(self, mocker: MockerFixture) -> None:
        """Test is_available returns False when claude is not installed."""
        mocker.patch("shutil.which", return_value=None)

        tool = ClaudeCodeTool()
        assert tool.is_available() is False

    @pytest.mark.asyncio
    async def test_fix_issue_when_tool_not_available(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test fix_issue returns error when tool not available."""
        mocker.patch("shutil.which", return_value=None)

        tool = ClaudeCodeTool()
        result = await tool.fix_issue(sample_issue, "src/test.py")

        assert result.success is False
        assert result.failed is True
        assert "not found" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_fix_issue_when_file_not_found(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test fix_issue returns error when file doesn't exist."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/claude")

        tool = ClaudeCodeTool()
        nonexistent_file = tmp_path / "nonexistent.py"
        result = await tool.fix_issue(sample_issue, str(nonexistent_file))

        assert result.success is False
        assert "File not found" in result.error_message

    @pytest.mark.asyncio
    async def test_successful_fix(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test successful fix execution."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/claude")

        # Create a temporary file
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    unused = 1\n    pass\n")

        # Mock subprocess
        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (
            b'{"result": "success"}',
            b"",
        )
        mock_process.returncode = 0

        mocker.patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        tool = ClaudeCodeTool()
        result = await tool.fix_issue(sample_issue, str(test_file))

        assert result.success is True
        assert result.failed is False
        assert str(test_file) in result.files_modified
        assert result.error_message is None

    @pytest.mark.asyncio
    async def test_fix_with_command_failure(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test handling of command execution failure."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/claude")

        # Create a temporary file
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        # Mock subprocess with failure
        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (
            b"",
            b"Error: Command failed",
        )
        mock_process.returncode = 1

        mocker.patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        tool = ClaudeCodeTool()
        result = await tool.fix_issue(sample_issue, str(test_file))

        assert result.success is False
        assert "failed" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_timeout_handling(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test handling of timeout."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/claude")

        # Create a temporary file
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        # Mock subprocess that times out
        mock_process = mocker.AsyncMock()
        mock_process.communicate.side_effect = asyncio.TimeoutError()
        mock_process.kill = mocker.Mock()
        mock_process.wait = mocker.AsyncMock()

        mocker.patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        tool = ClaudeCodeTool(timeout=1)
        result = await tool.fix_issue(sample_issue, str(test_file))

        assert result.success is False
        assert "timed out" in result.error_message.lower()
        mock_process.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_handling(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test handling of unexpected exceptions."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/claude")

        # Create a temporary file
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        # Mock subprocess that raises exception
        mocker.patch(
            "asyncio.create_subprocess_exec",
            side_effect=RuntimeError("Unexpected error"),
        )

        tool = ClaudeCodeTool()
        result = await tool.fix_issue(sample_issue, str(test_file))

        assert result.success is False
        assert "Error invoking Claude" in result.error_message

    @pytest.mark.asyncio
    async def test_command_construction(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test that command is constructed correctly."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/claude")

        # Create a temporary file
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        # Mock subprocess
        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"{}", b"")
        mock_process.returncode = 0

        mock_create_subprocess = mocker.patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        tool = ClaudeCodeTool()
        await tool.fix_issue(sample_issue, str(test_file))

        # Verify command was called correctly
        args, _ = mock_create_subprocess.call_args
        assert args[0] == "claude"
        assert "--print" in args
        assert "--output-format" in args
        assert "json" in args
        assert "--permission-mode" in args
        assert "acceptEdits" in args
        assert "--allowedTools" in args

        # Verify prompt references a temp file
        prompt_arg = args[2]  # The prompt is the 3rd argument after "claude" and "--print"
        assert "Read" in prompt_arg
        assert "follow the instructions" in prompt_arg
        assert ".txt" in prompt_arg

    @pytest.mark.asyncio
    async def test_temp_file_cleanup(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test that temporary file is cleaned up after execution."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/claude")

        # Create a test file
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        # Track created temp files
        created_temp_files = []
        original_mkstemp = tempfile.mkstemp

        def track_mkstemp(*args, **kwargs):
            fd, temp_path = original_mkstemp(*args, **kwargs)
            created_temp_files.append(temp_path)
            return fd, temp_path

        mocker.patch("tempfile.mkstemp", side_effect=track_mkstemp)

        # Mock aiofiles.open to avoid actual async file I/O
        mock_file = mocker.AsyncMock()
        mock_file.__aenter__.return_value = mock_file
        mock_file.__aexit__.return_value = None
        mock_file.write = mocker.AsyncMock()
        mocker.patch("aiofiles.open", return_value=mock_file)

        # Mock subprocess
        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"{}", b"")
        mock_process.returncode = 0

        mocker.patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        tool = ClaudeCodeTool()
        await tool.fix_issue(sample_issue, str(test_file))

        # Verify temp file was created and then cleaned up
        assert len(created_temp_files) == 1
        temp_file_path = Path(created_temp_files[0])
        assert not temp_file_path.exists()  # Should be deleted

    def test_custom_timeout(self) -> None:
        """Test that custom timeout is accepted."""
        tool = ClaudeCodeTool(timeout=600)
        assert tool.timeout == 600
