"""Configuration-related exceptions."""


class ConfigurationError(Exception):
    """Base exception for configuration errors."""


class MissingConfigurationError(ConfigurationError):
    """Required configuration is missing."""


class InvalidConfigurationError(ConfigurationError):
    """Configuration value is invalid."""
