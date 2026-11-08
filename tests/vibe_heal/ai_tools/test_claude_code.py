"""Tests for Claude Code AI tool (the shared launcher is mocked)."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from dotharness.ai_backend import ClaudeOptions
from dotharness.subprocess_runner import RunResult
from dotharness.vibe_heal.ai_tools import ClaudeCodeTool
from dotharness.vibe_heal.ai_tools.claude_code import CLAUDE_TIMEOUT_SECONDS
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue

LAUNCH = "dotharness.vibe_heal.ai_tools.claude_code.launch"


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


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    """Create a file to fix."""
    path = tmp_path / "test.py"
    path.write_text("def foo():\n    unused = 1\n")
    return path


@pytest.fixture(autouse=True)
def installed(mocker: MockerFixture) -> None:
    """Pretend the CLI is installed."""
    mocker.patch("shutil.which", return_value="/usr/local/bin/claude")


def _launch_mock(mocker: MockerFixture, result: RunResult):
    return mocker.patch(LAUNCH, return_value=result)


class TestClaudeCodeTool:
    """Tests for ClaudeCodeTool."""

    def test_is_available(self, mocker: MockerFixture) -> None:
        """is_available follows shutil.which."""
        assert ClaudeCodeTool().is_available() is True
        mocker.patch("shutil.which", return_value=None)
        assert ClaudeCodeTool().is_available() is False

    @pytest.mark.asyncio
    async def test_fix_issue_when_tool_not_available(
        self, mocker: MockerFixture, sample_issue: SonarQubeIssue, source_file: Path
    ) -> None:
        """fix_issue reports a missing CLI without launching."""
        mocker.patch("shutil.which", return_value=None)
        launch = _launch_mock(mocker, RunResult(0, "", ""))

        result = await ClaudeCodeTool().fix_issue(sample_issue, str(source_file))

        assert result.success is False
        assert "not found" in (result.error_message or "").lower()
        launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_fix_issue_when_file_not_found(
        self, mocker: MockerFixture, sample_issue: SonarQubeIssue, tmp_path: Path
    ) -> None:
        """fix_issue reports a missing file without launching."""
        launch = _launch_mock(mocker, RunResult(0, "", ""))

        result = await ClaudeCodeTool().fix_issue(sample_issue, str(tmp_path / "nope.py"))

        assert result.success is False
        assert "File not found" in (result.error_message or "")
        launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_fix(self, mocker: MockerFixture, sample_issue: SonarQubeIssue, source_file: Path) -> None:
        """A zero exit is a success and the prompt reaches the launcher."""
        launch = _launch_mock(mocker, RunResult(0, '{"result": "ok"}', ""))

        result = await ClaudeCodeTool().fix_issue(sample_issue, str(source_file))

        assert result.success is True
        assert str(source_file) in result.files_modified
        assert result.error_message is None
        assert launch.call_args.args[0] == "claude"
        assert "python:S1481" in launch.call_args.args[1]

    @pytest.mark.asyncio
    async def test_launcher_arguments(
        self, mocker: MockerFixture, sample_issue: SonarQubeIssue, source_file: Path
    ) -> None:
        """Only launcher parameters are passed; no argv is built here."""
        launch = _launch_mock(mocker, RunResult(0, "", ""))

        await ClaudeCodeTool().fix_issue(sample_issue, str(source_file))

        kwargs = launch.call_args.kwargs
        assert kwargs["cwd"] == Path.cwd()
        assert kwargs["timeout"] == 1200
        assert kwargs["backend_options"] == ClaudeOptions(
            print_flag="--print", output_format="json", permission_mode="acceptEdits", allowed_tools="Edit,Read"
        )

    @pytest.mark.asyncio
    async def test_nonzero_exit(self, mocker: MockerFixture, sample_issue: SonarQubeIssue, source_file: Path) -> None:
        """A non-zero exit is a failure carrying stderr."""
        _launch_mock(mocker, RunResult(2, "out", "boom"))

        result = await ClaudeCodeTool().fix_issue(sample_issue, str(source_file))

        assert result.success is False
        assert "exit code 2" in (result.error_message or "")
        assert "boom" in (result.error_message or "")
        assert result.ai_response == "out"

    @pytest.mark.asyncio
    async def test_not_found_from_launcher(
        self, mocker: MockerFixture, sample_issue: SonarQubeIssue, source_file: Path
    ) -> None:
        """A launcher not_found result is reported as a missing CLI."""
        _launch_mock(mocker, RunResult(127, "", "no such file", not_found=True))

        result = await ClaudeCodeTool().fix_issue(sample_issue, str(source_file))

        assert result.success is False
        assert "not found" in (result.error_message or "").lower()

    @pytest.mark.asyncio
    async def test_timeout_message_has_configured_value(
        self, mocker: MockerFixture, sample_issue: SonarQubeIssue, source_file: Path
    ) -> None:
        """The timeout message prints the same value the launcher enforces."""
        launch = _launch_mock(mocker, RunResult(124, "", "", timed_out=True))

        result = await ClaudeCodeTool(timeout=7).fix_issue(sample_issue, str(source_file))

        assert result.success is False
        assert "timed out after 7 seconds" in (result.error_message or "")
        assert launch.call_args.kwargs["timeout"] == 7

    @pytest.mark.asyncio
    async def test_launcher_exception_is_reported(
        self, mocker: MockerFixture, sample_issue: SonarQubeIssue, source_file: Path
    ) -> None:
        """An exception from the launcher becomes a failed FixResult."""
        mocker.patch(LAUNCH, side_effect=RuntimeError("kaput"))

        result = await ClaudeCodeTool().fix_issue(sample_issue, str(source_file))

        assert result.success is False
        assert "kaput" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_fix_duplication(self, mocker: MockerFixture, source_file: Path) -> None:
        """fix_duplication launches with the given prompt."""
        launch = _launch_mock(mocker, RunResult(0, "", ""))

        result = await ClaudeCodeTool().fix_duplication("Remove duplicate code", str(source_file))

        assert result.success is True
        assert launch.call_args.args[1] == "Remove duplicate code"

    @pytest.mark.asyncio
    async def test_fix_duplication_failure(self, mocker: MockerFixture, source_file: Path) -> None:
        """A failing duplication fix is reported."""
        _launch_mock(mocker, RunResult(1, "", "Error"))

        result = await ClaudeCodeTool().fix_duplication("x", str(source_file))

        assert result.success is False
        assert result.error_message is not None

    @pytest.mark.asyncio
    async def test_fix_duplication_tool_not_available(self, mocker: MockerFixture, source_file: Path) -> None:
        """fix_duplication reports a missing CLI."""
        mocker.patch("shutil.which", return_value=None)

        result = await ClaudeCodeTool().fix_duplication("x", str(source_file))

        assert result.success is False
        assert "not found" in (result.error_message or "").lower()

    def test_default_timeout_is_1200(self) -> None:
        """Decision D-6: one default, 1200 s (no 300 default, no literal 600)."""
        assert ClaudeCodeTool().timeout == 1200
        assert CLAUDE_TIMEOUT_SECONDS == 1200

    @pytest.mark.asyncio
    async def test_default_timeout_flows_to_launcher_and_message(
        self, mocker: MockerFixture, sample_issue: SonarQubeIssue, source_file: Path
    ) -> None:
        """The default 1200 s reaches the launcher and appears in the timeout message."""
        launch = _launch_mock(mocker, RunResult(124, "", "", timed_out=True))

        result = await ClaudeCodeTool().fix_issue(sample_issue, str(source_file))

        assert launch.call_args.kwargs["timeout"] == 1200
        assert "timed out after 1200 seconds" in (result.error_message or "")

    def test_parse_modified_files(self) -> None:
        """Edit and Write tool uses are extracted from the JSON output."""
        output = (
            '{"toolUses": [{"name": "Edit", "parameters": {"file_path": "a.py"}},'
            ' {"name": "Read", "parameters": {"file_path": "b.py"}}]}'
        )
        assert ClaudeCodeTool()._parse_modified_files(output, "t.py") == ["a.py"]

    def test_parse_modified_files_falls_back_to_target(self) -> None:
        """Invalid JSON or no tool uses assume the target file was modified."""
        tool = ClaudeCodeTool()
        assert tool._parse_modified_files("not json", "t.py") == ["t.py"]
        assert tool._parse_modified_files("{}", "t.py") == ["t.py"]
