"""SonarQube-specific exceptions."""


class SonarQubeError(Exception):
    """Base exception for SonarQube errors."""


class SonarQubeAuthError(SonarQubeError):
    """Authentication failed."""


class SonarQubeAPIError(SonarQubeError):
    """API request failed."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Initialize API error.

        Args:
            message: Error message
            status_code: HTTP status code if available
        """
        super().__init__(message)
        self.status_code = status_code


class ComponentNotFoundError(SonarQubeError):
    """Component not found in SonarQube project.

    This typically means the file was not included in the SonarQube analysis
    (e.g., documentation files, test files excluded from sources).
    """


class SonarQubeRuleNotFoundError(SonarQubeError):
    """Rule not found in SonarQube (external or unsupported rule)."""
