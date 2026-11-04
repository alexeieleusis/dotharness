"Gemini CLI AI tool implementation."

import asyncio
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, cast

from dotharness.vibe_heal.ai_tools.base import AITool, AIToolType
from dotharness.vibe_heal.ai_tools.models import FixResult
from dotharness.vibe_heal.ai_tools.utils import build_file_prompt, run_command, temp_prompt_file

if TYPE_CHECKING:
    from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue, SonarQubeRule, SourceLine


class GeminiCliTool(AITool):
    """Gemini CLI AI tool implementation."""

    tool_type = AIToolType.GEMINI

    def __init__(self, timeout: int = 300) -> None:
        """Initialize Gemini CLI tool.

        Args:
            timeout: Timeout in seconds for AI operations (default: 5 minutes)
        """
        self.timeout = timeout

    def is_available(self) -> bool:
        """Check if Gemini CLI is installed.

        Returns:
            True if gemini command is available
        """
        return shutil.which("gemini") is not None

    async def fix_issue(
        self,
        issue: "SonarQubeIssue",
        file_path: str,
        rule: "SonarQubeRule | None" = None,
        code_context: "list[SourceLine] | None" = None,
        external_docs: "list[str] | None" = None,
    ) -> FixResult:
        """Fix an issue using Gemini CLI.

        Args:
            issue: The SonarQube issue to fix
            file_path: Path to the file containing the issue
            rule: Detailed rule information (optional)
            code_context: Source code lines around the issue (optional)
            external_docs: Documentation fetched from URLs in the issue message (optional)

        Returns:
            Result of the fix attempt
        """
        # Import here to avoid circular dependency
        from dotharness.vibe_heal.ai_tools.prompts import create_fix_prompt

        if not self.is_available():
            return FixResult(
                success=False,
                error_message="Gemini CLI not found. Please install it first.",
            )

        # Verify file exists
        if not Path(file_path).exists():
            return FixResult(
                success=False,
                error_message=f"File not found: {file_path}",
            )

        # Create prompt with enriched context
        prompt = create_fix_prompt(issue, file_path, rule=rule, code_context=code_context, external_docs=external_docs)

        # Invoke Gemini
        try:
            result = await self._invoke_gemini(prompt, file_path)
        except TimeoutError:
            return FixResult(
                success=False,
                error_message=f"Gemini timed out after {self.timeout} seconds",
            )
        except Exception as e:
            return FixResult(
                success=False,
                error_message=f"Error invoking Gemini: {e}",
            )
        else:
            return result

    async def fix_duplication(
        self,
        prompt: str,
        file_path: str,
    ) -> FixResult:
        """Fix code duplication using Gemini CLI.

        Args:
            prompt: Detailed prompt describing the duplication
            file_path: Path to the file containing the duplication

        Returns:
            Result of the fix attempt
        """
        if not self.is_available():
            return FixResult(
                success=False,
                error_message="Gemini CLI not found. Please install it first.",
            )

        # Verify file exists
        if not Path(file_path).exists():
            return FixResult(
                success=False,
                error_message=f"File not found: {file_path}",
            )

        # Invoke Gemini with the duplication prompt
        try:
            result = await self._invoke_gemini(prompt, file_path)
        except TimeoutError:
            return FixResult(
                success=False,
                error_message=f"Gemini timed out after {self.timeout} seconds",
            )
        except Exception as e:
            return FixResult(
                success=False,
                error_message=f"Error invoking Gemini: {e}",
            )
        else:
            return result

    async def _invoke_gemini(
        self,
        prompt: str,
        file_path: str,
    ) -> FixResult:
        """Invoke Gemini CLI.

        Args:
            prompt: The prompt to send to Gemini
            file_path: File to fix

        Returns:
            FixResult with outcome
        """
        # Create a temporary file for the detailed instructions
        async with temp_prompt_file(prompt) as temp_file_path:
            # Build command with JSON output for structured parsing
            cmd = [
                "gemini",
                build_file_prompt(temp_file_path),
                "--output-format",
                "json",
                "--approval-mode",
                "auto_edit",
            ]

            async with asyncio.timeout(600):
                command_result = await run_command(cmd)
            if command_result.success:
                # Parse JSON response to extract information
                files_modified = self._parse_modified_files(command_result.stdout, file_path)

                return FixResult(
                    success=True,
                    files_modified=files_modified,
                    ai_response=command_result.stdout,
                )
            else:
                return FixResult(
                    success=False,
                    error_message=f"Gemini failed with exit code {command_result.exit_code}: {command_result.stderr}",
                    ai_response=command_result.stdout,
                )

    def _parse_modified_files(self, json_output: str, file_path: str) -> list[str]:
        """Parse JSON output to extract modified files.

        Args:
            json_output: JSON output from Gemini CLI
            file_path: The file we asked Gemini to fix

        Returns:
            List of modified file paths
        """
        try:
            # Gemini may output multiple JSON objects in a stream
            # We are interested in the last one which is a summary
            *_, last_json_output = json_output.strip().split("\n")
            data = json.loads(last_json_output)
        except ValueError:
            # If JSON parsing fails, assume the file was modified
            return [file_path]

        # Extract modified files from tool uses
        modified_files = self._extract_modified_files_from_data(data)

        # If we found specific modified files, return them
        if modified_files:
            return list(modified_files)

        # Otherwise, assume the target file was modified if JSON was valid
        return [file_path]

    def _extract_modified_files_from_data(self, data: dict | list) -> set[str]:
        """Extract modified files from Gemini CLI JSON data.

        Args:
            data: Parsed JSON data from Gemini CLI

        Returns:
            Set of modified file paths
        """
        modified_files: set[str] = set()

        if not isinstance(data, dict):
            return modified_files

        # Check for 'tool_code' that indicate Edit/Write operations
        tool_uses = data.get("tool_code", [])
        for tool_use in tool_uses:
            file_path = self._extract_file_path_from_tool_use(tool_use)
            if file_path:
                modified_files.add(file_path)

        return modified_files

    def _extract_file_path_from_tool_use(self, tool_use: dict | list) -> str | None:
        """Extract file path from a tool use entry.

        Args:
            tool_use: Tool use dictionary

        Returns:
            File path if found, None otherwise
        """
        if not isinstance(tool_use, dict):
            return None

        tool_name = tool_use.get("name", "")
        # Edit and Write tools modify files
        if tool_name not in ("edit", "write_file"):
            return None

        # Extract file_path parameter if present
        params = tool_use.get("parameters", {})
        if isinstance(params, dict) and "file_path" in params:
            return cast(str, params["file_path"])

        return None
