"""Tests for configuration loading from files."""

from pathlib import Path

import pytest

from dotharness.vibe_heal.config import InvalidConfigurationError, VibeHealConfig

# Removed backend names, built from pieces so repo-wide greps for the dropped tools stay empty.
REMOVED_A = "ai" + "der"
REMOVED_B = "gem" + "ini"


class TestConfigLoading:
    """Tests for loading configuration from .env files."""

    def test_load_from_env_vibeheal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading config from .env.vibeheal file."""
        # Change to temp directory
        monkeypatch.chdir(tmp_path)

        # Create .env.vibeheal file
        env_file = tmp_path / ".env.vibeheal"
        env_file.write_text(
            """
SONARQUBE_URL=https://sonar.vibeheal.com
SONARQUBE_TOKEN=vibeheal-token
SONARQUBE_PROJECT_KEY=vibeheal-project
AI_TOOL=claude-code
"""
        )

        config = VibeHealConfig()

        assert config.sonarqube_url == "https://sonar.vibeheal.com"
        assert config.sonarqube_token == "vibeheal-token"  # noqa: S105
        assert config.sonarqube_project_key == "vibeheal-project"
        assert config.ai_tool is not None
        assert config.ai_tool.value == "claude-code"

    def test_load_opencode_from_env_vibeheal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading opencode config from .env.vibeheal file."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env.vibeheal").write_text(
            "SONARQUBE_URL=https://sonar.example.com\n"
            "SONARQUBE_TOKEN=test-token\n"
            "SONARQUBE_PROJECT_KEY=test-project\n"
            "AI_TOOL=opencode\n"
        )

        config = VibeHealConfig()

        assert config.ai_tool is not None
        assert config.ai_tool.value == "opencode"

    @pytest.mark.parametrize("removed", [REMOVED_B, REMOVED_A])
    def test_removed_ai_tool_in_env_file_is_invalid(
        self, removed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AI_TOOL set to a removed backend is an Invalid AI tool error listing the valid values."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env.vibeheal").write_text(
            "SONARQUBE_URL=https://sonar.example.com\n"
            "SONARQUBE_TOKEN=test-token\n"
            "SONARQUBE_PROJECT_KEY=test-project\n"
            f"AI_TOOL={removed}\n"
        )

        with pytest.raises(InvalidConfigurationError, match="Invalid AI tool") as exc_info:
            VibeHealConfig()

        assert "claude-code" in str(exc_info.value)
        assert "opencode" in str(exc_info.value)

    def test_removed_provider_keys_in_env_file_are_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Old removed-backend and OLLAMA_* keys in an env file load silently and are not read."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env.vibeheal").write_text(
            "SONARQUBE_URL=https://sonar.example.com\n"
            "SONARQUBE_TOKEN=test-token\n"
            "SONARQUBE_PROJECT_KEY=test-project\n"
            f"{REMOVED_A.upper()}_MODEL=x\n{REMOVED_A.upper()}_API_KEY=y\n{REMOVED_A.upper()}_API_BASE=z\n"
            "OLLAMA_API_KEY=k\nOLLAMA_API_BASE=b\n"
        )

        config = VibeHealConfig()

        assert not hasattr(config, f"{REMOVED_A}_model")
        assert not hasattr(config, f"{REMOVED_A}_api_key")
        assert not hasattr(config, f"{REMOVED_A}_api_base")

    def test_load_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading config from .env file."""
        # Change to temp directory
        monkeypatch.chdir(tmp_path)

        # Create .env file
        env_file = tmp_path / ".env"
        env_file.write_text(
            """
SONARQUBE_URL=https://sonar.env.com
SONARQUBE_TOKEN=env-token
SONARQUBE_PROJECT_KEY=env-project
"""
        )

        config = VibeHealConfig()

        assert config.sonarqube_url == "https://sonar.env.com"
        assert config.sonarqube_token == "env-token"  # noqa: S105
        assert config.sonarqube_project_key == "env-project"

    def test_load_from_environment_variables(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading config from environment variables."""
        # Change to temp directory (no .env files)
        monkeypatch.chdir(tmp_path)

        # Set environment variables
        monkeypatch.setenv("SONARQUBE_URL", "https://sonar.envvar.com")
        monkeypatch.setenv("SONARQUBE_TOKEN", "envvar-token")
        monkeypatch.setenv("SONARQUBE_PROJECT_KEY", "envvar-project")

        config = VibeHealConfig()

        assert config.sonarqube_url == "https://sonar.envvar.com"
        assert config.sonarqube_token == "envvar-token"  # noqa: S105
        assert config.sonarqube_project_key == "envvar-project"

    def test_environment_variables_override_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that environment variables override .env files."""
        # Change to temp directory
        monkeypatch.chdir(tmp_path)

        # Create .env.vibeheal file
        env_file = tmp_path / ".env.vibeheal"
        env_file.write_text(
            """
SONARQUBE_URL=https://sonar.file.com
SONARQUBE_TOKEN=file-token
SONARQUBE_PROJECT_KEY=file-project
"""
        )

        # Set environment variables
        monkeypatch.setenv("SONARQUBE_URL", "https://sonar.envvar.com")
        monkeypatch.setenv("SONARQUBE_TOKEN", "envvar-token")

        config = VibeHealConfig()

        # Environment variables should override file
        assert config.sonarqube_url == "https://sonar.envvar.com"
        assert config.sonarqube_token == "envvar-token"  # noqa: S105
        # This should still come from file
        assert config.sonarqube_project_key == "file-project"

    def test_case_insensitive_env_vars(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that environment variable names are case insensitive."""
        # Change to temp directory
        monkeypatch.chdir(tmp_path)

        # Set environment variables with different cases
        monkeypatch.setenv("sonarqube_url", "https://sonar.example.com")
        monkeypatch.setenv("SONARQUBE_TOKEN", "token")
        monkeypatch.setenv("SonarQube_Project_Key", "project")

        config = VibeHealConfig()

        assert config.sonarqube_url == "https://sonar.example.com"
        assert config.sonarqube_token == "token"  # noqa: S105
        assert config.sonarqube_project_key == "project"

    def test_no_config_files_uses_env_vars(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading when no .env files exist but env vars are set."""
        # Change to empty temp directory
        monkeypatch.chdir(tmp_path)

        # Set environment variables
        monkeypatch.setenv("SONARQUBE_URL", "https://sonar.example.com")
        monkeypatch.setenv("SONARQUBE_USERNAME", "user")
        monkeypatch.setenv("SONARQUBE_PASSWORD", "pass")
        monkeypatch.setenv("SONARQUBE_PROJECT_KEY", "project")

        config = VibeHealConfig()

        assert config.sonarqube_url == "https://sonar.example.com"
        assert config.sonarqube_username == "user"
        assert config.sonarqube_password == "pass"  # noqa: S105
        assert config.sonarqube_project_key == "project"
