"""Tests for Aider AI tool."""

import asyncio
import tempfile
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from dotharness.vibe_heal.ai_tools import AiderTool
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


class TestAiderTool:
    """Tests for AiderTool."""

    def test_is_available_when_aider_exists(self, mocker: MockerFixture) -> None:
        """Test is_available returns True when aider is installed."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/aider")

        tool = AiderTool()
        assert tool.is_available() is True

    def test_is_available_when_aider_missing(self, mocker: MockerFixture) -> None:
        """Test is_available returns False when aider is not installed."""
        mocker.patch("shutil.which", return_value=None)

        tool = AiderTool()
        assert tool.is_available() is False

    @pytest.mark.asyncio
    async def test_fix_issue_when_tool_not_available(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test fix_issue returns error when tool not available."""
        mocker.patch("shutil.which", return_value=None)

        tool = AiderTool()
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
        mocker.patch("shutil.which", return_value="/usr/local/bin/aider")

        tool = AiderTool()
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
        mocker.patch("shutil.which", return_value="/usr/local/bin/aider")

        # Create a temporary file
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo():\n    unused = 1\n    pass\n")

        # Mock subprocess
        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (
            b"Fixed the issue",
            b"",
        )
        mock_process.returncode = 0

        mocker.patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        tool = AiderTool()
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
        mocker.patch("shutil.which", return_value="/usr/local/bin/aider")

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

        tool = AiderTool()
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
        mocker.patch("shutil.which", return_value="/usr/local/bin/aider")

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

        tool = AiderTool(timeout=1)
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
        mocker.patch("shutil.which", return_value="/usr/local/bin/aider")

        # Create a temporary file
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        # Mock subprocess that raises exception
        mocker.patch(
            "asyncio.create_subprocess_exec",
            side_effect=RuntimeError("Unexpected error"),
        )

        tool = AiderTool()
        result = await tool.fix_issue(sample_issue, str(test_file))

        assert result.success is False
        assert "Error invoking Aider" in result.error_message

    @pytest.mark.asyncio
    async def test_command_construction(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test that command is constructed correctly."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/aider")

        # Create a temporary file
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        # Mock subprocess
        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"Fixed", b"")
        mock_process.returncode = 0

        mock_create_subprocess = mocker.patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        tool = AiderTool()
        await tool.fix_issue(sample_issue, str(test_file))

        # Verify command was called correctly
        args, _ = mock_create_subprocess.call_args
        assert args[0] == "aider"
        assert "--yes" in args
        assert "--no-git" in args
        assert "--message-file" in args
        # Verify a temp file path is passed
        message_file_idx = args.index("--message-file")
        assert message_file_idx < len(args) - 1
        assert args[message_file_idx + 1].endswith(".txt")

    @pytest.mark.asyncio
    async def test_message_file_created_in_cwd(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test that the prompt temp file is created in cwd, not the system temp dir."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/aider")

        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        mkstemp_calls = []
        original_mkstemp = tempfile.mkstemp

        def track_mkstemp(*args, **kwargs):
            mkstemp_calls.append(kwargs)
            return original_mkstemp(*args, **kwargs)

        mocker.patch("tempfile.mkstemp", side_effect=track_mkstemp)

        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"Fixed", b"")
        mock_process.returncode = 0

        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

        tool = AiderTool()
        await tool.fix_issue(sample_issue, str(test_file))

        assert len(mkstemp_calls) == 1
        assert mkstemp_calls[0].get("dir") == Path.cwd()

    def test_custom_timeout(self) -> None:
        """Test that custom timeout is accepted."""
        tool = AiderTool(timeout=600)
        assert tool.timeout == 600

    def test_initialization_with_model_config(self) -> None:
        """Test that model config is properly stored."""
        env_file = Path("/path/to/.env.vibeheal")
        tool = AiderTool(
            model="ollama_chat/gemma3:27b",
            api_key="test-key",
            api_base="http://localhost:11434",
            env_file_path=env_file,
        )
        assert tool.model == "ollama_chat/gemma3:27b"
        assert tool.api_key == "test-key"
        assert tool.api_base == "http://localhost:11434"
        assert tool.env_file_path == env_file

    @pytest.mark.asyncio
    async def test_model_flag_added_to_command(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test that --model flag is added when model is specified."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/aider")

        # Create a temporary file
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        # Mock subprocess
        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"Fixed", b"")
        mock_process.returncode = 0

        mock_create_subprocess = mocker.patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        tool = AiderTool(model="ollama_chat/gemma3:27b")
        await tool.fix_issue(sample_issue, str(test_file))

        # Verify --model flag was added
        args, _ = mock_create_subprocess.call_args
        assert "--model" in args
        model_index = args.index("--model")
        assert args[model_index + 1] == "ollama_chat/gemma3:27b"

    @pytest.mark.asyncio
    async def test_environment_variables_set(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test that environment variables are set when provided."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/aider")

        # Create a temporary file
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        # Mock subprocess
        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"Fixed", b"")
        mock_process.returncode = 0

        mock_create_subprocess = mocker.patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        tool = AiderTool(
            api_key="test-api-key",
            api_base="http://localhost:11434",
        )
        await tool.fix_issue(sample_issue, str(test_file))

        # Verify environment variables were set
        _, kwargs = mock_create_subprocess.call_args
        env = kwargs.get("env", {})
        assert env.get("OLLAMA_API_KEY") == "test-api-key"
        assert env.get("OLLAMA_API_BASE") == "http://localhost:11434"

    @pytest.mark.asyncio
    async def test_env_file_flag_added_to_command(
        self,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test that --env-file flag is added when env_file_path is specified."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/aider")

        # Create a temporary file and env file
        test_file = tmp_path / "test.py"
        test_file.write_text("code")
        env_file = tmp_path / ".env.vibeheal"
        env_file.write_text("SOME_VAR=value")

        # Mock subprocess
        mock_process = mocker.AsyncMock()
        mock_process.communicate.return_value = (b"Fixed", b"")
        mock_process.returncode = 0

        mock_create_subprocess = mocker.patch(
            "asyncio.create_subprocess_exec",
            return_value=mock_process,
        )

        tool = AiderTool(env_file_path=env_file)
        await tool.fix_issue(sample_issue, str(test_file))

        # Verify --env-file flag was added
        args, _ = mock_create_subprocess.call_args
        assert "--env-file" in args
        env_file_index = args.index("--env-file")
        assert args[env_file_index + 1] == str(env_file)
