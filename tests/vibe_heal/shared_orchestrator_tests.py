"""Shared test classes and fixtures for orchestrator tests."""

from typing import Any
from unittest.mock import patch

import pytest

from dotharness.vibe_heal.sonarqube.exceptions import SonarQubeAPIError
from dotharness.vibe_heal.sonarqube.project_manager import TempProjectMetadata

TEST_USER_EMAIL = "user@example.com"


@pytest.fixture
def temp_project() -> TempProjectMetadata:
    """Create a test temporary project metadata."""
    return TempProjectMetadata(
        project_key="test-project-user_example_com-feature",
        project_name=f"Test Project ({TEST_USER_EMAIL} - feature)",
        created_at="2024-01-01T00:00:00Z",
        base_project_key="test-project",
        branch_name="feature",
        user_email=TEST_USER_EMAIL,
    )


class BaseTestCreateTempProject:
    """Shared tests for _create_temp_project method across orchestrators.

    Subclass this in each orchestrator's test file. The `orchestrator` fixture
    from the subclass's test module is injected automatically.
    """

    @pytest.mark.asyncio
    async def test_create_temp_project_warns_on_settings_copy_failure(
        self,
        orchestrator: Any,
        temp_project: TempProjectMetadata,
    ) -> None:
        """Test that the orchestrator warns but does not re-raise when copying settings fails."""
        with (
            patch.object(
                orchestrator.branch_analyzer,
                "get_current_branch",
                return_value="feature-branch",
            ),
            patch.object(
                orchestrator.branch_analyzer,
                "get_user_email",
                return_value=TEST_USER_EMAIL,
            ),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project",
                return_value=temp_project,
            ),
            patch.object(
                orchestrator.project_manager,
                "copy_exclusion_settings",
                side_effect=SonarQubeAPIError("Permission denied", status_code=403),
            ),
        ):
            result = await orchestrator._create_temp_project()

        assert result == temp_project

    @pytest.mark.asyncio
    async def test_create_temp_project_copies_settings_successfully(
        self,
        orchestrator: Any,
        temp_project: TempProjectMetadata,
    ) -> None:
        """Test that settings are copied successfully after project creation."""
        with (
            patch.object(
                orchestrator.branch_analyzer,
                "get_current_branch",
                return_value="feature-branch",
            ),
            patch.object(
                orchestrator.branch_analyzer,
                "get_user_email",
                return_value=TEST_USER_EMAIL,
            ),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project",
                return_value=temp_project,
            ),
            patch.object(
                orchestrator.project_manager,
                "copy_exclusion_settings",
                return_value=(["sonar.cpd.exclusions"], 0, 0),
            ) as mock_copy,
        ):
            result = await orchestrator._create_temp_project()

        assert result == temp_project
        mock_copy.assert_called_once_with(
            source_key=orchestrator.config.sonarqube_project_key,
            target_key=temp_project.project_key,
        )
