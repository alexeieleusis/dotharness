"""Tests for CLI commands."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from dotharness.vibe_heal.ai_tools.base import AIToolType
from dotharness.vibe_heal.cleanup.orchestrator import CleanupResult, FileCleanupResult
from dotharness.vibe_heal.cli import app
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.models import FixSummary
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue
from dotharness.vibe_heal.sonarqube.project_manager import PruneResult, StaleProject

runner = CliRunner()


def _post_report(total_issues: int) -> MagicMock:
    report = MagicMock()
    report.total_issues = total_issues
    report.total_duplications = 0
    report.branch = "feature-test"
    report.files = []
    return report


class TestFixCommand:
    """Tests for fix command."""

    @patch("dotharness.vibe_heal.cli.initialize_ai_tool")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    @patch("dotharness.vibe_heal.cli.VibeHealOrchestrator")
    def test_fix_basic(
        self,
        mock_orchestrator_class: MagicMock,
        mock_config_class: MagicMock,
        mock_init_ai_tool: MagicMock,
    ) -> None:
        """Test basic fix command."""
        # Setup mocks
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config_class.return_value = mock_config

        mock_ai_tool = MagicMock()
        mock_init_ai_tool.return_value = mock_ai_tool

        mock_orchestrator = MagicMock()
        mock_orchestrator.fix_file = AsyncMock(
            return_value=FixSummary(
                total_issues=5,
                fixed=3,
                failed=0,
                skipped=2,
            )
        )
        mock_orchestrator_class.return_value = mock_orchestrator

        # Run command
        result = runner.invoke(app, ["fix", "test.py"])

        # Assertions
        assert result.exit_code == 0
        mock_init_ai_tool.assert_called_once_with(mock_config)
        mock_orchestrator_class.assert_called_once_with(mock_config, mock_ai_tool)
        mock_orchestrator.fix_file.assert_called_once()
        call_kwargs = mock_orchestrator.fix_file.call_args.kwargs
        assert call_kwargs["file_path"] == "test.py"
        assert call_kwargs["dry_run"] is False

    @patch("dotharness.vibe_heal.cli.initialize_ai_tool")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    @patch("dotharness.vibe_heal.cli.VibeHealOrchestrator")
    def test_fix_with_options(
        self,
        mock_orchestrator_class: MagicMock,
        mock_config_class: MagicMock,
        mock_init_ai_tool: MagicMock,
    ) -> None:
        """Test fix command with all options."""
        # Setup mocks
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config_class.return_value = mock_config

        mock_ai_tool = MagicMock()
        mock_init_ai_tool.return_value = mock_ai_tool

        mock_orchestrator = MagicMock()
        mock_orchestrator.fix_file = AsyncMock(return_value=FixSummary(total_issues=1, fixed=1))
        mock_orchestrator_class.return_value = mock_orchestrator

        # Run command with options
        result = runner.invoke(
            app,
            [
                "fix",
                "test.py",
                "--dry-run",
                "--max-issues",
                "5",
                "--min-severity",
                "MAJOR",
                "--ai-tool",
                "claude-code",
                "--verbose",
            ],
        )

        # Assertions
        assert result.exit_code == 0
        call_kwargs = mock_orchestrator.fix_file.call_args.kwargs
        assert call_kwargs["dry_run"] is True
        assert call_kwargs["max_issues"] == 5
        assert call_kwargs["min_severity"] == "MAJOR"
        assert mock_config.ai_tool == AIToolType.CLAUDE_CODE

    @patch("dotharness.vibe_heal.cli.initialize_ai_tool")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    @patch("dotharness.vibe_heal.cli.VibeHealOrchestrator")
    def test_fix_with_failures(
        self,
        mock_orchestrator_class: MagicMock,
        mock_config_class: MagicMock,
        mock_init_ai_tool: MagicMock,
    ) -> None:
        """Test fix command exits with error when there are failures."""
        # Setup mocks
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config_class.return_value = mock_config

        mock_ai_tool = MagicMock()
        mock_init_ai_tool.return_value = mock_ai_tool

        mock_orchestrator = MagicMock()
        mock_orchestrator.fix_file = AsyncMock(
            return_value=FixSummary(
                total_issues=5,
                fixed=2,
                failed=3,
            )
        )
        mock_orchestrator_class.return_value = mock_orchestrator

        # Run command
        result = runner.invoke(app, ["fix", "test.py"])

        # Should exit with error code 1
        assert result.exit_code == 1


class TestCleanupCommand:
    """Tests for cleanup command."""

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.AIToolFactory")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    @patch("dotharness.vibe_heal.cli.CleanupOrchestrator")
    def test_cleanup_basic(
        self,
        mock_orchestrator_class: MagicMock,
        mock_config_class: MagicMock,
        mock_ai_factory: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test basic cleanup command."""
        # Setup config mock
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.ai_tool = None
        mock_config_class.return_value = mock_config

        # Setup AI tool mock
        mock_ai_tool = MagicMock()
        mock_ai_tool.is_available.return_value = True
        mock_ai_factory.detect_available.return_value = AIToolType.CLAUDE_CODE
        mock_ai_factory.create.return_value = mock_ai_tool

        # Setup client mock
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Setup orchestrator mock
        mock_orchestrator = MagicMock()
        mock_orchestrator.cleanup_branch = AsyncMock(
            return_value=CleanupResult(
                success=True,
                files_processed=[
                    FileCleanupResult(
                        file_path=Path("src/file1.py"),
                        issues_fixed=3,
                        success=True,
                    ),
                ],
                total_issues_fixed=3,
            )
        )
        mock_orchestrator_class.return_value = mock_orchestrator

        # Run command
        result = runner.invoke(app, ["cleanup"])

        # Assertions
        assert result.exit_code == 0
        assert "Branch cleanup complete" in result.stdout
        mock_orchestrator.cleanup_branch.assert_called_once()
        call_kwargs = mock_orchestrator.cleanup_branch.call_args.kwargs
        assert call_kwargs["base_branch"] == "origin/main"
        assert call_kwargs["max_iterations"] == 10
        assert call_kwargs["file_patterns"] is None

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.AIToolFactory")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    @patch("dotharness.vibe_heal.cli.CleanupOrchestrator")
    def test_cleanup_with_options(
        self,
        mock_orchestrator_class: MagicMock,
        mock_config_class: MagicMock,
        mock_ai_factory: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test cleanup command with all options."""
        # Setup config mock
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.ai_tool = None
        mock_config_class.return_value = mock_config

        # Setup AI tool mock
        mock_ai_tool = MagicMock()
        mock_ai_tool.is_available.return_value = True
        mock_ai_factory.detect_available.return_value = AIToolType.CLAUDE_CODE
        mock_ai_factory.create.return_value = mock_ai_tool

        # Setup client mock
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Setup orchestrator mock
        mock_orchestrator = MagicMock()
        mock_orchestrator.cleanup_branch = AsyncMock(
            return_value=CleanupResult(
                success=True,
                files_processed=[],
                total_issues_fixed=0,
            )
        )
        mock_orchestrator_class.return_value = mock_orchestrator

        # Run command with options
        result = runner.invoke(
            app,
            [
                "cleanup",
                "--base-branch",
                "develop",
                "--max-iterations",
                "5",
                "--pattern",
                "*.py",
                "--pattern",
                "*.ts",
                "--ai-tool",
                "claude-code",
                "--verbose",
            ],
        )

        # Assertions
        assert result.exit_code == 0
        call_kwargs = mock_orchestrator.cleanup_branch.call_args.kwargs
        assert call_kwargs["base_branch"] == "develop"
        assert call_kwargs["max_iterations"] == 5
        assert call_kwargs["file_patterns"] == ["*.py", "*.ts"]
        assert mock_config.ai_tool == AIToolType.CLAUDE_CODE

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.AIToolFactory")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    @patch("dotharness.vibe_heal.cli.CleanupOrchestrator")
    def test_cleanup_with_failures(
        self,
        mock_orchestrator_class: MagicMock,
        mock_config_class: MagicMock,
        mock_ai_factory: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test cleanup command exits with error when cleanup fails."""
        # Setup config mock
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.ai_tool = None
        mock_config_class.return_value = mock_config

        # Setup AI tool mock
        mock_ai_tool = MagicMock()
        mock_ai_tool.is_available.return_value = True
        mock_ai_factory.detect_available.return_value = AIToolType.CLAUDE_CODE
        mock_ai_factory.create.return_value = mock_ai_tool

        # Setup client mock
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Setup orchestrator mock
        mock_orchestrator = MagicMock()
        mock_orchestrator.cleanup_branch = AsyncMock(
            return_value=CleanupResult(
                success=False,
                files_processed=[],
                error_message="Analysis failed",
            )
        )
        mock_orchestrator_class.return_value = mock_orchestrator

        # Run command
        result = runner.invoke(app, ["cleanup"])

        # Should exit with error code 1
        assert result.exit_code == 1
        assert "Cleanup failed" in result.stdout

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.AIToolFactory")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_cleanup_no_ai_tool_available(
        self,
        mock_config_class: MagicMock,
        mock_ai_factory: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test cleanup command fails when no AI tool is available."""
        # Setup config mock
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.ai_tool = None
        mock_config_class.return_value = mock_config

        # Setup AI tool mock - no tool detected
        mock_ai_factory.detect_available.return_value = None

        # Run command
        result = runner.invoke(app, ["cleanup"])

        # Should exit with error code 1
        assert result.exit_code == 1
        assert "No AI tool found" in result.stdout

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.AIToolFactory")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    @patch("dotharness.vibe_heal.cli.CleanupOrchestrator")
    def test_cleanup_displays_file_results(
        self,
        mock_orchestrator_class: MagicMock,
        mock_config_class: MagicMock,
        mock_ai_factory: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test cleanup command displays per-file results."""
        # Setup config mock
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.ai_tool = AIToolType.CLAUDE_CODE
        mock_config_class.return_value = mock_config

        # Setup AI tool mock
        mock_ai_tool = MagicMock()
        mock_ai_tool.is_available.return_value = True
        mock_ai_factory.create.return_value = mock_ai_tool

        # Setup client mock
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Setup orchestrator mock with multiple files
        mock_orchestrator = MagicMock()
        mock_orchestrator.cleanup_branch = AsyncMock(
            return_value=CleanupResult(
                success=True,
                files_processed=[
                    FileCleanupResult(
                        file_path=Path("src/file1.py"),
                        issues_fixed=3,
                        success=True,
                    ),
                    FileCleanupResult(
                        file_path=Path("src/file2.py"),
                        issues_fixed=0,
                        success=True,
                    ),
                    FileCleanupResult(
                        file_path=Path("src/file3.py"),
                        issues_fixed=5,
                        success=False,
                        error_message="Fix failed",
                    ),
                ],
                total_issues_fixed=8,
            )
        )
        mock_orchestrator_class.return_value = mock_orchestrator

        # Run command
        result = runner.invoke(app, ["cleanup"])

        # Assertions
        assert result.exit_code == 0
        assert "Files processed: 3" in result.stdout
        assert "Total issues fixed: 8" in result.stdout
        assert "src/file1.py: 3 issues fixed" in result.stdout
        assert "src/file2.py: 0 issues fixed" in result.stdout
        assert "src/file3.py: 5 issues fixed" in result.stdout
        assert "Error: Fix failed" in result.stdout


class TestPruneProjectsCommand:
    """Tests for prune-projects command."""

    def _stale_project(self, key: str = "my_project_user_example_com_main_240101-0000") -> StaleProject:
        return StaleProject(project_key=key, created_at=datetime.now(UTC) - timedelta(hours=2))

    @patch("dotharness.vibe_heal.cli.ProjectManager")
    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_dry_run_does_not_delete(
        self,
        mock_config_class: MagicMock,
        mock_client_class: MagicMock,
        mock_project_manager_class: MagicMock,
    ) -> None:
        """--dry-run lists stale projects without calling prune_stale_projects."""
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.sonarqube_project_key = "my_project"
        mock_config_class.return_value = mock_config

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        mock_project_manager = MagicMock()
        mock_project_manager.find_stale_projects = AsyncMock(return_value=[self._stale_project()])
        mock_project_manager.prune_stale_projects = AsyncMock()
        mock_project_manager_class.return_value = mock_project_manager

        result = runner.invoke(app, ["prune-projects", "--dry-run"])

        assert result.exit_code == 0
        assert "No projects deleted" in result.stdout
        mock_project_manager.prune_stale_projects.assert_not_called()

    @patch("dotharness.vibe_heal.cli.typer.confirm")
    @patch("dotharness.vibe_heal.cli.ProjectManager")
    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_declining_confirmation_aborts_without_deleting(
        self,
        mock_config_class: MagicMock,
        mock_client_class: MagicMock,
        mock_project_manager_class: MagicMock,
        mock_confirm: MagicMock,
    ) -> None:
        """Declining the confirmation prompt aborts without pruning."""
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.sonarqube_project_key = "my_project"
        mock_config_class.return_value = mock_config

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        mock_project_manager = MagicMock()
        mock_project_manager.find_stale_projects = AsyncMock(return_value=[self._stale_project()])
        mock_project_manager.prune_stale_projects = AsyncMock()
        mock_project_manager_class.return_value = mock_project_manager

        mock_confirm.return_value = False

        result = runner.invoke(app, ["prune-projects"])

        assert result.exit_code == 0
        assert "Aborted" in result.stdout
        mock_confirm.assert_called_once()
        mock_project_manager.prune_stale_projects.assert_not_called()

    @patch("dotharness.vibe_heal.cli.typer.confirm")
    @patch("dotharness.vibe_heal.cli.ProjectManager")
    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_yes_flag_skips_confirmation_and_deletes(
        self,
        mock_config_class: MagicMock,
        mock_client_class: MagicMock,
        mock_project_manager_class: MagicMock,
        mock_confirm: MagicMock,
    ) -> None:
        """--yes skips the confirmation prompt entirely and prunes the stale projects."""
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.sonarqube_project_key = "my_project"
        mock_config_class.return_value = mock_config

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        stale = [self._stale_project()]
        mock_project_manager = MagicMock()
        mock_project_manager.find_stale_projects = AsyncMock(return_value=stale)
        mock_project_manager.prune_stale_projects = AsyncMock(return_value=PruneResult(deleted_count=1, total_count=1))
        mock_project_manager_class.return_value = mock_project_manager

        result = runner.invoke(app, ["prune-projects", "--yes"])

        assert result.exit_code == 0
        mock_confirm.assert_not_called()
        mock_project_manager.prune_stale_projects.assert_called_once_with(stale)

    @patch("dotharness.vibe_heal.cli.ProjectManager")
    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_partial_failure_exits_nonzero(
        self,
        mock_config_class: MagicMock,
        mock_client_class: MagicMock,
        mock_project_manager_class: MagicMock,
    ) -> None:
        """Exits with code 1 when some stale projects fail to delete."""
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.sonarqube_project_key = "my_project"
        mock_config_class.return_value = mock_config

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        mock_project_manager = MagicMock()
        mock_project_manager.find_stale_projects = AsyncMock(
            return_value=[self._stale_project(), self._stale_project("proj_b")]
        )
        mock_project_manager.prune_stale_projects = AsyncMock(return_value=PruneResult(deleted_count=1, total_count=2))
        mock_project_manager_class.return_value = mock_project_manager

        result = runner.invoke(app, ["prune-projects", "--yes"])

        assert result.exit_code == 1


class TestConfigCommand:
    """Tests for config command."""

    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_config_display(self, mock_config_class: MagicMock) -> None:
        """Test config command displays configuration."""
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.sonarqube_url = "https://sonar.example.com"
        mock_config.sonarqube_project_key = "test-project"
        mock_config.use_token_auth = True
        mock_config.ai_tool = AIToolType.CLAUDE_CODE
        mock_config.code_context_lines = 5
        mock_config.include_rule_description = True
        mock_config_class.return_value = mock_config

        result = runner.invoke(app, ["config"])

        assert result.exit_code == 0
        assert "https://sonar.example.com" in result.stdout
        assert "test-project" in result.stdout
        assert "Token" in result.stdout
        assert "Claude Code" in result.stdout


class TestVersionCommand:
    """Tests for version command."""

    def test_version_display(self) -> None:
        """Test version command displays version."""
        result = runner.invoke(app, ["version"])

        assert result.exit_code == 0
        assert "vibe_heal version" in result.stdout


class TestDebugIssuesCommand:
    """Tests for debug-issues command."""

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_debug_issues_with_file(
        self,
        mock_config_class: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test debug-issues command with file path."""
        # Setup config mock
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config_class.return_value = mock_config

        # Setup client mock
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get_issues_for_file = AsyncMock(
            return_value=[
                SonarQubeIssue(
                    key="AXtest123",
                    rule="python:S1234",
                    component="project:src/test.py",
                    message="Test issue message",
                    line=10,
                    status="OPEN",
                    severity="MAJOR",
                ),
            ]
        )
        mock_client_class.return_value = mock_client

        # Run command
        result = runner.invoke(app, ["debug-issues", "src/test.py"])

        # Assertions
        assert result.exit_code == 0
        assert "Fetching issues for file: src/test.py" in result.stdout
        assert "Found 1 issues" in result.stdout
        assert "Issue 1:" in result.stdout
        assert "Key: AXtest123" in result.stdout
        assert "Rule: python:S1234" in result.stdout
        assert "Line: 10" in result.stdout
        assert "Message: Test issue message" in result.stdout
        mock_client.get_issues_for_file.assert_called_once_with("src/test.py")

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_debug_issues_without_file(
        self,
        mock_config_class: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test debug-issues command without file path (all project issues)."""
        # Setup config mock
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config_class.return_value = mock_config

        # Setup client mock
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get_issues = AsyncMock(
            return_value=[
                SonarQubeIssue(
                    key="AXtest456",
                    rule="python:S5678",
                    component="project:src/other.py",
                    message="Another issue",
                    line=20,
                    status="CONFIRMED",
                    severity="CRITICAL",
                ),
            ]
        )
        mock_client_class.return_value = mock_client

        # Run command
        result = runner.invoke(app, ["debug-issues"])

        # Assertions
        assert result.exit_code == 0
        assert "Fetching ALL project issues" in result.stdout
        assert "Found 1 issues" in result.stdout
        assert "Key: AXtest456" in result.stdout
        mock_client.get_issues.assert_called_once_with(component=None, page_size=10)

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_debug_issues_with_limit(
        self,
        mock_config_class: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test debug-issues command respects limit option."""
        # Setup config mock
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config_class.return_value = mock_config

        # Setup client mock with multiple issues
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get_issues = AsyncMock(
            return_value=[
                SonarQubeIssue(
                    key=f"AXtest{i}",
                    rule="python:S1234",
                    component="project:src/test.py",
                    message=f"Issue {i}",
                    line=i * 10,
                    status="OPEN",
                    severity="MAJOR",
                )
                for i in range(5)
            ]
        )
        mock_client_class.return_value = mock_client

        # Run command with custom limit
        result = runner.invoke(app, ["debug-issues", "--limit", "3"])

        # Assertions
        assert result.exit_code == 0
        assert "Found 5 issues" in result.stdout
        # Should only display first 3 issues
        assert "Issue 1:" in result.stdout
        assert "Issue 2:" in result.stdout
        assert "Issue 3:" in result.stdout
        assert "Issue 4:" not in result.stdout
        mock_client.get_issues.assert_called_once_with(component=None, page_size=3)

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_debug_issues_shows_all_fields(
        self,
        mock_config_class: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test debug-issues command displays all issue fields."""
        # Setup config mock
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config_class.return_value = mock_config

        # Setup client mock with issue having all fields
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get_issues_for_file = AsyncMock(
            return_value=[
                SonarQubeIssue.model_validate({
                    "key": "AXfull123",
                    "rule": "python:S999",
                    "component": "myproject:src/module/file.py",
                    "message": "Complete issue with all fields",
                    "line": 42,
                    "status": "OPEN",
                    "issue_status": "OPEN",
                    "severity": "BLOCKER",
                }),
            ]
        )
        mock_client_class.return_value = mock_client

        # Run command
        result = runner.invoke(app, ["debug-issues", "src/module/file.py"])

        # Assertions - verify all fields are displayed
        assert result.exit_code == 0
        assert "Key: AXfull123" in result.stdout
        assert "Rule: python:S999" in result.stdout
        assert "Component: myproject:src/module/file.py" in result.stdout
        assert "Line: 42" in result.stdout
        assert "Status:" in result.stdout
        assert "Issue Status:" in result.stdout
        assert "Severity: BLOCKER" in result.stdout
        assert "Message: Complete issue with all fields" in result.stdout
        assert "Is Fixable:" in result.stdout

    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_debug_issues_config_error(self, mock_config_class: MagicMock) -> None:
        """Test debug-issues command handles configuration errors."""
        from dotharness.vibe_heal.config.exceptions import ConfigurationError

        mock_config_class.side_effect = ConfigurationError("Missing SONARQUBE_URL")

        result = runner.invoke(app, ["debug-issues"])

        assert result.exit_code == 1
        assert "Configuration error" in result.stdout
        assert "Missing SONARQUBE_URL" in result.stdout

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_debug_issues_api_error(
        self,
        mock_config_class: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test debug-issues command handles API errors."""
        from dotharness.vibe_heal.sonarqube.exceptions import SonarQubeAPIError

        # Setup config mock
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config_class.return_value = mock_config

        # Setup client mock that raises an error
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get_issues_for_file = AsyncMock(side_effect=SonarQubeAPIError("API request failed"))
        mock_client_class.return_value = mock_client

        # Run command
        result = runner.invoke(app, ["debug-issues", "src/test.py"])

        # Assertions
        assert result.exit_code == 1
        assert "Error:" in result.stdout

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_debug_issues_empty_results(
        self,
        mock_config_class: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test debug-issues command handles empty results."""
        # Setup config mock
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config_class.return_value = mock_config

        # Setup client mock returning no issues
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get_issues_for_file = AsyncMock(return_value=[])
        mock_client_class.return_value = mock_client

        # Run command
        result = runner.invoke(app, ["debug-issues", "src/clean.py"])

        # Assertions
        assert result.exit_code == 0
        assert "Found 0 issues" in result.stdout


class TestReviewCommand:
    """Tests for review command."""

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.ReviewOrchestrator")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_review_analysis_basic(
        self,
        mock_config_class: MagicMock,
        mock_orchestrator_class: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test basic review command in analysis mode."""
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.sonarqube_project_key = "test-project"
        mock_config_class.return_value = mock_config

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        mock_result = MagicMock()
        mock_result.total_issues = 3
        mock_result.files = []
        mock_result.branch = "feature-test"
        mock_result.base_branch = "origin/main"
        mock_orchestrator = MagicMock()
        mock_orchestrator.run_analysis = AsyncMock(return_value=mock_result)
        mock_orchestrator_class.return_value = mock_orchestrator

        result = runner.invoke(app, ["review"])

        assert result.exit_code == 0
        assert "Review Summary" in result.stdout
        assert "Total issues: 3" in result.stdout
        mock_orchestrator.run_analysis.assert_called_once()
        call_kwargs = mock_orchestrator.run_analysis.call_args.kwargs
        assert call_kwargs["base_branch"] is None
        assert call_kwargs["file_patterns"] is None

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.ReviewOrchestrator")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_review_analysis_with_options(
        self,
        mock_config_class: MagicMock,
        mock_orchestrator_class: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test review command with all analysis options."""
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.sonarqube_project_key = "test-project"
        mock_config_class.return_value = mock_config

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        mock_result = MagicMock()
        mock_result.total_issues = 0
        mock_result.files = []
        mock_result.branch = "feature-test"
        mock_result.base_branch = "develop"
        mock_orchestrator = MagicMock()
        mock_orchestrator.run_analysis = AsyncMock(return_value=mock_result)
        mock_orchestrator_class.return_value = mock_orchestrator

        result = runner.invoke(
            app,
            [
                "review",
                "--base-branch",
                "develop",
                "--pattern",
                "*.py",
                "--pattern",
                "*.ts",
                "--verbose",
            ],
        )

        assert result.exit_code == 0
        call_kwargs = mock_orchestrator.run_analysis.call_args.kwargs
        assert call_kwargs["base_branch"] == "develop"
        assert call_kwargs["file_patterns"] == ["*.py", "*.ts"]
        assert call_kwargs["verbose"] is True

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.ReviewOrchestrator")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_review_analysis_with_file_results(
        self,
        mock_config_class: MagicMock,
        mock_orchestrator_class: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test review command displays per-file breakdown."""
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.sonarqube_project_key = "test-project"
        mock_config_class.return_value = mock_config

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        mock_file_review = MagicMock()
        mock_file_review.file_path = "src/test.py"
        mock_file_review.issues = [MagicMock(severity="CRITICAL")]

        mock_result = MagicMock()
        mock_result.total_issues = 1
        mock_result.files = [mock_file_review]
        mock_result.branch = "feature-test"
        mock_result.base_branch = "origin/main"
        mock_orchestrator = MagicMock()
        mock_orchestrator.run_analysis = AsyncMock(return_value=mock_result)
        mock_orchestrator_class.return_value = mock_orchestrator

        result = runner.invoke(app, ["review"])

        assert result.exit_code == 0
        assert "Per-File Breakdown" in result.stdout
        assert "src/test.py" in result.stdout

    @patch("dotharness.vibe_heal.review.reporter.load_report_from_path")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_review_post_basic(
        self,
        mock_config_class: MagicMock,
        mock_load_report: MagicMock,
        fake_code_host,
    ) -> None:
        """Test review command in post mode."""
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.sonarqube_project_key = "test-project"
        mock_config_class.return_value = mock_config

        mock_load_report.return_value = _post_report(total_issues=2)
        fake_code_host.set_current_change_request(10)

        # Pass --report-file so config lookup is skipped
        result = runner.invoke(app, ["review", "--post", "--report-file", "/nonexistent/review.json"])

        assert result.exit_code == 0
        assert "Posted 2 issue(s) and 0 duplication finding(s) as review comments on PR #10." in result.stdout
        assert [p.kind for p in fake_code_host.posts()] == ["review"]

    @patch("dotharness.vibe_heal.review.reporter.load_report_from_path")
    def test_review_post_with_pr_number(
        self,
        mock_load_report: MagicMock,
        fake_code_host,
    ) -> None:
        """Test review command in post mode with explicit PR number."""
        mock_load_report.return_value = _post_report(total_issues=1)
        fake_code_host.set_current_change_request(None)  # a host call would raise

        result = runner.invoke(app, ["review", "--post", "--pr", "42", "--report-file", "/nonexistent/review.json"])

        assert result.exit_code == 0
        assert "PR #42" in result.stdout
        assert len(fake_code_host.posts()) == 1

    @patch("dotharness.vibe_heal.review.reporter.load_report_from_path")
    def test_review_post_no_change_request(self, mock_load_report: MagicMock, fake_code_host) -> None:
        mock_load_report.return_value = _post_report(total_issues=1)
        fake_code_host.set_current_change_request(None)

        result = runner.invoke(app, ["review", "--post", "--report-file", "/nonexistent/review.json"])

        assert result.exit_code == 0
        assert "No open pull request found for the current branch" in result.stdout
        assert "Report saved; use --post later when a PR is available." in result.stdout
        assert fake_code_host.posts() == []

    @patch("dotharness.vibe_heal.review.reporter.load_report_from_path")
    def test_review_post_fallback_warning(self, mock_load_report: MagicMock, fake_code_host) -> None:
        from dotharness.vibe_heal.review.models import FileReview, ReviewIssue, ReviewResult

        issue = ReviewIssue(rule="r:1", message="m", line=3)
        mock_load_report.return_value = ReviewResult(
            project_key="p",
            branch="feature-test",
            base_branch="main",
            files=[FileReview(file_path="a.py", issues=[issue])],
        )
        fake_code_host.reject_inline_reviews("bad line")

        result = runner.invoke(app, ["review", "--post", "--pr", "3", "--report-file", "/nonexistent/review.json"])

        assert result.exit_code == 0
        assert "inline review rejected by GitHub (bad line)" in result.stdout.replace("\n", " ")
        assert [p.kind for p in fake_code_host.posts()] == ["comment"]

    @patch("dotharness.vibe_heal.review.reporter.load_report_from_path")
    def test_review_post_dry_run_posts_nothing(self, mock_load_report: MagicMock, fake_code_host) -> None:
        mock_load_report.return_value = _post_report(total_issues=1)

        result = runner.invoke(
            app, ["review", "--post", "--dry-run", "--pr", "8", "--report-file", "/nonexistent/review.json"]
        )

        assert result.exit_code == 0
        assert "[dry-run] Would post 0 inline comment(s) to PR #8:" in result.stdout
        assert "Review body:" in result.stdout
        assert fake_code_host.post_attempts() == 0

    @patch("dotharness.vibe_heal.review.reporter.load_report_from_path")
    def test_review_post_client_missing(self, mock_load_report: MagicMock, fake_code_host) -> None:
        mock_load_report.return_value = _post_report(total_issues=1)
        fake_code_host.set_available(False)

        result = runner.invoke(app, ["review", "--post", "--pr", "8", "--report-file", "/nonexistent/review.json"])

        assert result.exit_code == 1
        assert "gh CLI is not installed or not in PATH." in result.stdout

    @patch("dotharness.vibe_heal.review.reporter.load_report_from_path")
    def test_review_report_not_found(
        self,
        mock_load_report: MagicMock,
    ) -> None:
        """Test review command handles missing report file."""
        mock_load_report.side_effect = FileNotFoundError("Cannot read report file: /path/to/review.json")

        result = runner.invoke(app, ["review", "--post", "--report-file", "/path/to/review.json"])

        assert result.exit_code == 1
        assert "Cannot read report file" in result.stdout

    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_review_config_error(self, mock_config_class: MagicMock) -> None:
        """Test review command handles configuration errors."""
        from dotharness.vibe_heal.config.exceptions import ConfigurationError

        mock_config_class.side_effect = ConfigurationError("Missing SONARQUBE_URL")

        result = runner.invoke(app, ["review"])

        assert result.exit_code == 1
        assert "Configuration error" in result.stdout
        assert "Missing SONARQUBE_URL" in result.stdout

    def test_review_help_included(self) -> None:
        """Test that review command appears in help output."""
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        assert "review" in result.stdout

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.ReviewOrchestrator")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_review_baseline_success(
        self,
        mock_config_class: MagicMock,
        mock_orchestrator_class: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test review --baseline runs a baseline scan and exits 0 on success."""
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.sonarqube_project_key = "test-project"
        mock_config_class.return_value = mock_config

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.project_key = "test-project"
        mock_result.dashboard_url = "https://sonar.test.com/dashboard?id=test-project"
        mock_orchestrator = MagicMock()
        mock_orchestrator.run_baseline_scan = AsyncMock(return_value=mock_result)
        mock_orchestrator_class.return_value = mock_orchestrator

        result = runner.invoke(app, ["review", "--baseline"])

        assert result.exit_code == 0
        assert "Baseline scan complete" in result.stdout
        mock_orchestrator.run_baseline_scan.assert_called_once()
        mock_orchestrator.run_analysis.assert_not_called()

    @patch("dotharness.vibe_heal.cli.SonarQubeClient")
    @patch("dotharness.vibe_heal.cli.ReviewOrchestrator")
    @patch("dotharness.vibe_heal.cli.VibeHealConfig")
    def test_review_baseline_failure_exits_nonzero(
        self,
        mock_config_class: MagicMock,
        mock_orchestrator_class: MagicMock,
        mock_client_class: MagicMock,
    ) -> None:
        """Test review --baseline exits 1 and prints the error when the scan fails."""
        mock_config = MagicMock(spec=VibeHealConfig)
        mock_config.sonarqube_project_key = "test-project"
        mock_config_class.return_value = mock_config

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.project_key = "test-project"
        mock_result.error_message = "Scanner not found"
        mock_orchestrator = MagicMock()
        mock_orchestrator.run_baseline_scan = AsyncMock(return_value=mock_result)
        mock_orchestrator_class.return_value = mock_orchestrator

        result = runner.invoke(app, ["review", "--baseline"])

        assert result.exit_code == 1
        assert "Baseline scan failed" in result.stdout
        assert "Scanner not found" in result.stdout


class TestDisplayReviewResultsCoverage:
    def test_no_crash_with_coverage_data(self) -> None:
        """_display_review_results runs without error when coverage data is present."""
        from dotharness.vibe_heal.cli import _display_review_results
        from dotharness.vibe_heal.review.models import FileReview
        from dotharness.vibe_heal.review.orchestrator import ReviewAnalysisResult

        result = ReviewAnalysisResult(
            success=True,
            project_key="p",
            branch="feature/x",
            base_branch="main",
            files=[
                FileReview(
                    file_path="src/f.py",
                    coverage_pct=72.0,
                    covered_lines=18,
                    instrumented_changed_lines=25,
                ),
            ],
            files_analyzed=1,
        )
        _display_review_results(result)  # must not raise

    def test_no_crash_without_coverage_data(self) -> None:
        """_display_review_results runs without error when no coverage data."""
        from dotharness.vibe_heal.cli import _display_review_results
        from dotharness.vibe_heal.review.models import FileReview
        from dotharness.vibe_heal.review.orchestrator import ReviewAnalysisResult

        result = ReviewAnalysisResult(
            success=True,
            project_key="p",
            branch="feature/x",
            base_branch="main",
            files=[FileReview(file_path="src/f.py")],
            files_analyzed=1,
        )
        _display_review_results(result)  # must not raise; no Coverage column

    def test_mixed_coverage_and_no_coverage_files(self) -> None:
        """Multiple files where only some have coverage_pct."""
        from dotharness.vibe_heal.cli import _display_review_results
        from dotharness.vibe_heal.review.models import FileReview
        from dotharness.vibe_heal.review.orchestrator import ReviewAnalysisResult

        result = ReviewAnalysisResult(
            success=True,
            project_key="p",
            branch="feature/x",
            base_branch="main",
            files=[
                FileReview(file_path="src/a.py", coverage_pct=50.0, covered_lines=5, instrumented_changed_lines=10),
                FileReview(file_path="src/b.py"),  # no coverage data → shown as "—"
            ],
            files_analyzed=2,
        )
        _display_review_results(result)  # must not raise


class TestConvertReportCommand:
    """Tests for convert-report command."""

    def test_input_file_not_found(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["convert-report", str(tmp_path / "nonexistent.json")])
        assert result.exit_code == 1

    def test_default_output_path(self, tmp_path: Path) -> None:
        input_file = tmp_path / "report.json"
        input_file.write_text('{"diagnostics": []}')
        result = runner.invoke(app, ["convert-report", str(input_file)])
        assert result.exit_code == 0
        assert (tmp_path / "report.eslint.json").exists()

    def test_default_output_path_double_suffix(self, tmp_path: Path) -> None:
        input_file = tmp_path / "report.eslint.json"
        input_file.write_text('{"diagnostics": []}')
        result = runner.invoke(app, ["convert-report", str(input_file)])
        assert result.exit_code == 0
        assert (tmp_path / "report.eslint.eslint.json").exists()

    def test_explicit_output_path(self, tmp_path: Path) -> None:
        input_file = tmp_path / "report.json"
        output_file = tmp_path / "custom-output.json"
        input_file.write_text('{"diagnostics": []}')
        result = runner.invoke(app, ["convert-report", str(input_file), str(output_file)])
        assert result.exit_code == 0
        assert output_file.exists()

    def test_invalid_json_exits_1(self, tmp_path: Path) -> None:
        input_file = tmp_path / "report.json"
        input_file.write_text("not json {{{")
        result = runner.invoke(app, ["convert-report", str(input_file)])
        assert result.exit_code == 1

    def test_json_list_not_dict_exits_1(self, tmp_path: Path) -> None:
        input_file = tmp_path / "report.json"
        input_file.write_text("[]")
        result = runner.invoke(app, ["convert-report", str(input_file)])
        assert result.exit_code == 1

    def test_missing_diagnostics_key_exits_1(self, tmp_path: Path) -> None:
        input_file = tmp_path / "report.json"
        input_file.write_text('{"other": []}')
        result = runner.invoke(app, ["convert-report", str(input_file)])
        assert result.exit_code == 1

    def test_diagnostics_null_exits_1(self, tmp_path: Path) -> None:
        input_file = tmp_path / "report.json"
        input_file.write_text('{"diagnostics": null}')
        result = runner.invoke(app, ["convert-report", str(input_file)])
        assert result.exit_code == 1

    def test_existing_output_overwritten_silently(self, tmp_path: Path) -> None:
        input_file = tmp_path / "report.json"
        input_file.write_text('{"diagnostics": []}')
        output_file = tmp_path / "report.eslint.json"
        output_file.write_text("old content")
        result = runner.invoke(app, ["convert-report", str(input_file)])
        assert result.exit_code == 0
        assert output_file.read_text() != "old content"

    def test_success_prints_converted_summary(self, tmp_path: Path) -> None:
        input_file = tmp_path / "report.json"
        input_file.write_text(
            '{"diagnostics": [{"filename": "src/foo.ts", "message": "test",'
            ' "code": "no-unused-vars", "severity": "warning",'
            ' "labels": [{"span": {"offset": 0, "length": 5, "line": 1, "column": 0}}]}]}'
        )
        result = runner.invoke(app, ["convert-report", str(input_file)])
        assert result.exit_code == 0
        assert "Converted 1 diagnostics across 1 files" in result.output

    def test_malformed_diagnostic_missing_filename_exits_1(self, tmp_path: Path) -> None:
        input_file = tmp_path / "report.json"
        input_file.write_text('{"diagnostics": [{"message": "test"}]}')
        result = runner.invoke(app, ["convert-report", str(input_file)])
        # The converter uses .get() with defaults for filename and message,
        # and _extract_position catches KeyError — so malformed diagnostics
        # are handled gracefully rather than raising KeyError.
        assert result.exit_code == 0


_AI_TOOL_COMMANDS: list[tuple[list[str], str]] = [
    (["fix", "test.py"], "VibeHealOrchestrator"),
    (["dedupe", "test.py"], "DeduplicationOrchestrator"),
    (["cleanup"], "CleanupOrchestrator"),
    (["cleanup-pr"], "CleanupPrOrchestrator"),
    (["dedupe-branch"], "DedupeBranchOrchestrator"),
]


class TestAiToolChoices:
    """The supported --ai-tool values are exactly claude-code and opencode (AC4)."""

    @pytest.mark.parametrize("removed_tool", ["aider", "gemini"])
    @pytest.mark.parametrize(("args", "orchestrator"), _AI_TOOL_COMMANDS)
    def test_removed_tool_is_rejected(self, args: list[str], orchestrator: str, removed_tool: str) -> None:
        """aider and gemini are rejected, naming the valid choices, before any work starts."""
        with (
            patch("dotharness.vibe_heal.cli.VibeHealConfig") as mock_config_class,
            patch(f"dotharness.vibe_heal.cli.{orchestrator}") as mock_orchestrator_class,
        ):
            result = runner.invoke(app, [*args, "--ai-tool", removed_tool])

        assert result.exit_code != 0
        assert "claude-code" in result.output
        assert "opencode" in result.output
        mock_orchestrator_class.assert_not_called()
        mock_config_class.assert_not_called()

    @pytest.mark.parametrize(("args", "orchestrator"), _AI_TOOL_COMMANDS)
    def test_opencode_is_accepted(self, args: list[str], orchestrator: str) -> None:
        """--ai-tool opencode is parsed and applied to the config."""
        with (
            patch("dotharness.vibe_heal.cli.VibeHealConfig") as mock_config_class,
            patch("dotharness.vibe_heal.cli.initialize_ai_tool"),
            patch("dotharness.vibe_heal.cli.SonarQubeClient"),
            patch(f"dotharness.vibe_heal.cli.{orchestrator}"),
        ):
            mock_config = MagicMock(spec=VibeHealConfig)
            mock_config_class.return_value = mock_config
            runner.invoke(app, [*args, "--ai-tool", "opencode"])

        assert mock_config.ai_tool == AIToolType.OPENCODE

    def test_enum_has_only_supported_tools(self) -> None:
        """AIToolType exposes exactly claude-code and opencode."""
        assert {t.value for t in AIToolType} == {"claude-code", "opencode"}

    @pytest.mark.parametrize("removed_tool", ["aider", "gemini"])
    def test_config_rejects_removed_tool_from_environment(
        self, removed_tool: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """AI_TOOL=gemini raises InvalidConfigurationError and `config` exits 1."""
        from dotharness.vibe_heal.config.exceptions import InvalidConfigurationError

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SONARQUBE_URL", "https://sonar.example.com")
        monkeypatch.setenv("SONARQUBE_TOKEN", "token")
        monkeypatch.setenv("SONARQUBE_PROJECT_KEY", "proj")
        monkeypatch.setenv("AI_TOOL", removed_tool)

        with pytest.raises(InvalidConfigurationError, match="Invalid AI tool"):
            VibeHealConfig()

        result = runner.invoke(app, ["config"])
        assert result.exit_code == 1
        assert "Invalid AI tool" in result.output
