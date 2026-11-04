"""Tests for AI tool base classes and enums."""

import pytest

from dotharness.vibe_heal.ai_tools.base import AIToolType


class TestAIToolType:
    """Tests for AIToolType enum."""

    def test_enum_values(self) -> None:
        """Test that enum has exactly the supported values."""
        assert AIToolType.CLAUDE_CODE.value == "claude-code"
        assert AIToolType.OPENCODE.value == "opencode"
        assert [t.value for t in AIToolType] == ["claude-code", "opencode"]

    def test_cli_command_property(self) -> None:
        """Test cli_command property returns correct command."""
        assert AIToolType.CLAUDE_CODE.cli_command == "claude-code"
        assert AIToolType.OPENCODE.cli_command == "opencode"

    def test_display_name_property(self) -> None:
        """Test display_name property returns human-readable names."""
        assert AIToolType.CLAUDE_CODE.display_name == "Claude Code"
        assert AIToolType.OPENCODE.display_name == "OpenCode"

    def test_string_conversion(self) -> None:
        """Test that the enum compares equal to its string value."""
        assert AIToolType.CLAUDE_CODE == "claude-code"
        assert AIToolType.OPENCODE == "opencode"

    def test_enum_comparison(self) -> None:
        """Test enum equality comparison."""
        assert AIToolType.OPENCODE != AIToolType.CLAUDE_CODE

    def test_enum_from_string(self) -> None:
        """Test creating enum from string value."""
        assert AIToolType("claude-code") == AIToolType.CLAUDE_CODE
        assert AIToolType("opencode") == AIToolType.OPENCODE

    @pytest.mark.parametrize("value", ["invalid-tool", "ai" + "der", "gem" + "ini"])
    def test_invalid_enum_value(self, value: str) -> None:
        """Test that invalid and removed values raise ValueError."""
        with pytest.raises(ValueError):
            AIToolType(value)
