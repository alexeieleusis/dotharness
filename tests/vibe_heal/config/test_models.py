"""Tests for configuration models."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dotharness.vibe_heal.ai_tools.base import AIToolType
from dotharness.vibe_heal.config import InvalidConfigurationError, VibeHealConfig


class TestVibeHealConfig:
    """Tests for VibeHealConfig model."""

    @pytest.fixture(autouse=True)
    def _no_scanner_auth_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Prevent real machine-local scanner config from leaking into these tests."""
        monkeypatch.setattr("dotharness.vibe_heal.config.models.resolve_scanner_auth", lambda project_dir: None)
        monkeypatch.setattr("dotharness.vibe_heal.config.models.resolve_scanner_host_url", lambda project_dir: None)

    def test_valid_config_with_token(self) -> None:
        """Test creating config with token authentication."""
        config = VibeHealConfig(
            sonarqube_url="https://sonar.example.com",
            sonarqube_token="test-token",
            sonarqube_project_key="my-project",
        )

        assert config.sonarqube_url == "https://sonar.example.com"
        assert config.sonarqube_token == "test-token"
        assert config.sonarqube_project_key == "my-project"
        assert config.use_token_auth is True

    def test_valid_config_with_basic_auth(self) -> None:
        """Test creating config with basic authentication."""
        config = VibeHealConfig(
            sonarqube_url="https://sonar.example.com",
            sonarqube_username="user",
            sonarqube_password="pass",
            sonarqube_project_key="my-project",
        )

        assert config.sonarqube_username == "user"
        assert config.sonarqube_password == "pass"
        assert config.use_token_auth is False

    def test_invalid_missing_auth(self) -> None:
        """Test that missing authentication raises error when no scanner auth is discoverable either."""
        with pytest.raises(InvalidConfigurationError, match="Must provide either"):
            VibeHealConfig(
                sonarqube_url="https://sonar.example.com",
                sonarqube_project_key="my-project",
            )

    def test_invalid_only_username(self) -> None:
        """Test that only username (without password) raises error when no scanner auth is discoverable either."""
        with pytest.raises(InvalidConfigurationError, match="Must provide either"):
            VibeHealConfig(
                sonarqube_url="https://sonar.example.com",
                sonarqube_username="user",
                sonarqube_project_key="my-project",
            )

    def test_invalid_only_password(self) -> None:
        """Test that only password (without username) raises error when no scanner auth is discoverable either."""
        with pytest.raises(InvalidConfigurationError, match="Must provide either"):
            VibeHealConfig(
                sonarqube_url="https://sonar.example.com",
                sonarqube_password="pass",
                sonarqube_project_key="my-project",
            )

    def test_falls_back_to_scanner_token_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a token discovered via scanner conventions satisfies auth."""
        monkeypatch.setattr(
            "dotharness.vibe_heal.config.models.resolve_scanner_auth",
            lambda project_dir: {"token": "discovered-token"},
        )
        config = VibeHealConfig(
            sonarqube_url="https://sonar.example.com",
            sonarqube_project_key="my-project",
        )

        assert config.sonarqube_token == "discovered-token"
        assert config.use_token_auth is True

    def test_scanner_fallback_not_used_when_token_already_provided(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that an explicitly provided token is not overridden by scanner discovery."""
        monkeypatch.setattr(
            "dotharness.vibe_heal.config.models.resolve_scanner_auth",
            lambda project_dir: {"token": "discovered-token"},
        )
        config = VibeHealConfig(
            sonarqube_url="https://sonar.example.com",
            sonarqube_token="explicit-token",
            sonarqube_project_key="my-project",
        )

        assert config.sonarqube_token == "explicit-token"

    def test_falls_back_to_scanner_host_url_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a host URL discovered via scanner conventions is used."""
        monkeypatch.setattr(
            "dotharness.vibe_heal.config.models.resolve_scanner_host_url",
            lambda project_dir: "https://discovered.example.com",
        )
        config = VibeHealConfig(
            sonarqube_token="test-token",
            sonarqube_project_key="my-project",
        )

        assert config.sonarqube_url == "https://discovered.example.com"

    def test_scanner_url_fallback_not_used_when_url_already_provided(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that an explicitly provided URL is not overridden by scanner discovery."""
        monkeypatch.setattr(
            "dotharness.vibe_heal.config.models.resolve_scanner_host_url",
            lambda project_dir: "https://discovered.example.com",
        )
        config = VibeHealConfig(
            sonarqube_url="https://explicit.example.com",
            sonarqube_token="test-token",
            sonarqube_project_key="my-project",
        )

        assert config.sonarqube_url == "https://explicit.example.com"

    def test_url_normalization_trailing_slash(self) -> None:
        """Test that trailing slash is removed from URL."""
        config = VibeHealConfig(
            sonarqube_url="https://sonar.example.com/",
            sonarqube_token="token",
            sonarqube_project_key="my-project",
        )

        assert config.sonarqube_url == "https://sonar.example.com"

    def test_url_normalization_multiple_slashes(self) -> None:
        """Test that multiple trailing slashes are removed."""
        config = VibeHealConfig(
            sonarqube_url="https://sonar.example.com///",
            sonarqube_token="token",
            sonarqube_project_key="my-project",
        )

        assert config.sonarqube_url == "https://sonar.example.com"

    def test_ai_tool_from_string_lowercase(self) -> None:
        """Test parsing AI tool from lowercase string."""
        config = VibeHealConfig(
            sonarqube_url="https://sonar.example.com",
            sonarqube_token="token",
            sonarqube_project_key="my-project",
            ai_tool="claude-code",
        )

        assert config.ai_tool == AIToolType.CLAUDE_CODE

    def test_ai_tool_from_string_uppercase(self) -> None:
        """Test parsing AI tool from uppercase string (case insensitive)."""
        config = VibeHealConfig(
            sonarqube_url="https://sonar.example.com",
            sonarqube_token="token",
            sonarqube_project_key="my-project",
            ai_tool="CLAUDE-CODE",
        )

        assert config.ai_tool == AIToolType.CLAUDE_CODE

    def test_ai_tool_aider_from_string(self) -> None:
        """Test parsing Aider from lowercase string."""
        config = VibeHealConfig(
            sonarqube_url="https://sonar.example.com",
            sonarqube_token="token",
            sonarqube_project_key="my-project",
            ai_tool="aider",
        )

        assert config.ai_tool == AIToolType.AIDER

    def test_ai_tool_from_enum(self) -> None:
        """Test that enum value is accepted directly."""
        config = VibeHealConfig(
            sonarqube_url="https://sonar.example.com",
            sonarqube_token="token",
            sonarqube_project_key="my-project",
            ai_tool=AIToolType.AIDER,
        )

        assert config.ai_tool == AIToolType.AIDER

    def test_ai_tool_none(self) -> None:
        """Test that None is accepted for ai_tool (auto-detect)."""
        config = VibeHealConfig(
            sonarqube_url="https://sonar.example.com",
            sonarqube_token="token",
            sonarqube_project_key="my-project",
        )

        assert config.ai_tool is None

    def test_ai_tool_invalid_string(self) -> None:
        """Test that invalid AI tool string raises error."""
        with pytest.raises(InvalidConfigurationError, match="Invalid AI tool"):
            VibeHealConfig(
                sonarqube_url="https://sonar.example.com",
                sonarqube_token="token",
                sonarqube_project_key="my-project",
                ai_tool="invalid-tool",
            )

    def test_missing_required_url(self) -> None:
        """Test that missing URL raises ValidationError."""
        with pytest.raises(ValidationError, match="sonarqube_url"):
            VibeHealConfig(
                sonarqube_token="token",
                sonarqube_project_key="my-project",
            )

    def test_missing_required_project_key(self) -> None:
        """Test that missing project key raises ValidationError."""
        with pytest.raises(ValidationError, match="sonarqube_project_key"):
            VibeHealConfig(
                sonarqube_url="https://sonar.example.com",
                sonarqube_token="token",
            )

    def test_token_auth_takes_precedence(self) -> None:
        """Test that token auth is preferred when both methods provided."""
        config = VibeHealConfig(
            sonarqube_url="https://sonar.example.com",
            sonarqube_token="token",
            sonarqube_username="user",
            sonarqube_password="pass",
            sonarqube_project_key="my-project",
        )

        # Token auth should be used when both are present
        assert config.use_token_auth is True

    def test_find_env_file_vibeheal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test find_env_file returns .env.vibeheal when it exists."""
        # Change to tmp directory
        monkeypatch.chdir(tmp_path)

        # Create .env.vibeheal
        env_file = tmp_path / ".env.vibeheal"
        env_file.write_text("TEST=value")

        result = VibeHealConfig.find_env_file()
        assert result is not None
        assert result.name == ".env.vibeheal"
        assert result == env_file.absolute()

    def test_find_env_file_fallback_to_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test find_env_file falls back to .env when .env.vibeheal doesn't exist."""
        # Change to tmp directory
        monkeypatch.chdir(tmp_path)

        # Create only .env
        env_file = tmp_path / ".env"
        env_file.write_text("TEST=value")

        result = VibeHealConfig.find_env_file()
        assert result is not None
        assert result.name == ".env"
        assert result == env_file.absolute()

    def test_find_env_file_prefers_vibeheal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test find_env_file prefers .env.vibeheal over .env when both exist."""
        # Change to tmp directory
        monkeypatch.chdir(tmp_path)

        # Create both files
        vibeheal_file = tmp_path / ".env.vibeheal"
        vibeheal_file.write_text("TEST=vibeheal")
        env_file = tmp_path / ".env"
        env_file.write_text("TEST=env")

        result = VibeHealConfig.find_env_file()
        assert result is not None
        assert result.name == ".env.vibeheal"
        assert result == vibeheal_file.absolute()

    def test_find_env_file_returns_none_when_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test find_env_file returns None when no env files exist."""
        # Change to tmp directory
        monkeypatch.chdir(tmp_path)

        result = VibeHealConfig.find_env_file()
        assert result is None

    def test_custom_env_file_path_str(self, tmp_path: Path) -> None:
        """Test loading config from custom env file (string path)."""
        # Create custom env file
        custom_env = tmp_path / ".env.custom"
        custom_env.write_text(
            "SONARQUBE_URL=https://custom.example.com\n"
            "SONARQUBE_TOKEN=custom-token\n"
            "SONARQUBE_PROJECT_KEY=custom-project\n"
        )

        config = VibeHealConfig(env_file=str(custom_env))

        assert config.sonarqube_url == "https://custom.example.com"
        assert config.sonarqube_token == "custom-token"
        assert config.sonarqube_project_key == "custom-project"

    def test_custom_env_file_path_object(self, tmp_path: Path) -> None:
        """Test loading config from custom env file (Path object)."""
        # Create custom env file
        custom_env = tmp_path / ".env.custom"
        custom_env.write_text(
            "SONARQUBE_URL=https://custom.example.com\n"
            "SONARQUBE_TOKEN=custom-token\n"
            "SONARQUBE_PROJECT_KEY=custom-project\n"
        )

        config = VibeHealConfig(env_file=custom_env)

        assert config.sonarqube_url == "https://custom.example.com"
        assert config.sonarqube_token == "custom-token"
        assert config.sonarqube_project_key == "custom-project"

    def test_custom_env_file_not_found(self, tmp_path: Path) -> None:
        """Test that non-existent custom env file raises error."""
        non_existent = tmp_path / ".env.nonexistent"

        with pytest.raises(InvalidConfigurationError, match="Environment file not found"):
            VibeHealConfig(env_file=non_existent)

    def test_custom_env_file_overrides_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that custom env file takes precedence over default files."""
        # Change to tmp directory
        monkeypatch.chdir(tmp_path)

        # Create default .env.vibeheal
        default_env = tmp_path / ".env.vibeheal"
        default_env.write_text(
            "SONARQUBE_URL=https://default.example.com\n"
            "SONARQUBE_TOKEN=default-token\n"
            "SONARQUBE_PROJECT_KEY=default-project\n"
        )

        # Create custom env file
        custom_env = tmp_path / ".env.custom"
        custom_env.write_text(
            "SONARQUBE_URL=https://custom.example.com\n"
            "SONARQUBE_TOKEN=custom-token\n"
            "SONARQUBE_PROJECT_KEY=custom-project\n"
        )

        # Custom env file should be used
        config = VibeHealConfig(env_file=custom_env)

        assert config.sonarqube_url == "https://custom.example.com"
        assert config.sonarqube_token == "custom-token"
        assert config.sonarqube_project_key == "custom-project"

    def test_custom_env_file_with_ai_tool(self, tmp_path: Path) -> None:
        """Test loading AI tool configuration from custom env file."""
        # Create custom env file with AI tool setting
        custom_env = tmp_path / ".env.custom"
        custom_env.write_text(
            "SONARQUBE_URL=https://custom.example.com\n"
            "SONARQUBE_TOKEN=custom-token\n"
            "SONARQUBE_PROJECT_KEY=custom-project\n"
            "AI_TOOL=aider\n"
        )

        config = VibeHealConfig(env_file=custom_env)

        assert config.ai_tool == AIToolType.AIDER
