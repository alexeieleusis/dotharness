"""Tests for OpenCode AI tool."""

import asyncio
import tempfile
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from dotharness.vibe_heal.ai_tools import OpenCodeTool
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


class TestOpenCodeTool:
    """Tests for OpenCodeTool."""

    def test_is_available_when_opencode_exists(self, mocker: MockerFixture) -> None:
        """Test is_available returns True when opencode is installed."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/opencode")

        tool = OpenCodeTool()
        assert tool.is_available() is True

    def test_is_available_when_opencode_missing(self, mocker: MockerFixture) -> None:
        """Test is_available returns False when opencode is not installed."""
        mocker.patch("shutil.which", return_value=None)

        tool = OpenCodeTool()
        assert tool.is_available() is False

    @pytest.mark.asyncio
    async def test_fix_issue_when_tool_not_available(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test fix_issue returns error when tool not available."""
        mocker.patch("shutil.which", return_value=None)

        tool = OpenCodeTool()
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
        mocker.patch("shutil.which", return_value="/usr/local/bin/opencode")

        tool = OpenCodeTool()
        nonexistent_file = tmp_path / "nonexistent.py"
        result = await tool.fix_issue(sample_issue, str(nonexistent_file))

        assert result.success is False
        assert "File not found" in result.error_message

    @pytest.mark.asyncio
    async def test_fix_issue_success(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test successful fix execution."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/opencode")

        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    unused = 1\n    pass\n")

        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"Fixed the issue", b"")
        mock_process.returncode = 0

        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

        tool = OpenCodeTool()
        result = await tool.fix_issue(sample_issue, str(test_file))

        assert result.success is True
        assert result.failed is False
        assert str(test_file) in result.files_modified
        assert result.error_message is None

    @pytest.mark.asyncio
    async def test_fix_issue_command_failure(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test handling of command execution failure."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/opencode")

        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"", b"Error: Command failed")
        mock_process.returncode = 1

        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

        tool = OpenCodeTool()
        result = await tool.fix_issue(sample_issue, str(test_file))

        assert result.success is False
        assert "failed" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_fix_issue_timeout(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test handling of timeout."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/opencode")

        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        mock_process = mocker.AsyncMock()
        mock_process.communicate.side_effect = asyncio.TimeoutError()
        mock_process.kill = mocker.Mock()
        mock_process.wait = mocker.AsyncMock()

        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

        tool = OpenCodeTool(timeout=1)
        result = await tool.fix_issue(sample_issue, str(test_file))

        assert result.success is False
        assert "timed out" in result.error_message.lower()
        mock_process.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_fix_issue_exception_handling(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test handling of unexpected exceptions."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/opencode")

        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        mocker.patch(
            "asyncio.create_subprocess_exec",
            side_effect=RuntimeError("Unexpected error"),
        )

        tool = OpenCodeTool()
        result = await tool.fix_issue(sample_issue, str(test_file))

        assert result.success is False
        assert "Error invoking OpenCode" in result.error_message

    @pytest.mark.asyncio
    async def test_fix_issue_command_construction(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that command is constructed correctly."""
        monkeypatch.chdir(tmp_path)
        mocker.patch("shutil.which", return_value="/usr/local/bin/opencode")

        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"Fixed", b"")
        mock_process.returncode = 0

        mock_create_subprocess = mocker.patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        tool = OpenCodeTool()
        await tool.fix_issue(sample_issue, str(test_file))

        args, _ = mock_create_subprocess.call_args
        assert args[0] == "opencode"
        assert args[1] == "run"
        assert len(args) >= 3  # opencode run <prompt>

        # Verify prompt references a temp file rather than embedding it directly
        prompt_arg = args[2]
        assert "Read" in prompt_arg
        assert "follow the instructions" in prompt_arg
        assert ".txt" in prompt_arg

    @pytest.mark.asyncio
    async def test_temp_file_cleanup(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that the temporary prompt file is cleaned up after execution."""
        monkeypatch.chdir(tmp_path)
        mocker.patch("shutil.which", return_value="/usr/local/bin/opencode")

        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        created_temp_files = []
        original_mkstemp = tempfile.mkstemp

        def track_mkstemp(*args, **kwargs):
            fd, temp_path = original_mkstemp(*args, **kwargs)
            created_temp_files.append(temp_path)
            return fd, temp_path

        mocker.patch("tempfile.mkstemp", side_effect=track_mkstemp)

        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"Fixed", b"")
        mock_process.returncode = 0

        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

        tool = OpenCodeTool()
        await tool.fix_issue(sample_issue, str(test_file))

        assert len(created_temp_files) == 1
        temp_file_path = Path(created_temp_files[0])
        assert not temp_file_path.exists()  # Should be deleted

    @pytest.mark.asyncio
    async def test_temp_file_cleanup_on_command_failure(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that the temporary prompt file is cleaned up even when the command fails."""
        monkeypatch.chdir(tmp_path)
        mocker.patch("shutil.which", return_value="/usr/local/bin/opencode")

        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        created_temp_files = []
        original_mkstemp = tempfile.mkstemp

        def track_mkstemp(*args, **kwargs):
            fd, temp_path = original_mkstemp(*args, **kwargs)
            created_temp_files.append(temp_path)
            return fd, temp_path

        mocker.patch("tempfile.mkstemp", side_effect=track_mkstemp)

        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"", b"Error: Command failed")
        mock_process.returncode = 1

        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

        tool = OpenCodeTool()
        result = await tool.fix_issue(sample_issue, str(test_file))

        assert result.success is False
        assert len(created_temp_files) == 1
        temp_file_path = Path(created_temp_files[0])
        assert not temp_file_path.exists()  # Should still be deleted on failure

    @pytest.mark.asyncio
    async def test_fix_issue_command_includes_model(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test that -m flag is added when model is specified."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/opencode")

        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"Fixed", b"")
        mock_process.returncode = 0

        mock_create_subprocess = mocker.patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        tool = OpenCodeTool(model="anthropic/claude-sonnet-4-5")
        await tool.fix_issue(sample_issue, str(test_file))

        args, _ = mock_create_subprocess.call_args
        assert "-m" in args
        model_index = list(args).index("-m")
        assert args[model_index + 1] == "anthropic/claude-sonnet-4-5"

    def test_custom_timeout(self) -> None:
        """Test that custom timeout is accepted."""
        tool = OpenCodeTool(timeout=600)
        assert tool.timeout == 600

    def test_initialization_with_model(self) -> None:
        """Test that model config is properly stored."""
        tool = OpenCodeTool(model="anthropic/claude-sonnet-4-5")
        assert tool.model == "anthropic/claude-sonnet-4-5"

    @pytest.mark.asyncio
    async def test_fix_duplication_success(
        self,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """Test successful duplication fix."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/opencode")

        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"Refactored", b"")
        mock_process.returncode = 0

        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

        tool = OpenCodeTool()
        result = await tool.fix_duplication("Remove duplicate code", str(test_file))

        assert result.success is True
        assert str(test_file) in result.files_modified

    @pytest.mark.asyncio
    async def test_fix_duplication_failure(
        self,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """Test failed duplication fix."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/opencode")

        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"", b"Error")
        mock_process.returncode = 1

        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

        tool = OpenCodeTool()
        result = await tool.fix_duplication("Remove duplicate code", str(test_file))

        assert result.success is False
        assert result.error_message is not None

    @pytest.mark.asyncio
    async def test_fix_duplication_tool_not_available(
        self,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """Test fix_duplication returns error when tool not available."""
        mocker.patch("shutil.which", return_value=None)

        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        tool = OpenCodeTool()
        result = await tool.fix_duplication("Remove duplicate code", str(test_file))

        assert result.success is False
        assert "not found" in result.error_message.lower()
