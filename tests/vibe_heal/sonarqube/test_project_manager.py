"""Tests for ProjectManager class."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from dotharness.vibe_heal.sonarqube.client import SonarQubeClient
from dotharness.vibe_heal.sonarqube.exceptions import SonarQubeAPIError
from dotharness.vibe_heal.sonarqube.project_manager import ProjectManager, StaleProject, TempProjectMetadata


@pytest.fixture
def mock_client() -> AsyncMock:
    """Create a mock SonarQubeClient."""
    client = AsyncMock(spec=SonarQubeClient)
    return client


@pytest.fixture
def project_manager(mock_client: AsyncMock) -> ProjectManager:
    """Create a ProjectManager with mocked client."""
    return ProjectManager(mock_client)


class TestProjectManagerInit:
    """Tests for ProjectManager initialization."""

    def test_init(self, mock_client: AsyncMock) -> None:
        """Test ProjectManager initialization."""
        manager = ProjectManager(mock_client)

        assert manager.client == mock_client


class TestCreateTempProject:
    """Tests for create_temp_project method."""

    @pytest.mark.asyncio
    async def test_create_temp_project_success(self, project_manager: ProjectManager, mock_client: AsyncMock) -> None:
        """Test successful temporary project creation."""
        base_key = "my_project"
        branch_name = "feature/new-api"
        user_email = "user@example.com"

        mock_client.create_project = AsyncMock()

        metadata = await project_manager.create_temp_project(base_key, branch_name, user_email)

        # Verify project key has expected format with timestamp
        expected_prefix = "my_project_user_example_com_feature_new_api_"
        assert metadata.project_key.startswith(expected_prefix)

        # Extract and verify timestamp format (yymmdd-hhmm)
        timestamp = metadata.project_key[len(expected_prefix) :]
        assert len(timestamp) == 11  # 6 digits + dash + 4 digits = 11 chars
        assert timestamp[6] == "-"
        assert timestamp[:6].isdigit()  # yymmdd
        assert timestamp[7:].isdigit()  # hhmm

        # Verify project was created with the generated key and human-readable name
        mock_client.create_project.assert_called_once_with(metadata.project_key, metadata.project_name)

        # Verify metadata
        assert metadata.project_name == "my_project analysis user feature-new-api"
        assert metadata.base_project_key == base_key
        assert metadata.branch_name == branch_name
        assert metadata.user_email == user_email
        assert isinstance(metadata.created_at, str)

        # Verify timestamp is recent (within last minute)
        created_time = datetime.fromisoformat(metadata.created_at)
        now = datetime.now(UTC)
        time_diff = (now - created_time).total_seconds()
        assert time_diff < 60

    @pytest.mark.asyncio
    async def test_create_temp_project_sanitizes_email(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """Test that email addresses are properly sanitized."""
        mock_client.create_project = AsyncMock()

        metadata = await project_manager.create_temp_project("project", "main", "user+tag@example.co.uk")

        # Check prefix without timestamp
        expected_prefix = "project_user_tag_example_co_uk_main_"
        assert metadata.project_key.startswith(expected_prefix)
        # Verify project was created with the generated key (including timestamp)
        mock_client.create_project.assert_called_once_with(metadata.project_key, metadata.project_name)

    @pytest.mark.asyncio
    async def test_create_temp_project_sanitizes_branch(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """Test that branch names are properly sanitized."""
        mock_client.create_project = AsyncMock()

        test_branches = [
            ("feature/api-v2", "feature_api_v2"),
            ("bugfix/issue-#123", "bugfix_issue__123"),
            ("hotfix/urgent!!", "hotfix_urgent__"),
            ("release/1.2.3", "release_1_2_3"),
        ]

        for branch, expected_sanitized in test_branches:
            metadata = await project_manager.create_temp_project("proj", branch, "user@test.com")
            # Check prefix without timestamp
            expected_prefix = f"proj_user_test_com_{expected_sanitized}_"
            assert metadata.project_key.startswith(expected_prefix)

    @pytest.mark.asyncio
    async def test_create_temp_project_api_error(self, project_manager: ProjectManager, mock_client: AsyncMock) -> None:
        """Test error handling when project creation fails."""
        mock_client.create_project = AsyncMock(side_effect=SonarQubeAPIError("Project already exists", status_code=400))

        with pytest.raises(SonarQubeAPIError, match="Project already exists"):
            await project_manager.create_temp_project("project", "main", "user@test.com")

    @pytest.mark.asyncio
    async def test_create_temp_project_unique_keys(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """Test that different branches/users generate different project keys."""
        mock_client.create_project = AsyncMock()

        metadata1 = await project_manager.create_temp_project("proj", "branch1", "user1@test.com")
        metadata2 = await project_manager.create_temp_project("proj", "branch2", "user1@test.com")
        metadata3 = await project_manager.create_temp_project("proj", "branch1", "user2@test.com")

        # All keys should be different
        assert metadata1.project_key != metadata2.project_key
        assert metadata1.project_key != metadata3.project_key
        assert metadata2.project_key != metadata3.project_key

    @pytest.mark.asyncio
    async def test_project_name_format_with_command(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        mock_client.create_project = AsyncMock()
        metadata = await project_manager.create_temp_project(
            base_key="my-project",
            branch_name="feature/add-login",
            user_email="alexei.eleusis@gmail.com",
            command_name="review",
        )
        assert metadata.project_name == "my-project review alexei.eleusis feature-add-login"

    @pytest.mark.asyncio
    async def test_project_name_uses_default_command_name(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        mock_client.create_project = AsyncMock()
        metadata = await project_manager.create_temp_project(
            base_key="my-project",
            branch_name="main",
            user_email="user@example.com",
        )
        assert metadata.project_name == "my-project analysis user main"

    @pytest.mark.asyncio
    async def test_project_name_email_without_at_sign(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        mock_client.create_project = AsyncMock()
        metadata = await project_manager.create_temp_project(
            base_key="proj",
            branch_name="main",
            user_email="localonly",
            command_name="cleanup",
        )
        assert metadata.project_name == "proj cleanup localonly main"

    @pytest.mark.asyncio
    async def test_project_name_branch_slashes_become_dashes(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        mock_client.create_project = AsyncMock()
        metadata = await project_manager.create_temp_project(
            base_key="proj",
            branch_name="feature/nested/branch",
            user_email="u@t.com",
            command_name="dedupe-branch",
        )
        assert metadata.project_name == "proj dedupe-branch u feature-nested-branch"


class TestDeleteProject:
    """Tests for delete_project method."""

    @pytest.mark.asyncio
    async def test_delete_project_success(self, project_manager: ProjectManager, mock_client: AsyncMock) -> None:
        """Test successful project deletion."""
        mock_client.delete_project = AsyncMock()

        await project_manager.delete_project("test_project")

        mock_client.delete_project.assert_called_once_with("test_project")

    @pytest.mark.asyncio
    async def test_delete_project_api_error(self, project_manager: ProjectManager, mock_client: AsyncMock) -> None:
        """Test error handling when project deletion fails."""
        mock_client.delete_project = AsyncMock(side_effect=SonarQubeAPIError("Project not found", status_code=404))

        with pytest.raises(SonarQubeAPIError, match="Project not found"):
            await project_manager.delete_project("nonexistent")


class TestProjectExists:
    """Tests for project_exists method."""

    @pytest.mark.asyncio
    async def test_project_exists_true(self, project_manager: ProjectManager, mock_client: AsyncMock) -> None:
        """Test checking existence of existing project."""
        mock_client.project_exists = AsyncMock(return_value=True)

        exists = await project_manager.project_exists("test_project")

        assert exists is True
        mock_client.project_exists.assert_called_once_with("test_project")

    @pytest.mark.asyncio
    async def test_project_exists_false(self, project_manager: ProjectManager, mock_client: AsyncMock) -> None:
        """Test checking existence of non-existent project."""
        mock_client.project_exists = AsyncMock(return_value=False)

        exists = await project_manager.project_exists("nonexistent")

        assert exists is False
        mock_client.project_exists.assert_called_once_with("nonexistent")


class TestSanitizeIdentifier:
    """Tests for _sanitize_identifier method."""

    def test_sanitize_email(self, project_manager: ProjectManager) -> None:
        """Test sanitization of email addresses."""
        test_cases = [
            ("user@example.com", "user_example_com"),
            ("first.last@example.com", "first_last_example_com"),
            ("user+tag@example.co.uk", "user_tag_example_co_uk"),
            ("123@test.com", "123_test_com"),
        ]

        for input_val, expected in test_cases:
            result = project_manager._sanitize_identifier(input_val)
            assert result == expected, f"Failed for input: {input_val}"

    def test_sanitize_branch_names(self, project_manager: ProjectManager) -> None:
        """Test sanitization of branch names."""
        test_cases = [
            ("feature/new-api", "feature_new_api"),
            ("bugfix/issue-#123", "bugfix_issue__123"),
            ("release/1.2.3", "release_1_2_3"),
            ("hotfix/urgent!", "hotfix_urgent_"),
            ("main", "main"),
            ("feature_branch", "feature_branch"),
        ]

        for input_val, expected in test_cases:
            result = project_manager._sanitize_identifier(input_val)
            assert result == expected, f"Failed for input: {input_val}"

    def test_sanitize_special_characters(self, project_manager: ProjectManager) -> None:
        """Test sanitization of various special characters."""
        test_cases = [
            ("test@#$%", "test____"),
            ("hello world", "hello_world"),
            ("Test-Case_123", "test_case_123"),
            ("a.b.c.d", "a_b_c_d"),
            ("mixed/chars-here_123", "mixed_chars_here_123"),
        ]

        for input_val, expected in test_cases:
            result = project_manager._sanitize_identifier(input_val)
            assert result == expected, f"Failed for input: {input_val}"

    def test_sanitize_uppercase_to_lowercase(self, project_manager: ProjectManager) -> None:
        """Test that sanitization converts to lowercase."""
        test_cases = [
            ("UPPERCASE", "uppercase"),
            ("MixedCase", "mixedcase"),
            ("User@Example.COM", "user_example_com"),
        ]

        for input_val, expected in test_cases:
            result = project_manager._sanitize_identifier(input_val)
            assert result == expected, f"Failed for input: {input_val}"

    def test_sanitize_preserves_underscores(self, project_manager: ProjectManager) -> None:
        """Test that underscores are preserved during sanitization."""
        test_cases = [
            ("already_sanitized", "already_sanitized"),
            ("test_with_underscores_123", "test_with_underscores_123"),
            ("_leading_underscore", "_leading_underscore"),
            ("trailing_underscore_", "trailing_underscore_"),
        ]

        for input_val, expected in test_cases:
            result = project_manager._sanitize_identifier(input_val)
            assert result == expected, f"Failed for input: {input_val}"

    def test_sanitize_consecutive_special_chars(self, project_manager: ProjectManager) -> None:
        """Test sanitization with consecutive special characters."""
        test_cases = [
            ("test---case", "test___case"),
            ("hello...world", "hello___world"),
            ("a@@b##c", "a__b__c"),
        ]

        for input_val, expected in test_cases:
            result = project_manager._sanitize_identifier(input_val)
            assert result == expected, f"Failed for input: {input_val}"


class TestTempProjectMetadata:
    """Tests for TempProjectMetadata model."""

    def test_metadata_creation(self) -> None:
        """Test creating TempProjectMetadata."""
        metadata = TempProjectMetadata(
            project_key="proj_user_test_com_main",
            project_name="proj_user_test_com_main",
            created_at="2025-10-23T12:00:00+00:00",
            base_project_key="proj",
            branch_name="main",
            user_email="user@test.com",
        )

        assert metadata.project_key == "proj_user_test_com_main"
        assert metadata.project_name == "proj_user_test_com_main"
        assert metadata.base_project_key == "proj"
        assert metadata.branch_name == "main"
        assert metadata.user_email == "user@test.com"
        assert metadata.created_at == "2025-10-23T12:00:00+00:00"

    def test_metadata_validation(self) -> None:
        """Test that metadata validates required fields."""
        from pydantic import ValidationError

        # Missing required field should raise error
        with pytest.raises(ValidationError):
            TempProjectMetadata.model_validate({
                "project_key": "test",
                "project_name": "test",
                # Missing created_at
                "base_project_key": "base",
                "branch_name": "main",
            })


class TestFindStaleProjects:
    """Tests for find_stale_projects method."""

    @pytest.mark.asyncio
    async def test_finds_stale_temp_project(self, project_manager: ProjectManager, mock_client: AsyncMock) -> None:
        """A temp project older than the threshold with zero analyses is reported stale."""
        old_timestamp = (datetime.now(UTC) - timedelta(hours=2)).strftime("%y%m%d-%H%M")
        stale_key = f"my_project_user_example_com_main_{old_timestamp}"
        mock_client.search_projects = AsyncMock(
            return_value=[{"key": stale_key, "name": "my_project analysis user main"}]
        )
        mock_client.get_project_analyses_count = AsyncMock(return_value=0)

        result = await project_manager.find_stale_projects("my_project", older_than_minutes=60)

        assert len(result) == 1
        assert result[0].project_key == stale_key
        assert result[0].age_minutes >= 120
        mock_client.search_projects.assert_called_once_with(query="my_project")
        mock_client.get_project_analyses_count.assert_called_once_with(stale_key)

    @pytest.mark.asyncio
    async def test_skips_project_with_finished_analysis(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """A temp project that has at least one analysis is not stale."""
        old_timestamp = (datetime.now(UTC) - timedelta(hours=2)).strftime("%y%m%d-%H%M")
        key = f"my_project_user_example_com_main_{old_timestamp}"
        mock_client.search_projects = AsyncMock(return_value=[{"key": key, "name": "my_project analysis user main"}])
        mock_client.get_project_analyses_count = AsyncMock(return_value=1)

        result = await project_manager.find_stale_projects("my_project", older_than_minutes=60)

        assert result == []

    @pytest.mark.asyncio
    async def test_skips_project_younger_than_threshold(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """A recently created temp project is not stale, even with zero analyses."""
        recent_timestamp = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%y%m%d-%H%M")
        key = f"my_project_user_example_com_main_{recent_timestamp}"
        mock_client.search_projects = AsyncMock(return_value=[{"key": key, "name": "my_project analysis user main"}])
        mock_client.get_project_analyses_count = AsyncMock(return_value=0)

        result = await project_manager.find_stale_projects("my_project", older_than_minutes=60)

        assert result == []
        mock_client.get_project_analyses_count.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_non_matching_project_keys(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """Projects that don't match the temp-project naming convention are ignored."""
        mock_client.search_projects = AsyncMock(
            return_value=[
                {"key": "my_project", "name": "My Project"},
                {"key": "my_project_other_thing", "name": "My Project Other"},
            ]
        )
        mock_client.get_project_analyses_count = AsyncMock(return_value=0)

        result = await project_manager.find_stale_projects("my_project", older_than_minutes=60)

        assert result == []
        mock_client.get_project_analyses_count.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_candidates(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """No candidates found means no stale projects."""
        mock_client.search_projects = AsyncMock(return_value=[])

        result = await project_manager.find_stale_projects("my_project", older_than_minutes=60)

        assert result == []

    @pytest.mark.asyncio
    async def test_one_candidate_failing_does_not_sink_the_batch(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """A single candidate whose analyses-count lookup fails is skipped, not fatal."""
        old_timestamp = (datetime.now(UTC) - timedelta(hours=2)).strftime("%y%m%d-%H%M")
        failing_key = f"my_project_user_example_com_main_{old_timestamp}"
        ok_key = f"my_project_user_example_com_other_{old_timestamp}"
        mock_client.search_projects = AsyncMock(
            return_value=[
                {"key": failing_key, "name": "my_project analysis user main"},
                {"key": ok_key, "name": "my_project analysis user other"},
            ]
        )
        mock_client.get_project_analyses_count = AsyncMock(side_effect=[SonarQubeAPIError("not found"), 0])

        result = await project_manager.find_stale_projects("my_project", older_than_minutes=60)

        assert [p.project_key for p in result] == [ok_key]

    @pytest.mark.asyncio
    async def test_pairs_analyses_counts_with_correct_candidate(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """With multiple candidates, each analyses count is paired with its own project key."""
        old_timestamp = (datetime.now(UTC) - timedelta(hours=2)).strftime("%y%m%d-%H%M")
        stale_key = f"my_project_user_example_com_a_{old_timestamp}"
        active_key = f"my_project_user_example_com_b_{old_timestamp}"
        mock_client.search_projects = AsyncMock(
            return_value=[
                {"key": stale_key, "name": "my_project analysis user a"},
                {"key": active_key, "name": "my_project analysis user b"},
            ]
        )
        mock_client.get_project_analyses_count = AsyncMock(side_effect=[0, 2])

        result = await project_manager.find_stale_projects("my_project", older_than_minutes=60)

        assert [p.project_key for p in result] == [stale_key]


class TestPruneStaleProjects:
    """Tests for prune_stale_projects method."""

    def _stale_project(self, key: str) -> StaleProject:
        return StaleProject(project_key=key, created_at=datetime.now(UTC) - timedelta(hours=2))

    @pytest.mark.asyncio
    async def test_all_deletions_succeed(self, project_manager: ProjectManager, mock_client: AsyncMock) -> None:
        """When every deletion succeeds, deleted_count matches total_count and failed_count is zero."""
        stale_projects = [self._stale_project("proj_a"), self._stale_project("proj_b")]
        mock_client.delete_project = AsyncMock()

        result = await project_manager.prune_stale_projects(stale_projects)

        assert result.deleted_count == 2
        assert result.total_count == 2
        assert result.failed_count == 0
        assert mock_client.delete_project.await_count == 2

    @pytest.mark.asyncio
    async def test_partial_failure_is_reflected_in_failed_count(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """A failed deletion is skipped rather than aborting the batch, and counted as failed."""
        stale_projects = [self._stale_project("proj_a"), self._stale_project("proj_b"), self._stale_project("proj_c")]
        mock_client.delete_project = AsyncMock(side_effect=[None, SonarQubeAPIError("boom"), None])

        result = await project_manager.prune_stale_projects(stale_projects)

        assert result.deleted_count == 2
        assert result.total_count == 3
        assert result.failed_count == 1
        assert mock_client.delete_project.await_count == 3

    @pytest.mark.asyncio
    async def test_empty_input_returns_zero_counts(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """Pruning an empty list deletes nothing and reports zero counts."""
        mock_client.delete_project = AsyncMock()

        result = await project_manager.prune_stale_projects([])

        assert result.deleted_count == 0
        assert result.total_count == 0
        assert result.failed_count == 0
        mock_client.delete_project.assert_not_awaited()


class TestCopyExclusionSettings:
    """Tests for copy_exclusion_settings method."""

    def test_exclusion_settings_constant(self) -> None:
        """Test that EXCLUSION_SETTINGS constant has expected values."""
        expected = (
            "sonar.exclusions",
            "sonar.test.exclusions",
            "sonar.coverage.exclusions",
            "sonar.cpd.exclusions",
            "sonar.inclusions",
            "sonar.test.inclusions",
        )
        assert expected == ProjectManager.EXCLUSION_SETTINGS

    @pytest.mark.asyncio
    async def test_copy_exclusion_settings_copies_non_inherited(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """Test that only non-inherited exclusion settings are copied."""
        settings = [
            {"key": "sonar.exclusions", "value": "**/vendor/**", "inherited": False},
            {"key": "sonar.test.exclusions", "values": ["tests/**"], "inherited": False},
            {"key": "sonar.coverage.exclusions", "value": "src/gen/**", "inherited": True},
            {"key": "sonar.cpd.exclusions", "value": "**/generated/**", "inherited": False},
            {"key": "sonar.inclusions", "values": ["src/**/*.py"], "inherited": True},
            {"key": "sonar.test.inclusions", "value": "tests/**/*.py", "inherited": False},
            {"key": "sonar.qualitygate", "value": "default", "inherited": False},
        ]
        mock_client.get_project_settings = AsyncMock(return_value=settings)
        mock_client.set_project_setting = AsyncMock()

        copied, inherited_count, failed_count = await project_manager.copy_exclusion_settings(
            "source-project", "target-project"
        )

        assert sorted(copied) == sorted([
            "sonar.exclusions",
            "sonar.test.exclusions",
            "sonar.cpd.exclusions",
            "sonar.test.inclusions",
        ])
        assert inherited_count == 2
        assert failed_count == 0
        assert mock_client.set_project_setting.call_count == 4
        mock_client.set_project_setting.assert_any_call("target-project", "sonar.exclusions", ["**/vendor/**"])
        mock_client.set_project_setting.assert_any_call("target-project", "sonar.test.exclusions", ["tests/**"])
        mock_client.set_project_setting.assert_any_call("target-project", "sonar.cpd.exclusions", ["**/generated/**"])
        mock_client.set_project_setting.assert_any_call("target-project", "sonar.test.inclusions", ["tests/**/*.py"])

    @pytest.mark.asyncio
    async def test_copy_exclusion_settings_returns_empty_when_none_match(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """Test that empty list is returned when no exclusion settings are found."""
        settings = [
            {"key": "sonar.qualitygate", "value": "default", "inherited": False},
            {"key": "sonar.javascript.lcov.reportPaths", "value": "coverage/lcov.info", "inherited": False},
        ]
        mock_client.get_project_settings = AsyncMock(return_value=settings)
        mock_client.set_project_setting = AsyncMock()

        copied, inherited_count, failed_count = await project_manager.copy_exclusion_settings(
            "source-project", "target-project"
        )

        assert copied == []
        assert inherited_count == 0
        assert failed_count == 0
        mock_client.set_project_setting.assert_not_called()

    @pytest.mark.asyncio
    async def test_copy_exclusion_settings_raises_if_fetch_fails(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """Test that the error propagates if fetching settings fails."""
        mock_client.get_project_settings = AsyncMock(
            side_effect=SonarQubeAPIError("Permission denied", status_code=403)
        )

        with pytest.raises(SonarQubeAPIError, match="Permission denied"):
            await project_manager.copy_exclusion_settings("source-project", "target-project")

    @pytest.mark.asyncio
    async def test_copy_exclusion_settings_skips_failed_set(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """Test that individual set failures are logged and skipped."""
        settings = [
            {"key": "sonar.cpd.exclusions", "value": "**/generated/**", "inherited": False},
            {"key": "sonar.exclusions", "value": "**/*.gen.ts", "inherited": False},
        ]
        mock_client.get_project_settings = AsyncMock(return_value=settings)
        mock_client.set_project_setting = AsyncMock(side_effect=[SonarQubeAPIError("Failed"), None])

        copied, inherited_count, failed_count = await project_manager.copy_exclusion_settings(
            "source-project", "target-project"
        )

        assert copied == ["sonar.exclusions"]
        assert inherited_count == 0
        assert failed_count == 1
        assert mock_client.set_project_setting.call_count == 2

    def test_normalize_setting_values_scalar(self, project_manager: ProjectManager) -> None:
        """Test normalization of scalar setting values."""
        setting = {"key": "sonar.cpd.exclusions", "value": "**/generated/**"}
        result = project_manager._normalize_setting_values(setting)
        assert result == ["**/generated/**"]

    def test_normalize_setting_values_list(self, project_manager: ProjectManager) -> None:
        """Test normalization of multi-value setting values."""
        setting = {"key": "sonar.exclusions", "values": ["**/*.gen.ts", "tests/**"]}
        result = project_manager._normalize_setting_values(setting)
        assert result == ["**/*.gen.ts", "tests/**"]

    def test_normalize_setting_values_none(self, project_manager: ProjectManager) -> None:
        """Test normalization of setting with None value."""
        setting = {"key": "sonar.exclusions", "value": None}
        result = project_manager._normalize_setting_values(setting)
        assert result == []

    def test_normalize_setting_values_empty(self, project_manager: ProjectManager) -> None:
        """Test normalization of setting with no value fields."""
        setting = {"key": "sonar.exclusions"}
        result = project_manager._normalize_setting_values(setting)
        assert result == []


class TestCreateTempProjectWithSettings:
    """Tests for create_temp_project_with_settings method."""

    @pytest.mark.asyncio
    async def test_create_with_settings_success(self, project_manager: ProjectManager, mock_client: AsyncMock) -> None:
        """Test successful creation with exclusion settings copy."""
        mock_client.create_project = AsyncMock()
        mock_client.get_project_settings = AsyncMock(
            return_value=[
                {"key": "sonar.exclusions", "value": "**/vendor/**", "inherited": False},
            ]
        )
        mock_client.set_project_setting = AsyncMock()

        metadata = await project_manager.create_temp_project_with_settings(
            base_key="my_project",
            branch_name="feature/test",
            user_email="user@example.com",
        )

        assert metadata.project_key.startswith("my_project_user_example_com_feature_test_")
        mock_client.create_project.assert_called_once()
        mock_client.set_project_setting.assert_called_once_with(
            metadata.project_key, "sonar.exclusions", ["**/vendor/**"]
        )

    @pytest.mark.asyncio
    async def test_create_with_settings_handles_copy_error(
        self, project_manager: ProjectManager, mock_client: AsyncMock
    ) -> None:
        """Test that settings copy errors don't prevent project creation."""
        mock_client.create_project = AsyncMock()
        mock_client.get_project_settings = AsyncMock(
            side_effect=SonarQubeAPIError("Permission denied", status_code=403)
        )

        with patch("dotharness.vibe_heal.sonarqube.project_manager.warn") as mock_warn:
            metadata = await project_manager.create_temp_project_with_settings(
                base_key="my_project",
                branch_name="main",
                user_email="user@example.com",
            )
            assert metadata is not None
            mock_warn.assert_any_call("Warning: Could not copy exclusion settings: Permission denied")
