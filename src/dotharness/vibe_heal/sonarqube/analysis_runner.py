"""SonarQube analysis execution via sonar-scanner CLI."""

import asyncio
import logging
import re
import shutil
from pathlib import Path

from pydantic import BaseModel

from dotharness.subprocess_runner import run
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.output import dim, error, success, warn
from dotharness.vibe_heal.sonarqube.client import SonarQubeClient
from dotharness.vibe_heal.sonarqube.exceptions import SonarQubeAPIError
from dotharness.vibe_heal.sonarqube.properties_handler import SonarPropertiesHandler

logger = logging.getLogger(__name__)

_SERVER_ANALYSIS_TIMEOUT_SECONDS = 300

_AUTH_ERROR_RE = re.compile(r"401|403|unauthorized|authentication", re.IGNORECASE)
_AUTH_HINT = (
    "\nHint: authentication may be configured via environment variable "
    "(SONAR_TOKEN, SONAR_LOGIN) or the central scanner settings "
    "(~/.sonar/sonar-scanner.properties). Check these if you expected "
    "auth to be picked up automatically."
)


class AnalysisResult(BaseModel):
    """Result of a SonarQube analysis run."""

    success: bool
    retryable: bool = False  # True only for transient server-side failures
    task_id: str | None = None
    dashboard_url: str | None = None
    error_message: str | None = None


class AnalysisRunner:
    """Executes SonarQube analysis using sonar-scanner CLI.

    Runs sonar-scanner as a subprocess and waits for analysis completion
    on the SonarQube server.
    """

    def __init__(self, config: VibeHealConfig, client: SonarQubeClient) -> None:
        """Initialize the AnalysisRunner.

        Args:
            config: Application configuration
            client: SonarQube API client (for polling analysis status)
        """
        self.config = config
        self.client = client

    async def run_analysis(
        self,
        project_key: str,
        project_name: str,
        project_dir: Path,
        sources: list[Path] | None = None,
        max_retries: int = 2,
    ) -> AnalysisResult:
        """Run SonarQube analysis on project.

        Args:
            project_key: SonarQube project key
            project_name: SonarQube project name
            project_dir: Root directory to analyze
            sources: Optional list of specific files/dirs to analyze (relative to project_dir)
            max_retries: How many times to retry if server-side analysis fails (not for scanner errors)

        Returns:
            AnalysisResult with success status and details

        Raises:
            SonarQubeAPIError: If scanner execution fails or analysis times out
        """
        if not self.validate_scanner_available():
            return AnalysisResult(
                success=False,
                error_message="sonar-scanner is not installed or not in PATH. "
                "Install from: https://docs.sonarsource.com/sonarqube/latest/analyzing-source-code/scanners/sonarscanner/",
            )

        handler = SonarPropertiesHandler(project_dir, self.config)
        command = handler.build_command(project_key, project_name, sources)

        result = AnalysisResult(success=False, error_message="No analysis attempt made")
        with handler.patched(project_key, project_name):
            for attempt in range(max_retries + 1):
                if attempt > 0:
                    retry_delay = 10 * 2 ** (attempt - 1)
                    warn(
                        f"    Server-side analysis failed, retrying in {retry_delay}s"
                        f" (attempt {attempt + 1} of {max_retries + 1})..."
                    )
                    await asyncio.sleep(retry_delay)
                try:
                    result = await self._run_scanner_attempt(command, project_key, project_dir, handler)
                except Exception as e:
                    return AnalysisResult(success=False, error_message=f"Failed to run analysis: {e}")
                if result.success or not result.retryable:
                    return result
        return result

    async def _run_scanner_attempt(
        self,
        command: list[str],
        project_key: str,
        project_dir: Path,
        handler: SonarPropertiesHandler,
    ) -> AnalysisResult:
        """Execute sonar-scanner once and wait for server-side completion."""
        dim(f"    Executing: sonar-scanner (project: {project_key})")
        dim("    Waiting for scanner to complete...")
        timeout_seconds = self.config.scanner_timeout_seconds
        # Shared runner: own process group, SIGTERM->SIGKILL of the whole group (the sonar-scanner
        # wrapper's JVM child included) on timeout and on task cancellation.
        run_result = await run(command, cwd=project_dir, timeout=timeout_seconds)
        if run_result.not_found:
            return AnalysisResult(success=False, error_message=f"Failed to run analysis: {run_result.stderr}")
        if run_result.timed_out:
            error(f"    Scanner did not complete after {timeout_seconds}s — killed")
            return AnalysisResult(
                success=False,
                error_message=f"sonar-scanner did not complete after {timeout_seconds}s — killed",
            )
        returncode = run_result.returncode
        stdout_text = run_result.stdout
        stderr_text = run_result.stderr
        dim(f"    Scanner finished with exit code: {returncode}")

        if returncode != 0:
            combined = "\n".join(o for o in (stderr_text, stdout_text) if o)
            error(f"    Scanner error: {combined[:500]}")
            error_msg = f"sonar-scanner failed with exit code {returncode}: {combined}"
            if handler.exists and _AUTH_ERROR_RE.search(combined):
                error_msg += _AUTH_HINT
            return AnalysisResult(success=False, error_message=error_msg)

        scanner_output = stdout_text
        dim("    Extracting task ID from scanner output...")
        task_id = self._extract_task_id(scanner_output)

        if not task_id:
            error("    Could not find task ID in scanner output")
            dim("    See debug log for full scanner output")
            logger.debug("Full scanner output when task ID extraction failed:\n%s", scanner_output)
            return AnalysisResult(success=False, error_message="Could not extract task ID from scanner output")

        dim(f"    Task ID: {task_id}")
        dim("    Waiting for server-side analysis to complete...")
        try:
            async with asyncio.timeout(_SERVER_ANALYSIS_TIMEOUT_SECONDS):
                analysis_success, server_error = await self._wait_for_analysis(task_id)
        except TimeoutError:
            error(f"    Analysis timed out after {_SERVER_ANALYSIS_TIMEOUT_SECONDS} seconds")
            return AnalysisResult(
                success=False,
                task_id=task_id,
                error_message=f"Analysis timed out after {_SERVER_ANALYSIS_TIMEOUT_SECONDS} seconds",
            )

        if analysis_success:
            success("    ✓ Server-side analysis completed successfully")
            return AnalysisResult(
                success=True,
                task_id=task_id,
                dashboard_url=f"{self.config.sonarqube_url}/dashboard?id={project_key}",
            )

        error_message = "Analysis failed on server"
        if server_error:
            error_message += f": {server_error}"
        error(f"    {error_message}")
        return AnalysisResult(success=False, retryable=True, task_id=task_id, error_message=error_message)

    async def _wait_for_analysis(self, task_id: str) -> tuple[bool, str | None]:
        """Poll SonarQube for analysis completion.

        Args:
            task_id: Analysis task ID from scanner

        Returns:
            (True, None) if analysis succeeded; (False, errorMessage | None) if failed or canceled
        """
        poll_interval = 2  # seconds between polls
        last_status = None

        while True:
            try:
                # Query task status via API
                data = await self.client._request("GET", "/api/ce/task", params={"id": task_id})

                task = data.get("task", {})
                status = task.get("status")

                # Only log status changes to reduce noise
                if status != last_status:
                    dim(f"    Analysis status: {status}")
                    last_status = status

                if status == "SUCCESS":
                    return True, None
                if status in ("FAILED", "CANCELED"):
                    server_reason = task.get("errorMessage")
                    msg = f"    Analysis failed with status: {status}"
                    if server_reason:
                        msg += f" — {server_reason}"
                    error(msg)
                    return False, server_reason

                # Status is PENDING or IN_PROGRESS, keep waiting
                await asyncio.sleep(poll_interval)

            except SonarQubeAPIError as e:
                warn(f"    Warning: API error while polling: {e}")
                # API error, keep trying
                await asyncio.sleep(poll_interval)

    def _extract_task_id(self, scanner_output: str) -> str | None:
        """Extract task ID from sonar-scanner output.

        The scanner prints a line like:
        "More about the report processing at https://sonar.example.com/api/ce/task?id=AY..."

        Args:
            scanner_output: stdout from sonar-scanner

        Returns:
            Task ID if found, None otherwise
        """
        for line in scanner_output.split("\n"):
            if "api/ce/task?id=" in line:
                # Extract task ID from URL
                parts = line.split("api/ce/task?id=")
                if len(parts) > 1:
                    # Task ID is everything after "id=" until whitespace or end
                    task_id = parts[1].split()[0].strip()
                    return task_id

        return None

    def validate_scanner_available(self) -> bool:
        """Check if sonar-scanner is installed and available.

        Returns:
            True if sonar-scanner is in PATH, False otherwise
        """
        return shutil.which("sonar-scanner") is not None
