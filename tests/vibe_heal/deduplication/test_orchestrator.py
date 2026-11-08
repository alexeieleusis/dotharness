"""Tests for DeduplicationOrchestrator.dedupe_file."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from dotharness.vibe_heal.ai_tools.base import AITool
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.deduplication.models import (
    DuplicationBlock,
    DuplicationFileInfo,
    DuplicationGroup,
    DuplicationsResponse,
)
from dotharness.vibe_heal.deduplication.orchestrator import DeduplicationOrchestrator
from dotharness.vibe_heal.deduplication.processor import DuplicationProcessingResult
from dotharness.vibe_heal.git import GitManager


@pytest.fixture
def config() -> VibeHealConfig:
    """Create test configuration."""
    return VibeHealConfig(
        sonarqube_url="https://sonar.test.com",
        sonarqube_token="test-token",  # noqa: S106
        sonarqube_project_key="my-project",
    )


@pytest.fixture
def mock_ai_tool() -> MagicMock:
    """Create a mock AITool."""
    return MagicMock(spec=AITool)


def _make_response(file_path: str, project_key: str) -> DuplicationsResponse:
    """Build a DuplicationsResponse with two groups, each holding a block in the target file.

    The target file uses ref "1"; the other file uses ref "2".
    """
    target_key = f"{project_key}:{file_path}"
    return DuplicationsResponse(
        duplications=[
            DuplicationGroup(
                blocks=[
                    DuplicationBlock(**{"from": 10, "size": 5, "_ref": "1"}),
                    DuplicationBlock(**{"from": 50, "size": 5, "_ref": "2"}),
                ]
            ),
            DuplicationGroup(
                blocks=[
                    DuplicationBlock(**{"from": 40, "size": 5, "_ref": "1"}),
                    DuplicationBlock(**{"from": 100, "size": 5, "_ref": "2"}),
                ]
            ),
        ],
        files={
            "1": DuplicationFileInfo(**{"key": target_key, "name": "test.py", "projectName": "My Project"}),
            "2": DuplicationFileInfo(**{
                "key": f"{project_key}:src/other.py",
                "name": "other.py",
                "projectName": "My Project",
            }),
        },
    )


class TestDedupeFileGroupFilter:
    """Tests for the optional group_filter hook on dedupe_file."""

    @pytest.mark.asyncio
    async def test_dedupe_file_group_filter_none_leaves_response_untouched(
        self,
        config: VibeHealConfig,
        mock_ai_tool: MagicMock,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """When group_filter is omitted, the processor receives the original response."""
        file_path = str(tmp_path / "test.py")
        (tmp_path / "test.py").write_text("code\n" * 60)

        mocker.patch.object(GitManager, "is_repository", return_value=True)

        response = _make_response(file_path, config.sonarqube_project_key)

        mock_client = AsyncMock()
        mock_client.get_duplications_for_file.return_value = response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.deduplication.orchestrator.DuplicationClient", return_value=mock_client)

        mock_processor = mocker.patch("dotharness.vibe_heal.deduplication.orchestrator.DuplicationProcessor")
        mock_processor.return_value.process.return_value = DuplicationProcessingResult(
            total_groups=2,
            processable_groups=2,
            skipped_groups=0,
            groups_to_fix=[],
        )

        orchestrator = DeduplicationOrchestrator(config=config, ai_tool=mock_ai_tool)

        summary = await orchestrator.dedupe_file(file_path, dry_run=True)

        assert summary.total_issues == 2
        processed_response = mock_processor.return_value.process.call_args.args[0]
        assert processed_response is response
        assert processed_response.duplications == response.duplications

    @pytest.mark.asyncio
    async def test_dedupe_file_group_filter_scopes_processed_groups(
        self,
        config: VibeHealConfig,
        mock_ai_tool: MagicMock,
        mocker: MockerFixture,
        tmp_path: Path,
    ) -> None:
        """When group_filter is supplied, it is called per group with the target ref and
        only the kept groups reach the processor."""
        file_path = str(tmp_path / "test.py")
        (tmp_path / "test.py").write_text("code\n" * 60)

        mocker.patch.object(GitManager, "is_repository", return_value=True)

        response = _make_response(file_path, config.sonarqube_project_key)
        kept_group = response.duplications[1]

        mock_client = AsyncMock()
        mock_client.get_duplications_for_file.return_value = response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mocker.patch("dotharness.vibe_heal.deduplication.orchestrator.DuplicationClient", return_value=mock_client)

        mock_processor = mocker.patch("dotharness.vibe_heal.deduplication.orchestrator.DuplicationProcessor")
        mock_processor.return_value.process.return_value = DuplicationProcessingResult(
            total_groups=1,
            processable_groups=1,
            skipped_groups=0,
            groups_to_fix=[],
        )

        orchestrator = DeduplicationOrchestrator(config=config, ai_tool=mock_ai_tool)

        seen: list[tuple[DuplicationGroup, str]] = []

        def group_filter(group: DuplicationGroup, target_ref: str) -> bool:
            seen.append((group, target_ref))
            return group is kept_group

        summary = await orchestrator.dedupe_file(file_path, dry_run=True, group_filter=group_filter)

        # The predicate was called for every group, with the target file ref
        assert seen == [(response.duplications[0], "1"), (kept_group, "1")]
        # Only the kept group reached the processor; the rest of the response is untouched
        processed_response = mock_processor.return_value.process.call_args.args[0]
        assert processed_response is not response
        assert processed_response.duplications == [kept_group]
        assert processed_response.files == response.files
        assert summary.total_issues == 1


class TestMarkupEscaping:
    """Dynamic error text must not be interpreted as Rich markup."""

    def test_failed_fix_with_markup_in_error(self, config: VibeHealConfig, mock_ai_tool: MagicMock) -> None:
        from rich.console import Console
        from rich.progress import Progress

        from dotharness.vibe_heal.ai_tools.models import FixResult
        from dotharness.vibe_heal.models import FixSummary

        orch = DeduplicationOrchestrator(config, mock_ai_tool, git_manager=MagicMock(spec=GitManager))
        summary = FixSummary(total_issues=1)
        with Progress(console=Console(record=True, width=120)) as progress:
            task = progress.add_task("x")
            fix = FixResult(success=False, error_message="hook output [/red] failed")
            orch._handle_failed_fix(fix, summary, progress, task)
            progress.refresh()
        assert summary.failed == 1
