"""Configuration models."""

import json
from pathlib import Path
from typing import Annotated, Any, Self, cast

from pydantic import Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    InitSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from dotharness.vibe_heal.ai_tools.base import AIToolType
from dotharness.vibe_heal.config.exceptions import InvalidConfigurationError
from dotharness.vibe_heal.config.scanner_discovery import resolve_scanner_auth, resolve_scanner_host_url

REDACTED_VALUE = "<redacted>"


class CodeHostSettings(BaseSettings):
    """Code host settings and the env-file loading shared by every settings model."""

    # Code host settings (the env prefix is empty, so the aliases carry the full variable names)
    code_host: str | None = Field(
        default=None,
        validation_alias="DOTHARNESS_CODE_HOST",
        description="Code host name or 'module:attribute' path (the reference host when unset)",
    )
    code_host_options: Annotated[dict[str, Any], NoDecode] = Field(
        default_factory=dict,
        validation_alias="DOTHARNESS_CODE_HOST_OPTIONS",
        description="Code host options as a JSON object",
    )

    model_config = SettingsConfigDict(
        env_file=[".env.vibeheal", ".env"],
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    def __init__(
        self,
        _env_file: str | Path | None = None,
        _settings_customise_sources_was_called: bool = False,
        **kwargs: Any,
    ) -> None:
        """Initialize configuration.

        Args:
            _env_file: Optional path to custom env file (use env_file for public API)
            _settings_customise_sources_was_called: Internal flag
            **kwargs: Additional configuration values

        Raises:
            InvalidConfigurationError: If _env_file is specified but does not exist
        """
        # Support both env_file and _env_file for backwards compatibility
        env_file = kwargs.pop("env_file", _env_file)

        if env_file is not None:
            env_path = Path(env_file)
            if not env_path.exists():
                msg = f"Environment file not found: {env_file}"
                raise InvalidConfigurationError(msg)
            # Pass custom env file path through kwargs so settings_customise_sources can access it
            kwargs["_custom_env_file"] = env_path

        super().__init__(**kwargs)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Customize settings sources to support custom env file.

        This method is called by Pydantic to determine the order and sources
        of configuration values. We check if a custom env file was specified
        in init_settings and use it instead of the default dotenv settings.

        Args:
            settings_cls: The settings class being instantiated
            init_settings: Settings from __init__ arguments
            env_settings: Settings from environment variables
            dotenv_settings: Settings from .env files
            file_secret_settings: Settings from secret files

        Returns:
            Tuple of settings sources in priority order
        """
        # Try to get custom env file path from init settings
        # init_settings wraps the kwargs passed to __init__
        # Note: init_kwargs exists at runtime but may not be in type stubs
        init_kwargs = cast("InitSettingsSource", init_settings).init_kwargs
        custom_env_path = init_kwargs.get("_custom_env_file")

        if custom_env_path is not None:
            # Use custom env file instead of default dotenv settings
            custom_dotenv = DotEnvSettingsSource(
                settings_cls,
                env_file=custom_env_path,
                env_file_encoding="utf-8",
            )
            return (init_settings, custom_dotenv, env_settings, file_secret_settings)

        # Default behavior: use standard dotenv settings
        return (init_settings, env_settings, dotenv_settings, file_secret_settings)

    @field_validator("code_host_options", mode="before")
    @classmethod
    def parse_code_host_options(cls, v: Any) -> Any:
        """Parse the options JSON object from the environment variable.

        The message names the field and the error kind only. The raw value may hold a secret, so it is redacted.

        Raises:
            InvalidConfigurationError: If the value is not valid JSON or not a JSON object
        """
        if v is None or v == "":
            return {}
        if not isinstance(v, str):
            return v
        prefix = f'code host setting DOTHARNESS_CODE_HOST_OPTIONS = "{REDACTED_VALUE}": '
        try:
            parsed = json.loads(v)
        except ValueError:
            msg = prefix + "value is not valid JSON"
            raise InvalidConfigurationError(msg) from None
        if not isinstance(parsed, dict):
            msg = prefix + "value is not a JSON object"
            raise InvalidConfigurationError(msg) from None
        return parsed


class VibeHealConfig(CodeHostSettings):
    """Configuration for vibe_heal application."""

    # SonarQube settings
    sonarqube_url: str = Field(description="SonarQube server URL")
    sonarqube_token: str | None = Field(
        default=None,
        description="SonarQube authentication token (preferred)",
    )
    sonarqube_username: str | None = Field(
        default=None,
        description="SonarQube username (alternative to token)",
    )
    sonarqube_password: str | None = Field(
        default=None,
        description="SonarQube password (alternative to token)",
    )
    sonarqube_project_key: str = Field(description="SonarQube project key")

    # AI Tool settings
    ai_tool: AIToolType | None = Field(
        default=None,
        description="AI tool to use (auto-detect if not specified)",
    )

    # Context enrichment settings
    code_context_lines: int = Field(
        default=5,
        description="Number of lines to show before/after the issue line for context",
    )
    include_rule_description: bool = Field(
        default=True,
        description="Include full rule description in AI prompts",
    )

    # Scanner execution settings
    scanner_timeout_seconds: int = Field(
        default=300,
        description="Max seconds to wait for the sonar-scanner subprocess before killing it",
    )

    # Pre-commit hook settings
    pre_commit_command: str | None = Field(
        default=None,
        description=(
            "Command to run before committing (e.g. 'pre-commit run --files'). "
            "Set to empty string to disable. Auto-detects pre-commit if not set."
        ),
    )

    pre_commit_timeout_seconds: int = Field(
        default=300,
        description="Max seconds to wait for the pre-commit hook subprocess before killing it",
    )

    @field_validator("ai_tool", mode="before")
    @classmethod
    def parse_ai_tool(cls, v: str | AIToolType | None) -> AIToolType | None:
        """Parse AI tool from string or enum.

        Args:
            v: AI tool value (string, enum, or None)

        Returns:
            Parsed AIToolType or None

        Raises:
            InvalidConfigurationError: If AI tool value is invalid
        """
        if v is None:
            return None
        if isinstance(v, AIToolType):
            return v
        if isinstance(v, str):
            try:
                return AIToolType(v.lower())
            except ValueError as e:
                valid_tools = [t.value for t in AIToolType]
                msg = f"Invalid AI tool: {v}. Valid options: {valid_tools}"
                raise InvalidConfigurationError(msg) from e
        msg = f"Invalid AI tool type: {type(v)}"
        raise InvalidConfigurationError(msg)

    @field_validator("sonarqube_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Ensure URL doesn't have trailing slash.

        Args:
            v: URL value

        Returns:
            Normalized URL without trailing slash
        """
        return v.rstrip("/")

    @model_validator(mode="before")
    @classmethod
    def apply_scanner_url_fallback(cls, data: Any) -> Any:
        """Fall back to sonar-scanner's own host URL sources when unset.

        Mirrors `apply_scanner_auth_fallback`, but for the SonarQube server URL:
        only triggers when `sonarqube_url` wasn't already provided via vibe_heal's
        normal sources (init kwargs, env vars, .env file).

        Args:
            data: Merged settings values from all sources.

        Returns:
            Data, augmented with a discovered host URL if none was already configured.
        """
        if not isinstance(data, dict):
            return data
        if data.get("sonarqube_url") is not None:
            return data
        url = resolve_scanner_host_url(Path.cwd())
        if url is None:
            return data
        return {**data, "sonarqube_url": url}

    @model_validator(mode="before")
    @classmethod
    def apply_scanner_auth_fallback(cls, data: Any) -> Any:
        """Fall back to sonar-scanner's own credential sources when auth is unset.

        Only triggers when neither a token nor a full username+password pair was
        found via vibe_heal's normal sources (init kwargs, env vars, .env file).

        Args:
            data: Merged settings values from all sources.

        Returns:
            Data, augmented with discovered auth if none was already configured.
        """
        if not isinstance(data, dict):
            return data
        has_token = data.get("sonarqube_token") is not None
        has_basic = data.get("sonarqube_username") is not None and data.get("sonarqube_password") is not None
        if has_token or has_basic:
            return data
        fallback = resolve_scanner_auth(Path.cwd())
        if fallback is None:
            return data
        if "token" in fallback:
            return {**data, "sonarqube_token": fallback["token"]}
        return {**data, "sonarqube_username": fallback["login"], "sonarqube_password": fallback["password"]}

    @model_validator(mode="after")
    def validate_auth(self) -> Self:
        """Ensure either token or username/password is provided.

        Returns:
            Self

        Raises:
            InvalidConfigurationError: If neither auth method is provided
        """
        has_token = self.sonarqube_token is not None
        has_basic = self.sonarqube_username is not None and self.sonarqube_password is not None

        if not has_token and not has_basic:
            msg = "Must provide either SONARQUBE_TOKEN or both SONARQUBE_USERNAME and SONARQUBE_PASSWORD"
            raise InvalidConfigurationError(msg)

        return self

    @property
    def use_token_auth(self) -> bool:
        """Check if token authentication should be used.

        Returns:
            True if token auth is configured
        """
        return self.sonarqube_token is not None

    @staticmethod
    def find_env_file() -> Path | None:
        """Find the environment file being used.

        Checks for .env.vibeheal and .env in current directory in that order.

        Returns:
            Path to the env file if found, None otherwise
        """
        for env_file in [".env.vibeheal", ".env"]:
            path = Path(env_file)
            if path.exists():
                return path.absolute()
        return None
