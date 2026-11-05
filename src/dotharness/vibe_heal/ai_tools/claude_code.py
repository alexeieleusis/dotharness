"""Claude Code AI tool implementation."""

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, cast

from dotharness.ai_backend import ClaudeOptions, launch
from dotharness.vibe_heal.ai_tools.base import AITool, AIToolType
from dotharness.vibe_heal.ai_tools.models import FixResult

if TYPE_CHECKING:
    from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue, SonarQubeRule, SourceLine


CLAUDE_TIMEOUT_SECONDS = 1200


class ClaudeCodeTool(AITool):
    """Claude Code AI tool implementation.

    Decision D-6: the Claude timeout is one value, 1200 seconds (20 minutes). The same value is passed to the
    shared launcher (which enforces it and kills the whole process group) and printed in the timeout message.
    The earlier 300 s default and the literal 600 s wrapper are gone.
    """

    tool_type = AIToolType.CLAUDE_CODE

    def __init__(self, timeout: int = CLAUDE_TIMEOUT_SECONDS) -> None:
        """Initialize Claude Code tool.

        Args:
            timeout: Timeout in seconds for AI operations (default: 1200, decision D-6)
        """
        self.timeout = timeout

    def is_available(self) -> bool:
        """Check if Claude CLI is installed.

        Returns:
            True if claude command is available
        """
        return shutil.which("claude") is not None

    async def fix_issue(
        self,
        issue: "SonarQubeIssue",
        file_path: str,
        rule: "SonarQubeRule | None" = None,
        code_context: "list[SourceLine] | None" = None,
        external_docs: "list[str] | None" = None,
    ) -> FixResult:
        """Fix an issue using Claude Code.

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
                error_message="Claude CLI not found. Please install Claude Code first.",
            )

        # Verify file exists
        if not Path(file_path).exists():
            return FixResult(
                success=False,
                error_message=f"File not found: {file_path}",
            )

        # Create prompt with enriched context
        prompt = create_fix_prompt(issue, file_path, rule=rule, code_context=code_context, external_docs=external_docs)

        # Invoke Claude
        try:
            result = await self._invoke_claude(prompt, file_path)
        except Exception as e:
            return FixResult(
                success=False,
                error_message=f"Error invoking Claude: {e}",
            )
        else:
            return result

    async def fix_duplication(
        self,
        prompt: str,
        file_path: str,
    ) -> FixResult:
        """Fix code duplication using Claude Code.

        Args:
            prompt: Detailed prompt describing the duplication
            file_path: Path to the file containing the duplication

        Returns:
            Result of the fix attempt
        """
        if not self.is_available():
            return FixResult(
                success=False,
                error_message="Claude CLI not found. Please install Claude Code first.",
            )

        # Verify file exists
        if not Path(file_path).exists():
            return FixResult(
                success=False,
                error_message=f"File not found: {file_path}",
            )

        # Invoke Claude with the duplication prompt
        try:
            result = await self._invoke_claude(prompt, file_path)
        except Exception as e:
            return FixResult(
                success=False,
                error_message=f"Error invoking Claude: {e}",
            )
        else:
            return result

    async def _invoke_claude(
        self,
        prompt: str,
        file_path: str,
    ) -> FixResult:
        """Invoke Claude CLI.

        Args:
            prompt: The prompt to send to Claude
            file_path: File to fix

        Returns:
            FixResult with outcome
        """
        result = await launch(
            "claude",
            prompt,
            cwd=Path.cwd(),
            timeout=self.timeout,
            backend_options=ClaudeOptions(
                print_flag="--print",
                output_format="json",
                permission_mode="acceptEdits",
                allowed_tools="Edit,Read",
            ),
        )
        if result.timed_out:
            return FixResult(
                success=False,
                error_message=f"Claude timed out after {self.timeout} seconds",
                ai_response=result.stdout,
            )
        if result.not_found:
            return FixResult(
                success=False,
                error_message="Claude CLI not found. Please install Claude Code first.",
            )
        if result.returncode == 0:
            # Parse JSON response to extract information
            return FixResult(
                success=True,
                files_modified=self._parse_modified_files(result.stdout, file_path),
                ai_response=result.stdout,
            )
        return FixResult(
            success=False,
            error_message=f"Claude failed with exit code {result.returncode}: {result.stderr}",
            ai_response=result.stdout,
        )

    def _parse_modified_files(self, json_output: str, file_path: str) -> list[str]:
        """Parse JSON output to extract modified files.

        Args:
            json_output: JSON output from Claude CLI
            file_path: The file we asked Claude to fix

        Returns:
            List of modified file paths
        """
        try:
            data = json.loads(json_output)
        except json.JSONDecodeError:
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
        """Extract modified files from Claude CLI JSON data.

        Args:
            data: Parsed JSON data from Claude CLI

        Returns:
            Set of modified file paths
        """
        modified_files: set[str] = set()

        if not isinstance(data, dict):
            return modified_files

        # Check for 'toolUses' or similar fields that indicate Edit/Write operations
        tool_uses = data.get("toolUses", [])
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
        if tool_name not in ("Edit", "Write"):
            return None

        # Extract file_path parameter if present
        params = tool_use.get("parameters", {})
        if isinstance(params, dict) and "file_path" in params:
            return cast(str, params["file_path"])

        return None
