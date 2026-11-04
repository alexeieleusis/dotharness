"""Tests for orchestrator."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from dotharness.vibe_heal.ai_tools import AIToolType, ClaudeCodeTool, FixResult
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.git import GitManager
from dotharness.vibe_heal.orchestrator import VibeHealOrchestrator
from dotharness.vibe_heal.processor.models import ProcessingResult
from dotharness.vibe_heal.sonarqube.exceptions import SonarQubeRuleNotFoundError
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue, SonarQubeRule


@pytest.fixture
def mock_config() -> VibeHealConfig:
    """Create mock configuration.

    Returns:
        Mock configuration
    """
    config = MagicMock(spec=VibeHealConfig)
    config.sonarqube_url = "https://sonar.example.com"
    config.sonarqube_project_key = "test-project"
    config.ai_tool = None  # Test auto-detection
    config.include_rule_description = True  # Default to true
    config.code_context_lines = 5  # Default value
    config.pre_commit_command = None  # Default: auto-detect
    config.pre_commit_timeout_seconds = 300
    return config


@pytest.fixture
def sample_issue() -> SonarQubeIssue:
    """Create sample issue for testing.

    Returns:
        Sample SonarQube issue
    """
    return SonarQubeIssue(
        key="ABC123",
        rule="python:S1481",
        severity="MAJOR",
        message="Remove unused variable",
        component="project:src/test.py",
        line=10,
        status="OPEN",
        type="CODE_SMELL",
    )


class TestOrchestratorInit:
    """Tests for orchestrator initialization."""

    def test_init_with_configured_ai_tool(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
    ) -> None:
        """Test initialization with configured AI tool."""
        mock_config.ai_tool = AIToolType.CLAUDE_CODE
        mocker.patch("shutil.which", return_value="/usr/bin/claude")

        orchestrator = VibeHealOrchestrator(mock_config)

        assert orchestrator.config == mock_config
        assert isinstance(orchestrator.ai_tool, ClaudeCodeTool)

    def test_init_with_auto_detect(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
    ) -> None:
        """Test initialization with auto-detected AI tool."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")

        orchestrator = VibeHealOrchestrator(mock_config)

        assert isinstance(orchestrator.ai_tool, ClaudeCodeTool)

    def test_init_raises_when_no_ai_tool(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
    ) -> None:
        """Test initialization fails when no AI tool available."""
        mocker.patch("shutil.which", return_value=None)

        with pytest.raises(RuntimeError, match="No AI tool found"):
            VibeHealOrchestrator(mock_config)


class TestOrchestratorValidation:
    """Tests for precondition validation."""

    def test_validate_preconditions_not_git_repo(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """Test validation fails when not a Git repository."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=False)

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        with pytest.raises(RuntimeError, match="Not a Git repository"):
            orchestrator._validate_preconditions(str(test_file), dry_run=False)

    def test_validate_preconditions_working_directory_has_uncommitted_changes(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """Test validation fails when working directory has uncommitted changes."""
        from dotharness.vibe_heal.git.exceptions import DirtyWorkingDirectoryError

        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)
        mocker.patch.object(
            GitManager, "require_clean_working_directory", side_effect=DirtyWorkingDirectoryError("dirty")
        )

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        with pytest.raises(DirtyWorkingDirectoryError):
            orchestrator._validate_preconditions(str(test_file), dry_run=False)

    def test_validate_preconditions_file_not_found(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
    ) -> None:
        """Test validation fails when file doesn't exist."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)

        orchestrator = VibeHealOrchestrator(mock_config)

        with pytest.raises(FileNotFoundError):
            orchestrator._validate_preconditions("/nonexistent/file.py", dry_run=False)

    def test_validate_preconditions_ai_tool_not_available(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """Test validation fails when AI tool is not available."""
        # Use configured AI tool to bypass auto-detection
        mock_config.ai_tool = AIToolType.CLAUDE_CODE
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)
        mocker.patch.object(GitManager, "require_clean_working_directory")

        orchestrator = VibeHealOrchestrator(mock_config)

        # Now mock the tool as unavailable for validation
        mocker.patch.object(orchestrator.ai_tool, "is_available", return_value=False)

        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        with pytest.raises(RuntimeError, match="not available"):
            orchestrator._validate_preconditions(str(test_file), dry_run=False)

    def test_validate_preconditions_dry_run_skips_clean_check(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """Test validation skips clean working directory check in dry-run mode."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)
        mock_clean_check = mocker.patch.object(GitManager, "require_clean_working_directory")

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        orchestrator._validate_preconditions(str(test_file), dry_run=True)

        # Should not check for clean working directory in dry-run mode
        mock_clean_check.assert_not_called()


class TestOrchestratorFixFile:
    """Tests for fix_file method."""

    @pytest.mark.asyncio
    async def test_fix_file_no_issues(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """Test fix_file when no issues found."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)

        # Mock SonarQube client
        mock_client = AsyncMock()
        mock_client.get_issues_for_file.return_value = []
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.orchestrator.SonarQubeClient", return_value=mock_client)

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        summary = await orchestrator.fix_file(str(test_file), dry_run=True)

        assert summary.total_issues == 0
        assert summary.fixed == 0
        assert summary.failed == 0

    @pytest.mark.asyncio
    async def test_fix_file_no_fixable_issues(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test fix_file when no fixable issues."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)

        # Make issue not fixable
        sample_issue.status = "RESOLVED"

        # Mock SonarQube client
        mock_client = AsyncMock()
        mock_client.get_issues_for_file.return_value = [sample_issue]
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.orchestrator.SonarQubeClient", return_value=mock_client)

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        summary = await orchestrator.fix_file(str(test_file), dry_run=True)

        assert summary.total_issues == 1
        assert summary.fixed == 0
        assert summary.skipped == 1

    @pytest.mark.asyncio
    async def test_fix_file_issue_filter_none_passes_full_list_to_processor(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """When issue_filter is omitted, the processor receives the full fetched list."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)

        issues = [
            SonarQubeIssue(
                key="issue-1",
                rule="python:S1481",
                message="First issue",
                component="project:src/test.py",
                line=10,
                status="OPEN",
                severity="MAJOR",
                type="CODE_SMELL",
            ),
            SonarQubeIssue(
                key="issue-2",
                rule="python:S1481",
                message="Second issue",
                component="project:src/test.py",
                line=20,
                status="OPEN",
                severity="MAJOR",
                type="CODE_SMELL",
            ),
        ]

        mock_client = AsyncMock()
        mock_client.get_issues_for_file.return_value = issues
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.orchestrator.SonarQubeClient", return_value=mock_client)

        mock_processor = mocker.patch("dotharness.vibe_heal.orchestrator.IssueProcessor")
        mock_processor.return_value.process.return_value = ProcessingResult(
            total_issues=2,
            fixable_issues=2,
            skipped_issues=0,
            issues_to_fix=[],
        )

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        summary = await orchestrator.fix_file(str(test_file), dry_run=True)

        mock_processor.return_value.process.assert_called_once_with(issues)
        assert summary.total_issues == 2
        assert summary.fixed == 0

    @pytest.mark.asyncio
    async def test_fix_file_issue_filter_passes_only_matching_issues_to_processor(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """When issue_filter is supplied, only the issues it keeps reach the processor."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)

        issues = [
            SonarQubeIssue(
                key="issue-1",
                rule="python:S1481",
                message="First issue",
                component="project:src/test.py",
                line=10,
                status="OPEN",
                severity="MAJOR",
                type="CODE_SMELL",
            ),
            SonarQubeIssue(
                key="issue-2",
                rule="python:S1481",
                message="Second issue",
                component="project:src/test.py",
                line=20,
                status="OPEN",
                severity="MAJOR",
                type="CODE_SMELL",
            ),
            SonarQubeIssue(
                key="issue-3",
                rule="python:S1481",
                message="Third issue",
                component="project:src/test.py",
                line=30,
                status="OPEN",
                severity="MAJOR",
                type="CODE_SMELL",
            ),
        ]

        mock_client = AsyncMock()
        mock_client.get_issues_for_file.return_value = issues
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.orchestrator.SonarQubeClient", return_value=mock_client)

        mock_processor = mocker.patch("dotharness.vibe_heal.orchestrator.IssueProcessor")
        mock_processor.return_value.process.return_value = ProcessingResult(
            total_issues=1,
            fixable_issues=1,
            skipped_issues=0,
            issues_to_fix=[],
        )

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        await orchestrator.fix_file(str(test_file), dry_run=True, issue_filter=lambda issue: issue.line == 20)

        mock_processor.return_value.process.assert_called_once_with([issues[1]])

    @pytest.mark.asyncio
    async def test_fix_file_issue_filter_removes_all_issues_uses_existing_empty_path(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """When issue_filter removes every issue, the existing no-fixable path is taken."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)

        issue = SonarQubeIssue(
            key="issue-1",
            rule="python:S1481",
            message="First issue",
            component="project:src/test.py",
            line=10,
            status="OPEN",
            severity="MAJOR",
            type="CODE_SMELL",
        )

        mock_client = AsyncMock()
        mock_client.get_issues_for_file.return_value = [issue]
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.orchestrator.SonarQubeClient", return_value=mock_client)

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        summary = await orchestrator.fix_file(str(test_file), dry_run=True, issue_filter=lambda i: False)

        assert summary.total_issues == 0
        assert summary.fixed == 0
        assert summary.failed == 0

    @pytest.mark.asyncio
    async def test_fix_file_successful_dry_run(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test successful fix in dry-run mode."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)

        # Mock SonarQube client
        mock_client = AsyncMock()
        mock_client.get_issues_for_file.return_value = [sample_issue]
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.orchestrator.SonarQubeClient", return_value=mock_client)

        # Mock AI tool
        mocker.patch.object(
            ClaudeCodeTool,
            "fix_issue",
            return_value=FixResult(success=True, files_modified=["test.py"]),
        )

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        summary = await orchestrator.fix_file(str(test_file), dry_run=True)

        assert summary.total_issues == 1
        assert summary.fixed == 1
        assert summary.failed == 0
        assert len(summary.commits) == 0  # No commits in dry-run

    @pytest.mark.asyncio
    async def test_fix_file_rule_not_found_falls_back_to_external_docs(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """When get_rule_details raises SonarQubeRuleNotFoundError, external docs from the
        issue message URLs are fetched and forwarded to fix_issue."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)

        mock_client = AsyncMock()
        mock_client.get_issues_for_file.return_value = [sample_issue]
        mock_client.get_rule_details.side_effect = SonarQubeRuleNotFoundError("rule not found")
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.orchestrator.SonarQubeClient", return_value=mock_client)

        # external_docs fetched from issue message URLs
        mocker.patch(
            "dotharness.vibe_heal.orchestrator.fetch_external_rule_docs",
            return_value=["# External rule doc"],
        )

        mock_fix_issue = mocker.patch.object(
            ClaudeCodeTool,
            "fix_issue",
            return_value=FixResult(success=True, files_modified=["test.py"]),
        )

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        summary = await orchestrator.fix_file(str(test_file), dry_run=True)

        assert summary.total_issues == 1
        assert summary.fixed == 1
        assert mock_fix_issue.call_args.kwargs.get("external_docs") == ["# External rule doc"]

    @pytest.mark.asyncio
    async def test_fix_file_rule_not_found_reuses_vibe_types_docs(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """When the rule isn't found but the concurrent vibe-types knowledge-doc fetch already
        succeeded, those docs are reused and merged with non-vibe-types docs instead of being
        discarded and re-fetched from scratch."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)

        mock_client = AsyncMock()
        mock_client.get_issues_for_file.return_value = [sample_issue]
        mock_client.get_rule_details.side_effect = SonarQubeRuleNotFoundError("rule not found")
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.orchestrator.SonarQubeClient", return_value=mock_client)

        mocker.patch(
            "dotharness.vibe_heal.orchestrator.fetch_vibe_types_knowledge_docs",
            return_value=["# T01 knowledge"],
        )
        mock_fetch_external = mocker.patch(
            "dotharness.vibe_heal.orchestrator.fetch_external_rule_docs",
            return_value=["# Other doc"],
        )

        mock_fix_issue = mocker.patch.object(
            ClaudeCodeTool,
            "fix_issue",
            return_value=FixResult(success=True, files_modified=["test.py"]),
        )

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        summary = await orchestrator.fix_file(str(test_file), dry_run=True)

        assert summary.total_issues == 1
        assert summary.fixed == 1
        assert mock_fix_issue.call_args.kwargs.get("external_docs") == ["# T01 knowledge", "# Other doc"]
        mock_fetch_external.assert_called_once_with(sample_issue.message, exclude_vibe_types=True)

    @pytest.mark.asyncio
    async def test_fix_file_rule_found_fetches_vibe_types_knowledge_docs(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """When get_rule_details succeeds, vibe-types knowledge docs from the issue message
        URLs are still fetched and forwarded to fix_issue."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)

        mock_rule = MagicMock(spec=SonarQubeRule)
        mock_client = AsyncMock()
        mock_client.get_issues_for_file.return_value = [sample_issue]
        mock_client.get_rule_details.return_value = mock_rule
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.orchestrator.SonarQubeClient", return_value=mock_client)

        mocker.patch(
            "dotharness.vibe_heal.orchestrator.fetch_vibe_types_knowledge_docs",
            return_value=["# T01 knowledge"],
        )

        mock_fix_issue = mocker.patch.object(
            ClaudeCodeTool,
            "fix_issue",
            return_value=FixResult(success=True, files_modified=["test.py"]),
        )

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        summary = await orchestrator.fix_file(str(test_file), dry_run=True)

        assert summary.total_issues == 1
        assert summary.fixed == 1
        assert mock_fix_issue.call_args.kwargs.get("rule") is mock_rule
        assert mock_fix_issue.call_args.kwargs.get("external_docs") == ["# T01 knowledge"]

    @pytest.mark.asyncio
    async def test_fix_file_rule_found_no_vibe_types_docs(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        tmp_path: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """When get_rule_details succeeds and no vibe-types URL is in the message,
        external_docs stays None (unchanged existing behavior)."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")
        mocker.patch.object(GitManager, "is_repository", return_value=True)

        mock_rule = MagicMock(spec=SonarQubeRule)
        mock_client = AsyncMock()
        mock_client.get_issues_for_file.return_value = [sample_issue]
        mock_client.get_rule_details.return_value = mock_rule
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.orchestrator.SonarQubeClient", return_value=mock_client)

        mocker.patch("dotharness.vibe_heal.orchestrator.fetch_vibe_types_knowledge_docs", return_value=[])

        mock_fix_issue = mocker.patch.object(
            ClaudeCodeTool,
            "fix_issue",
            return_value=FixResult(success=True, files_modified=["test.py"]),
        )

        orchestrator = VibeHealOrchestrator(mock_config)
        test_file = tmp_path / "test.py"
        test_file.write_text("code")

        await orchestrator.fix_file(str(test_file), dry_run=True)

        assert mock_fix_issue.call_args.kwargs.get("external_docs") is None

    @pytest.mark.asyncio
    async def test_fetch_rule_details_docs_fetch_error_still_returns_rule(
        self,
        mock_config: VibeHealConfig,
        mocker: MockerFixture,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """A docs-fetch exception on the rule-found path must not discard the already-fetched
        rule — it must not be caught by the get_rule_details try/except."""
        mocker.patch("shutil.which", return_value="/usr/bin/claude")

        mock_rule = MagicMock(spec=SonarQubeRule)
        mock_client = AsyncMock()
        mock_client.get_rule_details.return_value = mock_rule
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.orchestrator.SonarQubeClient", return_value=mock_client)
        mocker.patch(
            "dotharness.vibe_heal.orchestrator.fetch_vibe_types_knowledge_docs",
            side_effect=RuntimeError("boom"),
        )

        orchestrator = VibeHealOrchestrator(mock_config)

        rule, docs = await orchestrator._fetch_rule_details(sample_issue)

        assert rule is mock_rule
        assert docs is None
