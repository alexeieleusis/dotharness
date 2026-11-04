"""Tests for Gemini CLI AI tool."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pytest_mock import MockerFixture

from dotharness.vibe_heal.ai_tools.gemini import GeminiCliTool
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue

# Mark all tests in this file as asyncio
# pytestmark = pytest.mark.asyncio


@pytest.fixture
def gemini_tool() -> GeminiCliTool:
    """Fixture for GeminiCliTool."""
    return GeminiCliTool(timeout=10)


@pytest.fixture
def mock_issue() -> SonarQubeIssue:
    """Fixture for a mock SonarQube issue."""
    return SonarQubeIssue(
        key="test-issue",
        rule="python:S1135",
        component="src/main.py",
        project="test-project",
        line=10,
        message="Test issue message",
        status="OPEN",
        severity="MAJOR",
        type="CODE_SMELL",
        textRange={"startLine": 10, "endLine": 10, "startOffset": 4, "endOffset": 15},
    )


class TestGeminiCliTool:
    """Tests for GeminiCliTool."""

    def test_is_available(self, mocker: MockerFixture, gemini_tool: GeminiCliTool) -> None:
        """Test is_available returns True if 'gemini' is on PATH."""
        mocker.patch("shutil.which", return_value="/usr/bin/gemini")
        assert gemini_tool.is_available()

    def test_is_not_available(self, mocker: MockerFixture, gemini_tool: GeminiCliTool) -> None:
        """Test is_available returns False if 'gemini' is not on PATH."""
        mocker.patch("shutil.which", return_value=None)
        assert not gemini_tool.is_available()

    @pytest.mark.asyncio
    async def test_fix_issue_gemini_not_found(
        self,
        gemini_tool: GeminiCliTool,
        mock_issue: SonarQubeIssue,
        tmp_path: Path,
    ) -> None:
        """Test fix_issue returns error if Gemini CLI is not found."""
        with patch.object(gemini_tool, "is_available", return_value=False):
            file_path = tmp_path / "test.py"
            file_path.touch()
            result = await gemini_tool.fix_issue(mock_issue, str(file_path))
            assert not result.success
            assert "Gemini CLI not found" in result.error_message

    @pytest.mark.asyncio
    async def test_fix_issue_file_not_found(
        self,
        gemini_tool: GeminiCliTool,
        mock_issue: SonarQubeIssue,
    ) -> None:
        """Test fix_issue returns error if file does not exist."""
        with patch.object(gemini_tool, "is_available", return_value=True):
            result = await gemini_tool.fix_issue(mock_issue, "non_existent_file.py")
            assert not result.success
            assert "File not found" in result.error_message

    @pytest.mark.asyncio
    async def test_fix_duplication_gemini_not_found(
        self,
        gemini_tool: GeminiCliTool,
        tmp_path: Path,
    ) -> None:
        """Test fix_duplication returns error if Gemini CLI is not found."""
        with patch.object(gemini_tool, "is_available", return_value=False):
            file_path = tmp_path / "test.py"
            file_path.touch()
            result = await gemini_tool.fix_duplication("prompt", str(file_path))
            assert not result.success
            assert "Gemini CLI not found" in result.error_message

    @pytest.mark.asyncio
    async def test_invoke_gemini_timeout(
        self,
        gemini_tool: GeminiCliTool,
        tmp_path: Path,
        mocker: MockerFixture,
    ) -> None:
        """Test that _invoke_gemini handles asyncio.TimeoutError."""
        mocker.patch("asyncio.create_subprocess_exec", side_effect=asyncio.TimeoutError)
        file_path = tmp_path / "test.py"
        file_path.touch()
        with pytest.raises(asyncio.TimeoutError):
            await gemini_tool._invoke_gemini("prompt", str(file_path))

    @pytest.mark.asyncio
    async def test_fix_issue_success(
        self,
        gemini_tool: GeminiCliTool,
        mock_issue: SonarQubeIssue,
        tmp_path: Path,
        mocker: MockerFixture,
    ) -> None:
        """Test a successful issue fix."""
        file_path = tmp_path / "src/main.py"
        file_path.parent.mkdir()
        file_path.touch()

        # Mock the subprocess call
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (
            b"""{"tool_code": [{"name": "edit", "parameters": {"file_path": "src/main.py"}}]}""",
            b"",
        )
        mock_process.returncode = 0
        mock_create_subprocess = mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

        with patch.object(gemini_tool, "is_available", return_value=True):
            result = await gemini_tool.fix_issue(mock_issue, str(file_path))

            assert result.success
            assert result.files_modified == ["src/main.py"]
            assert "tool_code" in result.ai_response

        # Verify prompt references a temp file rather than embedding it directly
        args, _ = mock_create_subprocess.call_args
        prompt_arg = args[1]
        assert "Read" in prompt_arg
        assert "follow the instructions" in prompt_arg
        assert ".txt" in prompt_arg

    @pytest.mark.asyncio
    async def test_fix_issue_failure(
        self,
        gemini_tool: GeminiCliTool,
        mock_issue: SonarQubeIssue,
        tmp_path: Path,
        mocker: MockerFixture,
    ) -> None:
        """Test a failed issue fix."""
        file_path = tmp_path / "test.py"
        file_path.touch()

        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"{}", b"Error from Gemini")
        mock_process.returncode = 1
        mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

        with patch.object(gemini_tool, "is_available", return_value=True):
            result = await gemini_tool.fix_issue(mock_issue, str(file_path))

            assert not result.success
            assert "Gemini failed" in result.error_message
            assert "Error from Gemini" in result.error_message

    @pytest.mark.asyncio
    async def test_parse_modified_files(self, gemini_tool: GeminiCliTool) -> None:
        """Test parsing of modified files from Gemini's JSON output."""
        json_output = (
            '{"other_data": "value"}\n'
            '{"tool_code": [{"name": "edit", "parameters": {"file_path": "path/to/file1.py"}}]}\n'
        )
        modified = gemini_tool._parse_modified_files(json_output, "path/to/default.py")
        assert modified == ["path/to/file1.py"]

    @pytest.mark.asyncio
    async def test_parse_modified_files_no_tool_code(self, gemini_tool: GeminiCliTool) -> None:
        """Test parsing when 'tool_code' is missing."""
        json_output = '{"last_object": true}\n'
        modified = gemini_tool._parse_modified_files(json_output, "path/to/default.py")
        assert modified == ["path/to/default.py"]

    @pytest.mark.asyncio
    async def test_parse_modified_files_invalid_json(self, gemini_tool: GeminiCliTool) -> None:
        """Test parsing with invalid JSON."""
        json_output = "this is not json"
        modified = gemini_tool._parse_modified_files(json_output, "path/to/default.py")
        assert modified == ["path/to/default.py"]
