"""Tests for CleanupOrchestrator class."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dotharness.vibe_heal.ai_tools.base import AITool
from dotharness.vibe_heal.cleanup.orchestrator import (
    CleanupOrchestrator,
)
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.git.branch_analyzer import BranchAnalyzer
from dotharness.vibe_heal.git.manager import GitManager
from dotharness.vibe_heal.sonarqube.analysis_runner import AnalysisResult, AnalysisRunner
from dotharness.vibe_heal.sonarqube.client import SonarQubeClient
from dotharness.vibe_heal.sonarqube.project_manager import ProjectManager, TempProjectMetadata
from tests.vibe_heal.shared_orchestrator_tests import BaseTestCreateTempProject


@pytest.fixture
def config() -> VibeHealConfig:
    """Create test configuration."""
    return VibeHealConfig(
        sonarqube_url="https://sonar.test.com",
        sonarqube_token="test-token",  # noqa: S106
        sonarqube_project_key="my-project",
    )


@pytest.fixture
def mock_client() -> AsyncMock:
    """Create a mock SonarQubeClient."""
    return AsyncMock(spec=SonarQubeClient)


@pytest.fixture
def mock_ai_tool() -> MagicMock:
    """Create a mock AITool."""
    return MagicMock(spec=AITool)


@pytest.fixture
def orchestrator(
    config: VibeHealConfig,
    mock_client: AsyncMock,
    mock_ai_tool: MagicMock,
) -> CleanupOrchestrator:
    """Create CleanupOrchestrator with mocked dependencies."""
    return CleanupOrchestrator(config, mock_client, mock_ai_tool)


class TestCleanupOrchestratorInit:
    """Tests for CleanupOrchestrator initialization."""

    def test_init(
        self,
        config: VibeHealConfig,
        mock_client: AsyncMock,
        mock_ai_tool: MagicMock,
    ) -> None:
        """Test CleanupOrchestrator initialization."""
        orchestrator = CleanupOrchestrator(config, mock_client, mock_ai_tool)

        assert orchestrator.config == config
        assert orchestrator.client == mock_client
        assert orchestrator.ai_tool == mock_ai_tool
        assert isinstance(orchestrator.project_manager, ProjectManager)
        assert isinstance(orchestrator.analysis_runner, AnalysisRunner)
        assert isinstance(orchestrator.branch_analyzer, BranchAnalyzer)
        assert isinstance(orchestrator.git_manager, GitManager)


class TestCleanupBranch:
    """Tests for cleanup_branch method."""

    @pytest.mark.asyncio
    async def test_no_modified_files(self, orchestrator: CleanupOrchestrator) -> None:
        """Test when branch has no modified files."""
        with patch.object(
            orchestrator.branch_analyzer,
            "get_modified_files",
            return_value=[],
        ):
            result = await orchestrator.cleanup_branch()

        assert result.success is True
        assert result.files_processed == []
        assert result.total_issues_fixed == 0
        assert result.temp_project is None

    @pytest.mark.asyncio
    async def test_no_files_after_filtering(self, orchestrator: CleanupOrchestrator) -> None:
        """Test when file patterns filter out all files."""
        with patch.object(
            orchestrator.branch_analyzer,
            "get_modified_files",
            return_value=[Path("test.txt"), Path("data.json")],
        ):
            result = await orchestrator.cleanup_branch(file_patterns=["*.py"])

        assert result.success is True
        assert result.files_processed == []
        assert result.total_issues_fixed == 0
        assert result.temp_project is None

    @pytest.mark.asyncio
    async def test_initial_analysis_fails(
        self,
        orchestrator: CleanupOrchestrator,
        temp_project: TempProjectMetadata,
    ) -> None:
        """Test when initial SonarQube analysis fails."""
        with (
            patch.object(
                orchestrator.branch_analyzer,
                "get_modified_files",
                return_value=[Path("src/file1.py")],
            ),
            patch.object(
                orchestrator.branch_analyzer,
                "get_current_branch",
                return_value="feature-branch",
            ),
            patch.object(
                orchestrator.branch_analyzer,
                "get_user_email",
                return_value="user@example.com",
            ),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project",
                return_value=temp_project,
            ),
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                return_value=AnalysisResult(
                    success=False,
                    error_message="Analysis failed",
                ),
            ),
            patch.object(
                orchestrator.project_manager,
                "delete_project",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            result = await orchestrator.cleanup_branch()

        assert result.success is False
        assert "Analysis failed at iteration 1" in (result.error_message or "")
        assert result.temp_project == temp_project
        mock_delete.assert_called_once_with(temp_project.project_key)

    @pytest.mark.asyncio
    async def test_cleanup_exception_handling(
        self,
        orchestrator: CleanupOrchestrator,
    ) -> None:
        """Test that exceptions are handled gracefully."""
        with (
            patch.object(
                orchestrator.branch_analyzer,
                "get_modified_files",
                side_effect=Exception("Git error"),
            ),
        ):
            result = await orchestrator.cleanup_branch()

        assert result.success is False
        assert "Cleanup failed: Git error" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_project_cleanup_on_exception(
        self,
        orchestrator: CleanupOrchestrator,
        temp_project: TempProjectMetadata,
    ) -> None:
        """Test that temporary project is deleted even on exception."""
        with (
            patch.object(
                orchestrator.branch_analyzer,
                "get_modified_files",
                return_value=[Path("src/file1.py")],
            ),
            patch.object(
                orchestrator.branch_analyzer,
                "get_current_branch",
                return_value="feature-branch",
            ),
            patch.object(
                orchestrator.branch_analyzer,
                "get_user_email",
                return_value="user@example.com",
            ),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project",
                return_value=temp_project,
            ),
            patch.object(
                orchestrator.analysis_runner,
                "run_analysis",
                side_effect=Exception("Analysis error"),
            ),
            patch.object(
                orchestrator.project_manager,
                "delete_project",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            result = await orchestrator.cleanup_branch()

        assert result.success is False
        # Project should still be deleted
        mock_delete.assert_called_once_with(temp_project.project_key)


# Note: _cleanup_file method was removed in favor of integrated loop in cleanup_branch
# Tests for the new integrated workflow are in TestCleanupBranch


# Note: _filter_files was extracted to vibe_heal.git.file_selection.filter_files_by_patterns;
# its tests live in tests/git/test_file_selection.py.


class TestCreateTempProject(BaseTestCreateTempProject):
    """Tests for _create_temp_project method."""

    pass
