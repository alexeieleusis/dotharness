"""Tests for AI tool factory."""

from pytest_mock import MockerFixture

from dotharness.vibe_heal.ai_tools import (
    AIToolFactory,
    AIToolType,
    ClaudeCodeTool,
    OpenCodeTool,
)


class TestAIToolFactory:
    """Tests for AIToolFactory."""

    def test_create_claude_code_tool(self) -> None:
        """Test creating Claude Code tool."""
        tool = AIToolFactory.create(AIToolType.CLAUDE_CODE)

        assert isinstance(tool, ClaudeCodeTool)
        assert tool.tool_type == AIToolType.CLAUDE_CODE

    def test_create_opencode_tool(self) -> None:
        """Test creating OpenCode tool."""
        tool = AIToolFactory.create(AIToolType.OPENCODE)

        assert isinstance(tool, OpenCodeTool)
        assert tool.tool_type == AIToolType.OPENCODE

    def test_detect_available_when_claude_exists(self, mocker: MockerFixture) -> None:
        """Test auto-detection when Claude is available."""
        # Mock shutil.which to return Claude path
        mocker.patch("shutil.which", return_value="/usr/local/bin/claude")

        detected = AIToolFactory.detect_available()

        assert detected == AIToolType.CLAUDE_CODE

    def test_detect_available_when_only_opencode_exists(self, mocker: MockerFixture) -> None:
        """Test auto-detection falls back to opencode when claude is missing."""

        def which_mock(cmd: str) -> str | None:
            return "/usr/local/bin/opencode" if cmd == "opencode" else None

        mocker.patch("shutil.which", side_effect=which_mock)

        assert AIToolFactory.detect_available() == AIToolType.OPENCODE

    def test_detect_prefers_claude_over_opencode(self, mocker: MockerFixture) -> None:
        """Test auto-detection order: claude-code, then opencode."""
        mocker.patch("shutil.which", return_value="/usr/local/bin/any")

        assert AIToolFactory.detect_available() == AIToolType.CLAUDE_CODE

    def test_detect_available_when_no_tools(self, mocker: MockerFixture) -> None:
        """Test auto-detection when no tools available."""
        # Mock shutil.which to return None (tool not found)
        mocker.patch("shutil.which", return_value=None)

        detected = AIToolFactory.detect_available()

        assert detected is None

    def test_factory_returns_new_instances(self) -> None:
        """Test that factory creates new instances each time."""
        tool1 = AIToolFactory.create(AIToolType.CLAUDE_CODE)
        tool2 = AIToolFactory.create(AIToolType.CLAUDE_CODE)

        assert tool1 is not tool2
        assert isinstance(tool1, ClaudeCodeTool)
        assert isinstance(tool2, ClaudeCodeTool)
