"""SonarQube duplications API client."""

from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.deduplication.models import DuplicationsResponse
from dotharness.vibe_heal.sonarqube.client import SonarQubeClient


class DuplicationClient:
    """Client for interacting with SonarQube duplications API.

    This extends the base SonarQubeClient with duplication-specific methods.
    """

    def __init__(self, config: VibeHealConfig) -> None:
        """Initialize the duplication client.

        Args:
            config: Application configuration
        """
        self.config = config
        self._sonarqube_client = SonarQubeClient(config)

    async def __aenter__(self) -> "DuplicationClient":
        """Async context manager entry.

        Returns:
            Self
        """
        await self._sonarqube_client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
        """Async context manager exit.

        Args:
            exc_type: Exception type
            exc_val: Exception value
            exc_tb: Exception traceback
        """
        await self._sonarqube_client.__aexit__(exc_type, exc_val, exc_tb)

    async def get_duplications_for_file(self, file_path: str) -> DuplicationsResponse:
        """Get all code duplications for a specific file.

        Args:
            file_path: Path to the file (relative to project root)

        Returns:
            Duplications response containing duplication groups and file information

        Raises:
            SonarQubeAuthError: Authentication failed
            SonarQubeAPIError: API request failed
        """
        # Build component identifier: projectKey:filePath
        # Note: We don't lowercase the project key here as duplications API
        # may handle it differently than issues API
        component = f"{self.config.sonarqube_project_key}:{file_path}"

        params = {
            "key": component,
        }

        # Use the underlying client's _request method
        data = await self._sonarqube_client._request("GET", "/api/duplications/show", params=params)

        return DuplicationsResponse(**data)
