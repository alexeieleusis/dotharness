"""OpenCode AI tool implementation."""

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from dotharness.ai_backend import OpencodeOptions, launch
from dotharness.vibe_heal.ai_tools.base import AITool, AIToolType
from dotharness.vibe_heal.ai_tools.models import FixResult

if TYPE_CHECKING:
    from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue, SonarQubeRule, SourceLine

_CLI_NOT_FOUND_MESSAGE = "OpenCode CLI not found. Please install OpenCode first."


class OpenCodeTool(AITool):
    """OpenCode AI tool implementation."""

    tool_type = AIToolType.OPENCODE

    def __init__(
        self,
        timeout: int = 1200,
        model: str | None = None,
    ) -> None:
        """Initialize OpenCode tool.

        Args:
            timeout: Timeout in seconds for AI operations (default: 20 minutes)
            model: Model to use in provider/model format (e.g., 'anthropic/claude-sonnet-4-5')
        """
        self.timeout = timeout
        self.model = model

    def is_available(self) -> bool:
        """Check if OpenCode CLI is installed.

        Returns:
            True if opencode command is available
        """
        return shutil.which("opencode") is not None

    async def fix_issue(
        self,
        issue: "SonarQubeIssue",
        file_path: str,
        rule: "SonarQubeRule | None" = None,
        code_context: "list[SourceLine] | None" = None,
        external_docs: "list[str] | None" = None,
    ) -> FixResult:
        """Fix an issue using OpenCode.

        Args:
            issue: The SonarQube issue to fix
            file_path: Path to the file containing the issue
            rule: Detailed rule information (optional)
            code_context: Source code lines around the issue (optional)
            external_docs: Documentation fetched from URLs in the issue message (optional)

        Returns:
            Result of the fix attempt
        """
        from dotharness.vibe_heal.ai_tools.prompts import create_fix_prompt

        if not self.is_available():
            return FixResult(
                success=False,
                error_message=_CLI_NOT_FOUND_MESSAGE,
            )

        if not Path(file_path).exists():
            return FixResult(
                success=False,
                error_message=f"File not found: {file_path}",
            )

        prompt = create_fix_prompt(issue, file_path, rule=rule, code_context=code_context, external_docs=external_docs)

        try:
            result = await self._invoke_opencode(prompt, file_path)
        except Exception as e:
            return FixResult(
                success=False,
                error_message=f"Error invoking OpenCode: {e}",
            )
        else:
            return result

    async def fix_duplication(
        self,
        prompt: str,
        file_path: str,
    ) -> FixResult:
        """Fix code duplication using OpenCode.

        Args:
            prompt: Detailed prompt describing the duplication
            file_path: Path to the file containing the duplication

        Returns:
            Result of the fix attempt
        """
        if not self.is_available():
            return FixResult(
                success=False,
                error_message=_CLI_NOT_FOUND_MESSAGE,
            )

        if not Path(file_path).exists():
            return FixResult(
                success=False,
                error_message=f"File not found: {file_path}",
            )

        try:
            result = await self._invoke_opencode(prompt, file_path)
        except Exception as e:
            return FixResult(
                success=False,
                error_message=f"Error invoking OpenCode: {e}",
            )
        else:
            return result

    async def _invoke_opencode(
        self,
        prompt: str,
        file_path: str,
    ) -> FixResult:
        """Invoke OpenCode CLI.

        Args:
            prompt: The prompt to send to OpenCode
            file_path: File to fix

        Returns:
            FixResult with outcome
        """
        result = await launch(
            "opencode",
            prompt,
            cwd=Path.cwd(),
            timeout=self.timeout,
            backend_options=OpencodeOptions(model=self.model),
        )
        if result.timed_out:
            return FixResult(
                success=False,
                error_message=f"OpenCode timed out after {self.timeout} seconds",
                ai_response=result.stdout,
            )
        if result.not_found:
            return FixResult(
                success=False,
                error_message=_CLI_NOT_FOUND_MESSAGE,
            )
        if result.returncode == 0:
            return FixResult(
                success=True,
                files_modified=[file_path],
                ai_response=result.stdout,
            )
        return FixResult(
            success=False,
            error_message=f"OpenCode failed with exit code {result.returncode}: {result.stderr}",
            ai_response=result.stdout,
        )
