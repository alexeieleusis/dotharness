"""Base classes and types for AI tool integration."""

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dotharness.vibe_heal.ai_tools.models import FixResult
    from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue, SonarQubeRule, SourceLine


class AIToolType(StrEnum):
    """Supported AI coding tools."""

    AIDER = "aider"
    CLAUDE_CODE = "claude-code"
    GEMINI = "gemini"
    OPENCODE = "opencode"

    @property
    def cli_command(self) -> str:
        """Get the CLI command name for this tool.

        Returns:
            CLI command name
        """
        return self.value

    @property
    def display_name(self) -> str:
        """Get human-readable display name.

        Returns:
            Display name for the tool
        """
        return {
            AIToolType.CLAUDE_CODE: "Claude Code",
            AIToolType.AIDER: "Aider",
            AIToolType.GEMINI: "Gemini",
            AIToolType.OPENCODE: "OpenCode",
        }[self]


class AITool(ABC):
    """Abstract base class for AI coding tools."""

    tool_type: AIToolType

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the tool is installed and accessible.

        Returns:
            True if tool is available, False otherwise
        """

    @abstractmethod
    async def fix_issue(
        self,
        issue: "SonarQubeIssue",
        file_path: str,
        rule: "SonarQubeRule | None" = None,
        code_context: "list[SourceLine] | None" = None,
        external_docs: "list[str] | None" = None,
    ) -> "FixResult":
        """Attempt to fix a SonarQube issue.

        Args:
            issue: The SonarQube issue to fix
            file_path: Path to the file containing the issue
            rule: Detailed rule information (optional)
            code_context: Source code lines around the issue (optional)
            external_docs: Documentation fetched from URLs in the issue message (optional)

        Returns:
            Result of the fix attempt
        """

    @abstractmethod
    async def fix_duplication(
        self,
        prompt: str,
        file_path: str,
    ) -> "FixResult":
        """Attempt to fix code duplication.

        Args:
            prompt: Detailed prompt describing the duplication
            file_path: Path to the file containing the duplication

        Returns:
            Result of the fix attempt
        """
