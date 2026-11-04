"""AI tool integration for vibe_heal."""

from typing import Any

from dotharness.vibe_heal.ai_tools.aider import AiderTool
from dotharness.vibe_heal.ai_tools.base import AITool, AIToolType
from dotharness.vibe_heal.ai_tools.claude_code import ClaudeCodeTool
from dotharness.vibe_heal.ai_tools.factory import AIToolFactory
from dotharness.vibe_heal.ai_tools.gemini import GeminiCliTool
from dotharness.vibe_heal.ai_tools.models import FixResult
from dotharness.vibe_heal.ai_tools.opencode import OpenCodeTool

__all__ = [
    "AITool",
    "AIToolFactory",
    "AIToolType",
    "AiderTool",
    "ClaudeCodeTool",
    "FixResult",
    "GeminiCliTool",
    "OpenCodeTool",
    "create_fix_prompt",
]


def __getattr__(name: str) -> Any:
    """Lazy import of create_fix_prompt to avoid circular imports."""
    if name == "create_fix_prompt":
        from dotharness.vibe_heal.ai_tools.prompts import create_fix_prompt as _create_fix_prompt

        return _create_fix_prompt
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
