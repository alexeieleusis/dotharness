"""Tests for AI tool factory."""

from pathlib import Path

from pytest_mock import MockerFixture

from dotharness.vibe_heal.ai_tools import (
    AiderTool,
    AIToolFactory,
    AIToolType,
    ClaudeCodeTool,
    GeminiCliTool,
)


class TestAIToolFactory:
    """Tests for AIToolFactory."""

    def test_create_claude_code_tool(self) -> None:
        """Test creating Claude Code tool."""
        tool = AIToolFactory.create(AIToolType.CLAUDE_CODE)

        assert isinstance(tool, ClaudeCodeTool)
        assert tool.tool_type == AIToolType.CLAUDE_CODE

    def test_create_aider_tool(self) -> None:
        """Test creating Aider tool."""
        tool = AIToolFactory.create(AIToolType.AIDER)

        assert isinstance(tool, AiderTool)
        assert tool.tool_type == AIToolType.AIDER

    def test_create_gemini_tool(self) -> None:
        """Test creating Gemini CLI tool."""
        tool = AIToolFactory.create(AIToolType.GEMINI)

        assert isinstance(tool, GeminiCliTool)
        assert tool.tool_type == AIToolType.GEMINI

    def test_detect_available_when_claude_exists(self, mocker: MockerFixture) -> None:
        """Test auto-detection when Claude is available."""
        # Mock shutil.which to return Claude path
        mocker.patch("shutil.which", return_value="/usr/local/bin/claude")

        detected = AIToolFactory.detect_available()

        assert detected == AIToolType.CLAUDE_CODE

    def test_detect_available_when_aider_exists(self, mocker: MockerFixture) -> None:
        """Test auto-detection when Aider is available but Claude is not."""

        # Mock shutil.which to return None for claude, path for aider
        def which_mock(cmd: str) -> str | None:
            if cmd == "claude":
                return None
            if cmd == "aider":
                return "/usr/local/bin/aider"
            return None

        mocker.patch("shutil.which", side_effect=which_mock)

        detected = AIToolFactory.detect_available()

        assert detected == AIToolType.AIDER

    def test_detect_available_when_gemini_exists(self, mocker: MockerFixture) -> None:
        """Test auto-detection when Gemini is available but others are not."""

        def which_mock(cmd: str) -> str | None:
            if cmd in ("claude", "aider"):
                return None
            if cmd == "gemini":
                return "/usr/local/bin/gemini"
            return None

        mocker.patch("shutil.which", side_effect=which_mock)

        detected = AIToolFactory.detect_available()

        assert detected == AIToolType.GEMINI

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

        tool3 = AIToolFactory.create(AIToolType.AIDER)
        tool4 = AIToolFactory.create(AIToolType.AIDER)

        assert tool3 is not tool4
        assert isinstance(tool3, AiderTool)
        assert isinstance(tool4, AiderTool)

    def test_create_aider_with_config(self, mocker: MockerFixture) -> None:
        """Test creating Aider tool with configuration."""
        # Create mock config
        mock_config = mocker.MagicMock()
        mock_config.aider_model = "ollama_chat/gemma3:27b"
        mock_config.aider_api_key = "test-key"
        mock_config.aider_api_base = "http://localhost:11434"
        env_file = Path("/path/to/.env.vibeheal")
        mock_config.find_env_file.return_value = env_file

        tool = AIToolFactory.create(AIToolType.AIDER, config=mock_config)

        assert isinstance(tool, AiderTool)
        assert tool.model == "ollama_chat/gemma3:27b"
        assert tool.api_key == "test-key"
        assert tool.api_base == "http://localhost:11434"
        assert tool.env_file_path == env_file

    def test_create_aider_without_config(self) -> None:
        """Test creating Aider tool without configuration."""
        tool = AIToolFactory.create(AIToolType.AIDER)

        assert isinstance(tool, AiderTool)
        assert tool.model is None
        assert tool.api_key is None
        assert tool.api_base is None
