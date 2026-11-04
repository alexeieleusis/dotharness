"""Factory for creating AI tool instances."""

from typing import TYPE_CHECKING, ClassVar

from dotharness.vibe_heal.ai_tools.aider import AiderTool
from dotharness.vibe_heal.ai_tools.base import AITool, AIToolType
from dotharness.vibe_heal.ai_tools.claude_code import ClaudeCodeTool
from dotharness.vibe_heal.ai_tools.gemini import GeminiCliTool
from dotharness.vibe_heal.ai_tools.opencode import OpenCodeTool

if TYPE_CHECKING:
    from dotharness.vibe_heal.config import VibeHealConfig


class AIToolFactory:
    """Factory for creating AI tool instances."""

    _tool_map: ClassVar[dict[AIToolType, type[AITool]]] = {
        AIToolType.CLAUDE_CODE: ClaudeCodeTool,
        AIToolType.AIDER: AiderTool,
        AIToolType.GEMINI: GeminiCliTool,
        AIToolType.OPENCODE: OpenCodeTool,
    }

    @staticmethod
    def create(tool_type: AIToolType, config: "VibeHealConfig | None" = None) -> AITool:
        """Create an AI tool instance based on type.

        Args:
            tool_type: The type of AI tool to create
            config: Optional configuration for tool-specific settings

        Returns:
            AI tool instance

        Raises:
            ValueError: If tool type is not supported
        """
        # Create tool with config-specific parameters
        if tool_type == AIToolType.AIDER:
            if config:
                return AiderTool(
                    model=config.aider_model,
                    api_key=config.aider_api_key,
                    api_base=config.aider_api_base,
                    env_file_path=config.find_env_file(),
                )
            return AiderTool()

        if tool_type == AIToolType.CLAUDE_CODE:
            return ClaudeCodeTool()

        if tool_type == AIToolType.GEMINI:
            return GeminiCliTool()

        if tool_type == AIToolType.OPENCODE:
            return OpenCodeTool()

        msg = f"Unsupported AI tool type: {tool_type}"
        raise ValueError(msg)

    @staticmethod
    def detect_available() -> AIToolType | None:
        """Auto-detect first available AI tool.

        Tries tools in order of preference:
        1. Claude Code
        2. Aider
        3. Gemini
        4. OpenCode

        Returns:
            AIToolType of first available tool, or None if none found
        """
        # Try in preference order
        for tool_type in [AIToolType.CLAUDE_CODE, AIToolType.AIDER, AIToolType.GEMINI, AIToolType.OPENCODE]:
            try:
                tool = AIToolFactory.create(tool_type)
                if tool.is_available():
                    return tool_type
            except ValueError:
                continue

        return None
