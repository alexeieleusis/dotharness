"""Tests for AnalysisRunner class."""

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from dotharness.subprocess_runner import RunResult
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.sonarqube.analysis_runner import AnalysisResult, AnalysisRunner
from dotharness.vibe_heal.sonarqube.client import SonarQubeClient
from dotharness.vibe_heal.sonarqube.exceptions import SonarQubeAPIError

_RUN_PATH = "dotharness.vibe_heal.sonarqube.analysis_runner.run"


def _fake_run(returncode: int, stdout: bytes, stderr: bytes) -> AsyncMock:
    """Stand-in for the shared subprocess runner returning a fixed RunResult."""
    return AsyncMock(return_value=RunResult(returncode=returncode, stdout=stdout.decode(), stderr=stderr.decode()))


@pytest.fixture
def config() -> VibeHealConfig:
    """Create test configuration."""
    return VibeHealConfig(
        sonarqube_url="https://sonar.test.com",
        sonarqube_token="test-token",  # noqa: S106
        sonarqube_project_key="my-project",
    )


@pytest.fixture
def mock_client() -> AsyncMock:
    """Create a mock SonarQubeClient."""
    client = AsyncMock(spec=SonarQubeClient)
    return client


@pytest.fixture
def analysis_runner(config: VibeHealConfig, mock_client: AsyncMock) -> AnalysisRunner:
    """Create an AnalysisRunner with mocked client."""
    return AnalysisRunner(config, mock_client)


class TestAnalysisRunnerInit:
    """Tests for AnalysisRunner initialization."""

    def test_init(self, config: VibeHealConfig, mock_client: AsyncMock) -> None:
        """Test AnalysisRunner initialization."""
        runner = AnalysisRunner(config, mock_client)

        assert runner.config == config
        assert runner.client == mock_client


class TestValidateScannerAvailable:
    """Tests for validate_scanner_available method."""

    def test_scanner_available(self, analysis_runner: AnalysisRunner) -> None:
        """Test when scanner is available in PATH."""
        with patch("shutil.which", return_value="/usr/bin/sonar-scanner"):
            assert analysis_runner.validate_scanner_available() is True

    def test_scanner_not_available(self, analysis_runner: AnalysisRunner) -> None:
        """Test when scanner is not in PATH."""
        with patch("shutil.which", return_value=None):
            assert analysis_runner.validate_scanner_available() is False


class TestExtractTaskId:
    """Tests for _extract_task_id method."""

    def test_extract_from_typical_output(self, analysis_runner: AnalysisRunner) -> None:
        """Test extracting task ID from typical scanner output."""
        output = """
INFO: Analysis report generated in 123ms
INFO: Analysis report uploaded in 456ms
INFO: More about the report processing at https://sonar.test.com/api/ce/task?id=AY1234567890
INFO: Analysis total time: 10.234 s
"""
        task_id = analysis_runner._extract_task_id(output)
        assert task_id == "AY1234567890"

    def test_extract_from_output_with_trailing_content(self, analysis_runner: AnalysisRunner) -> None:
        """Test extracting task ID when there's content after the ID."""
        output = """
INFO: More about the report processing at https://sonar.test.com/api/ce/task?id=AY_ABCD some other text
"""
        task_id = analysis_runner._extract_task_id(output)
        assert task_id == "AY_ABCD"

    def test_extract_returns_none_when_not_found(self, analysis_runner: AnalysisRunner) -> None:
        """Test that None is returned when task ID is not in output."""
        output = """
INFO: Analysis report generated
INFO: Some other info
"""
        task_id = analysis_runner._extract_task_id(output)
        assert task_id is None


class TestWaitForAnalysis:
    """Tests for _wait_for_analysis method."""

    @pytest.mark.asyncio
    async def test_analysis_succeeds_immediately(self, analysis_runner: AnalysisRunner, mock_client: AsyncMock) -> None:
        """Test when analysis succeeds on first poll."""
        mock_client._request.return_value = {"task": {"status": "SUCCESS"}}

        ok, reason = await analysis_runner._wait_for_analysis("test-task-id")

        assert ok is True
        assert reason is None
        mock_client._request.assert_called_once()

    @pytest.mark.asyncio
    async def test_analysis_succeeds_after_pending(
        self, analysis_runner: AnalysisRunner, mock_client: AsyncMock
    ) -> None:
        """Test when analysis succeeds after being pending."""
        # First call: PENDING, second call: IN_PROGRESS, third call: SUCCESS
        mock_client._request.side_effect = [
            {"task": {"status": "PENDING"}},
            {"task": {"status": "IN_PROGRESS"}},
            {"task": {"status": "SUCCESS"}},
        ]

        ok, reason = await analysis_runner._wait_for_analysis("test-task-id")

        assert ok is True
        assert reason is None
        assert mock_client._request.call_count == 3

    @pytest.mark.asyncio
    async def test_analysis_fails(self, analysis_runner: AnalysisRunner, mock_client: AsyncMock) -> None:
        """Test when analysis fails."""
        mock_client._request.return_value = {"task": {"status": "FAILED", "errorMessage": "Out of memory"}}

        ok, reason = await analysis_runner._wait_for_analysis("test-task-id")

        assert ok is False
        assert reason == "Out of memory"

    @pytest.mark.asyncio
    async def test_analysis_fails_no_error_message(
        self, analysis_runner: AnalysisRunner, mock_client: AsyncMock
    ) -> None:
        """Test when analysis fails without an errorMessage from SonarQube."""
        mock_client._request.return_value = {"task": {"status": "FAILED"}}

        ok, reason = await analysis_runner._wait_for_analysis("test-task-id")

        assert ok is False
        assert reason is None

    @pytest.mark.asyncio
    async def test_analysis_canceled(self, analysis_runner: AnalysisRunner, mock_client: AsyncMock) -> None:
        """Test when analysis is canceled."""
        mock_client._request.return_value = {"task": {"status": "CANCELED"}}

        ok, reason = await analysis_runner._wait_for_analysis("test-task-id")

        assert ok is False
        assert reason is None

    @pytest.mark.asyncio
    async def test_analysis_timeout(self, analysis_runner: AnalysisRunner, mock_client: AsyncMock) -> None:
        """Test when analysis times out."""
        import asyncio

        # Always return IN_PROGRESS to simulate timeout
        mock_client._request.return_value = {"task": {"status": "IN_PROGRESS"}}

        # Use timeout context manager as the caller would
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):  # Very short timeout for test
                await analysis_runner._wait_for_analysis("test-task-id")

    @pytest.mark.asyncio
    async def test_analysis_api_error_recovers(self, analysis_runner: AnalysisRunner, mock_client: AsyncMock) -> None:
        """Test recovery from temporary API errors."""
        # First call fails, second succeeds
        mock_client._request.side_effect = [
            SonarQubeAPIError("Temporary error"),
            {"task": {"status": "SUCCESS"}},
        ]

        ok, reason = await analysis_runner._wait_for_analysis("test-task-id")

        assert ok is True
        assert reason is None
        assert mock_client._request.call_count == 2


class TestRunAnalysis:
    """Tests for run_analysis method."""

    @pytest.mark.asyncio
    async def test_scanner_not_available(self, analysis_runner: AnalysisRunner, tmp_path: Path) -> None:
        """Test when scanner is not installed."""
        with patch.object(analysis_runner, "validate_scanner_available", return_value=False):
            result = await analysis_runner.run_analysis(
                project_key="test-key",
                project_name="Test Project",
                project_dir=tmp_path,
            )

        assert result.success is False
        assert "not installed" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_successful_analysis(
        self, analysis_runner: AnalysisRunner, mock_client: AsyncMock, tmp_path: Path
    ) -> None:
        """Test successful analysis execution."""
        scanner_output = b"""
INFO: Analysis report uploaded in 456ms
INFO: More about the report processing at https://sonar.test.com/api/ce/task?id=AY123
INFO: Analysis total time: 10.234 s
"""

        # Mock subprocess
        mock_run = _fake_run(0, scanner_output, b"")

        with (
            patch.object(analysis_runner, "validate_scanner_available", return_value=True),
            patch(_RUN_PATH, mock_run),
            patch.object(analysis_runner, "_wait_for_analysis", return_value=(True, None)),
        ):
            result = await analysis_runner.run_analysis(
                project_key="test-key",
                project_name="Test Project",
                project_dir=tmp_path,
            )

        assert result.success is True
        assert result.task_id == "AY123"
        assert result.dashboard_url == "https://sonar.test.com/dashboard?id=test-key"
        assert result.error_message is None

    @pytest.mark.asyncio
    async def test_scanner_hang_times_out_and_kills_process(
        self, analysis_runner: AnalysisRunner, tmp_path: Path
    ) -> None:
        """A scanner subprocess that never completes must be killed, not awaited forever."""
        mock_run = AsyncMock(
            return_value=RunResult(returncode=124, stdout="", stderr="timed out after 0.05s", timed_out=True)
        )

        cast("Any", analysis_runner.config).scanner_timeout_seconds = 0.05

        with (
            patch.object(analysis_runner, "validate_scanner_available", return_value=True),
            patch(_RUN_PATH, mock_run),
        ):
            result = await analysis_runner.run_analysis(
                project_key="test-key",
                project_name="Test Project",
                project_dir=tmp_path,
            )

        assert result.success is False
        assert result.retryable is False
        assert "did not complete" in (result.error_message or "")
        mock_run.assert_awaited_once()
        assert mock_run.await_args.kwargs["timeout"] == 0.05

    @pytest.mark.asyncio
    async def test_scanner_execution_fails(self, analysis_runner: AnalysisRunner, tmp_path: Path) -> None:
        """Test when scanner execution fails."""
        mock_run = _fake_run(1, b"", b"ERROR: Analysis failed")

        with (
            patch.object(analysis_runner, "validate_scanner_available", return_value=True),
            patch(_RUN_PATH, mock_run),
        ):
            result = await analysis_runner.run_analysis(
                project_key="test-key",
                project_name="Test Project",
                project_dir=tmp_path,
            )

        assert result.success is False
        assert "exit code 1" in (result.error_message or "")
        assert "ERROR: Analysis failed" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_no_task_id_in_output(self, analysis_runner: AnalysisRunner, tmp_path: Path) -> None:
        """Test when task ID cannot be extracted from output."""
        mock_run = _fake_run(0, b"No task ID here", b"")

        with (
            patch.object(analysis_runner, "validate_scanner_available", return_value=True),
            patch(_RUN_PATH, mock_run),
        ):
            result = await analysis_runner.run_analysis(
                project_key="test-key",
                project_name="Test Project",
                project_dir=tmp_path,
            )

        assert result.success is False
        assert "Could not extract task ID" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_analysis_failed_on_server(self, analysis_runner: AnalysisRunner, tmp_path: Path) -> None:
        """Test when analysis fails on SonarQube server."""
        scanner_output = b"""
INFO: More about the report processing at https://sonar.test.com/api/ce/task?id=AY123
"""

        mock_run = _fake_run(0, scanner_output, b"")

        with (
            patch.object(analysis_runner, "validate_scanner_available", return_value=True),
            patch(_RUN_PATH, mock_run),
            patch.object(analysis_runner, "_wait_for_analysis", return_value=(False, "Out of memory")),
        ):
            result = await analysis_runner.run_analysis(
                project_key="test-key",
                project_name="Test Project",
                project_dir=tmp_path,
                max_retries=0,
            )

        assert result.success is False
        assert result.task_id == "AY123"
        assert "failed on server" in (result.error_message or "")
        assert "Out of memory" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_analysis_succeeds_after_retry(self, analysis_runner: AnalysisRunner, tmp_path: Path) -> None:
        """Test that a transient server failure is retried and eventually succeeds."""
        scanner_output = b"""
INFO: More about the report processing at https://sonar.test.com/api/ce/task?id=AY123
"""
        mock_run = _fake_run(0, scanner_output, b"")

        with (
            patch.object(analysis_runner, "validate_scanner_available", return_value=True),
            patch(_RUN_PATH, mock_run),
            patch.object(
                analysis_runner,
                "_wait_for_analysis",
                side_effect=[(False, "Fail to extract report from database"), (True, None)],
            ),
            patch("asyncio.sleep"),
        ):
            result = await analysis_runner.run_analysis(
                project_key="test-key",
                project_name="Test Project",
                project_dir=tmp_path,
                max_retries=1,
            )

        assert result.success is True
        assert result.task_id == "AY123"
        assert mock_run.call_count == 2

    @pytest.mark.asyncio
    async def test_analysis_exhausts_retries(self, analysis_runner: AnalysisRunner, tmp_path: Path) -> None:
        """Test that persistent server failures return the last failure after all retries."""
        scanner_output = b"""
INFO: More about the report processing at https://sonar.test.com/api/ce/task?id=AY123
"""
        mock_run = _fake_run(0, scanner_output, b"")

        with (
            patch.object(analysis_runner, "validate_scanner_available", return_value=True),
            patch(_RUN_PATH, mock_run),
            patch.object(
                analysis_runner,
                "_wait_for_analysis",
                side_effect=[(False, "Fail to extract report from database")] * 3,
            ),
            patch("asyncio.sleep"),
        ):
            result = await analysis_runner.run_analysis(
                project_key="test-key",
                project_name="Test Project",
                project_dir=tmp_path,
                max_retries=2,
            )

        assert result.success is False
        assert "failed on server" in (result.error_message or "")
        assert "Fail to extract report from database" in (result.error_message or "")
        assert mock_run.call_count == 3

    @pytest.mark.asyncio
    async def test_analysis_timeout_on_server(self, analysis_runner: AnalysisRunner, tmp_path: Path) -> None:
        """Test when analysis times out on SonarQube server."""
        import asyncio

        scanner_output = b"""
INFO: More about the report processing at https://sonar.test.com/api/ce/task?id=AY123
"""

        mock_run = _fake_run(0, scanner_output, b"")

        # Mock _wait_for_analysis to sleep forever (simulating timeout)
        async def wait_forever(task_id: str) -> tuple[bool, str | None]:
            await asyncio.sleep(1000)
            return True, None

        real_timeout = asyncio.timeout

        with (
            patch.object(analysis_runner, "validate_scanner_available", return_value=True),
            patch(_RUN_PATH, mock_run),
            patch.object(analysis_runner, "_wait_for_analysis", side_effect=wait_forever),
            # Fresh Timeout instance per call — asyncio.Timeout can't be entered twice, and
            # this function now opens a timeout around both the scanner wait and this one.
            patch("asyncio.timeout", side_effect=lambda _: real_timeout(0.1)),
        ):
            result = await analysis_runner.run_analysis(
                project_key="test-key",
                project_name="Test Project",
                project_dir=tmp_path,
            )

        assert result.success is False
        assert result.task_id == "AY123"
        assert "timed out after 300 seconds" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_subprocess_exception(self, analysis_runner: AnalysisRunner, tmp_path: Path) -> None:
        """Test handling of subprocess exceptions."""
        with (
            patch.object(analysis_runner, "validate_scanner_available", return_value=True),
            patch(_RUN_PATH, side_effect=Exception("Subprocess error")),
        ):
            result = await analysis_runner.run_analysis(
                project_key="test-key",
                project_name="Test Project",
                project_dir=tmp_path,
            )

        assert result.success is False
        assert "Failed to run analysis" in (result.error_message or "")
        assert "Subprocess error" in (result.error_message or "")


class TestAnalysisResult:
    """Tests for AnalysisResult model."""

    def test_successful_result(self) -> None:
        """Test creating a successful result."""
        result = AnalysisResult(
            success=True,
            task_id="AY123",
            dashboard_url="https://sonar.test.com/dashboard?id=test",
        )

        assert result.success is True
        assert result.task_id == "AY123"
        assert result.dashboard_url == "https://sonar.test.com/dashboard?id=test"
        assert result.error_message is None

    def test_failed_result(self) -> None:
        """Test creating a failed result."""
        result = AnalysisResult(
            success=False,
            error_message="Analysis failed",
        )

        assert result.success is False
        assert result.error_message == "Analysis failed"
        assert result.task_id is None
        assert result.dashboard_url is None

    def test_retryable_defaults_to_false(self) -> None:
        """Test that retryable is False by default so non-server failures are not retried."""
        result = AnalysisResult(success=False, error_message="Some error")
        assert result.retryable is False

    def test_retryable_true_for_server_failure(self) -> None:
        """Test that retryable can be set explicitly for server-side failures."""
        result = AnalysisResult(success=False, retryable=True, error_message="Analysis failed on server")
        assert result.retryable is True


class TestAuthHint:
    @pytest.mark.asyncio
    async def test_auth_hint_added_when_properties_file_and_auth_error(
        self, analysis_runner: AnalysisRunner, tmp_path: Path
    ) -> None:
        (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=orig\n")
        mock_run = _fake_run(1, b"", b"ERROR: 401 Unauthorized")
        with (
            patch.object(analysis_runner, "validate_scanner_available", return_value=True),
            patch(_RUN_PATH, mock_run),
        ):
            result = await analysis_runner.run_analysis(
                project_key="temp-key",
                project_name="Temp",
                project_dir=tmp_path,
            )
        assert result.success is False
        assert "SONAR_TOKEN" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_no_auth_hint_without_properties_file(self, analysis_runner: AnalysisRunner, tmp_path: Path) -> None:
        mock_run = _fake_run(1, b"", b"ERROR: 401 Unauthorized")
        with (
            patch.object(analysis_runner, "validate_scanner_available", return_value=True),
            patch(_RUN_PATH, mock_run),
        ):
            result = await analysis_runner.run_analysis(
                project_key="test-key",
                project_name="Test",
                project_dir=tmp_path,
            )
        assert result.success is False
        assert "SONAR_TOKEN" not in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_no_auth_hint_for_non_auth_failure(self, analysis_runner: AnalysisRunner, tmp_path: Path) -> None:
        (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=orig\n")
        mock_run = _fake_run(1, b"", b"ERROR: Project not found")
        with (
            patch.object(analysis_runner, "validate_scanner_available", return_value=True),
            patch(_RUN_PATH, mock_run),
        ):
            result = await analysis_runner.run_analysis(
                project_key="temp-key",
                project_name="Temp",
                project_dir=tmp_path,
            )
        assert result.success is False
        assert "SONAR_TOKEN" not in (result.error_message or "")
