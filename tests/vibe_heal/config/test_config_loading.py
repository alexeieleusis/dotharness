"""Tests for configuration loading from files."""

from pathlib import Path

import pytest

from dotharness.vibe_heal.config import VibeHealConfig


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

    def test_load_aider_from_env_vibeheal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading Aider config from .env.vibeheal file."""
        # Change to temp directory
        monkeypatch.chdir(tmp_path)

        # Create .env.vibeheal file with Aider config
        env_file = tmp_path / ".env.vibeheal"
        env_file.write_text(
            """
SONARQUBE_URL=https://sonar.example.com
SONARQUBE_TOKEN=test-token
SONARQUBE_PROJECT_KEY=test-project
AI_TOOL=aider
AIDER_MODEL=ollama_chat/gemma3:27b
AIDER_API_BASE=http://127.0.0.1:11434
"""
        )

        config = VibeHealConfig()

        assert config.sonarqube_url == "https://sonar.example.com"
        assert config.sonarqube_token == "test-token"  # noqa: S105
        assert config.sonarqube_project_key == "test-project"
        assert config.ai_tool is not None
        assert config.ai_tool.value == "aider"
        assert config.aider_model == "ollama_chat/gemma3:27b"
        assert config.aider_api_base == "http://127.0.0.1:11434"

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
