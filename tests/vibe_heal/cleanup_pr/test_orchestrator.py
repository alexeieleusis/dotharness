"""Tests for CleanupPrOrchestrator class."""

import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dotharness.vibe_heal.ai_tools.base import AITool
from dotharness.vibe_heal.cleanup_pr.orchestrator import (
    CleanupPrOrchestrator,
    FileCleanupPrResult,
)
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.deduplication.models import DuplicationsResponse
from dotharness.vibe_heal.git.branch_analyzer import BranchAnalyzer
from dotharness.vibe_heal.git.diff_parser import DiffLines
from dotharness.vibe_heal.git.exceptions import DirtyWorkingDirectoryError, GitOperationError
from dotharness.vibe_heal.git.manager import GitManager
from dotharness.vibe_heal.models import FixSummary
from dotharness.vibe_heal.sonarqube.analysis_runner import AnalysisResult, AnalysisRunner
from dotharness.vibe_heal.sonarqube.client import SonarQubeClient
from dotharness.vibe_heal.sonarqube.exceptions import ComponentNotFoundError
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue
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
    """Create a mock SonarQubeClient.

    ``config`` is an instance attribute (not a class attribute) so the spec does
    not expose it; it is set up front so the orchestrator's key override/restore
    can read and write ``client.config.sonarqube_project_key``.
    """
    client = AsyncMock(spec=SonarQubeClient)
    client.config = MagicMock()
    return client


@pytest.fixture
def mock_ai_tool() -> MagicMock:
    """Create a mock AITool."""
    return MagicMock(spec=AITool)


@pytest.fixture
def orchestrator(
    config: VibeHealConfig,
    mock_client: AsyncMock,
    mock_ai_tool: MagicMock,
) -> CleanupPrOrchestrator:
    """Create CleanupPrOrchestrator with mocked dependencies."""
    return CleanupPrOrchestrator(config, mock_client, mock_ai_tool)


def _git_result(returncode: int, stderr: bytes = b"") -> subprocess.CompletedProcess:
    """Build a subprocess result with the given exit code and stderr."""
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=None, stderr=stderr)


@contextmanager
def _preconditions_pass(
    orchestrator: CleanupPrOrchestrator,
    fetch_rc: int = 0,
    ancestor_rc: int = 0,
    dirty: bool = False,
    remotes: tuple[str, ...] = ("origin", "upstream"),
) -> Generator[list[list[str]]]:
    """Make the up-front preconditions pass, with controllable git exit codes.

    ``fetch_rc`` controls the ``git fetch`` exit code, ``ancestor_rc`` the
    ``git merge-base --is-ancestor`` exit code, and ``dirty`` whether the strict
    clean-tree check raises. Yields the git argvs that were run, in order, so
    callers can pin the exact commands (parsed remote/branch, argument order).
    """
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        commands.append(cmd)
        if cmd == ["git", "remote"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(remotes).encode(), stderr=b"")
        if "fetch" in cmd:
            stderr = b"fatal: unable to access 'https://example.com/'" if fetch_rc else b""
            return _git_result(fetch_rc, stderr=stderr)
        if "merge-base" in cmd:
            return _git_result(ancestor_rc)
        msg = f"Unexpected git command: {cmd}"
        raise AssertionError(msg)

    clean_side_effect = DirtyWorkingDirectoryError("dirty working tree") if dirty else None
    with (
        patch.object(orchestrator.git_manager, "is_repository", return_value=True),
        patch.object(orchestrator.branch_analyzer, "validate_branch_exists", return_value=True),
        patch.object(orchestrator.branch_analyzer, "get_current_branch", return_value="feature"),
        patch.object(orchestrator.branch_analyzer, "get_user_email", return_value="u@example.com"),
        patch.object(orchestrator.ai_tool, "is_available", return_value=True),
        patch.object(orchestrator.git_manager, "require_clean_working_directory", side_effect=clean_side_effect),
        patch("dotharness.vibe_heal.git.branch_analyzer.subprocess.run", side_effect=fake_run),
    ):
        yield commands


class TestCleanupPrOrchestratorInit:
    """Tests for CleanupPrOrchestrator initialization."""

    def test_init(
        self,
        config: VibeHealConfig,
        mock_client: AsyncMock,
        mock_ai_tool: MagicMock,
    ) -> None:
        """Test CleanupPrOrchestrator initialization."""
        orchestrator = CleanupPrOrchestrator(config, mock_client, mock_ai_tool)

        assert orchestrator.config == config
        assert orchestrator.client == mock_client
        assert orchestrator.ai_tool == mock_ai_tool
        assert isinstance(orchestrator.project_manager, ProjectManager)
        assert isinstance(orchestrator.analysis_runner, AnalysisRunner)
        assert isinstance(orchestrator.branch_analyzer, BranchAnalyzer)
        assert isinstance(orchestrator.git_manager, GitManager)


class TestCleanupPrPreconditions:
    """Strict preconditions are enforced before any SonarQube work."""

    @pytest.mark.asyncio
    async def test_fetch_failure_refuses_before_sonarqube(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
    ) -> None:
        """A failed fetch refuses the run with a domain error, before SonarQube work."""
        with _preconditions_pass(orchestrator, fetch_rc=1), pytest.raises(GitOperationError, match="fetch"):
            await orchestrator.cleanup_pr()

        # No SonarQube client call happened.
        assert mock_client.method_calls == []

    @pytest.mark.asyncio
    async def test_fetch_failure_uses_parsed_remote_and_branch(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
    ) -> None:
        """The fetch command is built from the parsed remote/branch of the base ref."""
        with _preconditions_pass(orchestrator, fetch_rc=1) as commands, pytest.raises(GitOperationError):
            await orchestrator.cleanup_pr(base_branch="upstream/develop")

        # Pin the exact argv: the split remote and branch, not just "fetch" somewhere.
        assert ["git", "fetch", "upstream", "develop"] in commands
        assert mock_client.method_calls == []

    @pytest.mark.asyncio
    async def test_ancestry_check_pins_base_ref_before_head(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
    ) -> None:
        """The up-to-date check runs merge-base --is-ancestor <base> HEAD, in that order."""
        with (
            _preconditions_pass(orchestrator) as commands,
            patch.object(orchestrator.branch_analyzer, "get_modified_files", return_value=[]),
        ):
            await orchestrator.cleanup_pr(base_branch="upstream/develop")

        # The full base ref (unsplit) is the ancestor argument, HEAD the descendant argument.
        assert ["git", "merge-base", "--is-ancestor", "upstream/develop", "HEAD"] in commands
        assert mock_client.method_calls == []

    @pytest.mark.asyncio
    async def test_bare_branch_name_skips_fetch(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
    ) -> None:
        """A bare local name is not fetched (it would not update the local ref)."""
        with (
            _preconditions_pass(orchestrator) as commands,
            patch.object(orchestrator.branch_analyzer, "get_modified_files", return_value=[]),
        ):
            await orchestrator.cleanup_pr(base_branch="main")

        assert not any("fetch" in c for c in commands)
        assert ["git", "merge-base", "--is-ancestor", "main", "HEAD"] in commands

    @pytest.mark.asyncio
    async def test_slashed_local_branch_is_not_split(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
    ) -> None:
        """A local branch like release/1.0 (prefix is not a remote) is not misparsed."""
        with (
            _preconditions_pass(orchestrator) as commands,
            patch.object(orchestrator.branch_analyzer, "get_modified_files", return_value=[]),
        ):
            await orchestrator.cleanup_pr(base_branch="release/1.0")

        assert not any("fetch" in c for c in commands)
        assert ["git", "merge-base", "--is-ancestor", "release/1.0", "HEAD"] in commands

    @pytest.mark.asyncio
    async def test_slashed_branch_name_splits_on_first_slash_only(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
    ) -> None:
        """A slashed branch name keeps everything after the first slash as the branch."""
        with _preconditions_pass(orchestrator, fetch_rc=1) as commands, pytest.raises(GitOperationError):
            await orchestrator.cleanup_pr(base_branch="origin/feature/x")

        assert ["git", "fetch", "origin", "feature/x"] in commands
        assert mock_client.method_calls == []

    @pytest.mark.asyncio
    async def test_not_up_to_date_refuses_before_sonarqube(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
    ) -> None:
        """A non-ancestor (not up to date) base refuses the run, before SonarQube work."""
        with _preconditions_pass(orchestrator, ancestor_rc=1), pytest.raises(GitOperationError, match="not up to date"):
            await orchestrator.cleanup_pr()

        # No SonarQube client call happened.
        assert mock_client.method_calls == []

    @pytest.mark.asyncio
    async def test_dirty_tree_raises_before_sonarqube(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
    ) -> None:
        """A dirty working tree raises DirtyWorkingDirectoryError, before SonarQube work."""
        with _preconditions_pass(orchestrator, dirty=True), pytest.raises(DirtyWorkingDirectoryError):
            await orchestrator.cleanup_pr()

        # No SonarQube client call happened.
        assert mock_client.method_calls == []


class TestCleanupPrFileSelection:
    """File selection (FR-2 step 2)."""

    @pytest.mark.asyncio
    async def test_empty_file_list_returns_success_with_zero_counts(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
    ) -> None:
        """An empty selection returns a successful result with zero counts."""
        with (
            _preconditions_pass(orchestrator),
            patch.object(
                orchestrator.branch_analyzer,
                "get_modified_files",
                return_value=[],
            ),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project_with_settings",
                new_callable=AsyncMock,
            ) as mock_create,
        ):
            result = await orchestrator.cleanup_pr()

        assert result.success is True
        assert result.files_processed == []
        assert result.total_issues_fixed == 0
        assert result.total_duplications_fixed == 0
        assert result.total_main_duplications_fixed == 0
        assert result.external_files_touched == []
        assert result.temp_project is None
        # No temp project was created and no SonarQube work happened.
        mock_create.assert_not_called()
        assert mock_client.method_calls == []

    @pytest.mark.asyncio
    async def test_empty_file_list_after_pattern_filtering(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
    ) -> None:
        """Files filtered out by patterns also return a successful zero-count result."""
        with (
            _preconditions_pass(orchestrator),
            patch.object(
                orchestrator.branch_analyzer,
                "get_modified_files",
                return_value=[Path("src/a.py"), Path("src/b.py")],
            ),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project_with_settings",
                new_callable=AsyncMock,
            ) as mock_create,
        ):
            result = await orchestrator.cleanup_pr(file_patterns=["*.ts"])

        assert result.success is True
        assert result.files_processed == []
        assert result.total_issues_fixed == 0
        mock_create.assert_not_called()
        assert mock_client.method_calls == []

    # Note: pattern filtering now lives in vibe_heal.git.file_selection;
    # its Path.match semantics are tested in tests/git/test_file_selection.py.


class TestCleanupPrTempProjectLifecycle:
    """Project-key override/restore and temp-project cleanup."""

    @pytest.mark.asyncio
    async def test_project_keys_restored_after_exception(
        self,
        orchestrator: CleanupPrOrchestrator,
        config: VibeHealConfig,
        mock_client: AsyncMock,
        temp_project: TempProjectMetadata,
    ) -> None:
        """Both config and client project keys are restored after an exception."""
        original_key = config.sonarqube_project_key
        with (
            _preconditions_pass(orchestrator),
            patch.object(
                orchestrator.branch_analyzer,
                "get_modified_files",
                return_value=[Path("src/a.py")],
            ),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project_with_settings",
                new_callable=AsyncMock,
                return_value=temp_project,
            ),
            patch.object(
                orchestrator,
                "_run_iteration_loop",
                new_callable=AsyncMock,
                side_effect=Exception("boom"),
            ),
            patch.object(orchestrator.project_manager, "delete_project", new_callable=AsyncMock),
        ):
            result = await orchestrator.cleanup_pr()

        assert result.success is False
        assert "boom" in (result.error_message or "")
        # Both keys restored to the original value on every path.
        assert config.sonarqube_project_key == original_key
        assert mock_client.config.sonarqube_project_key == original_key

    @pytest.mark.asyncio
    async def test_temp_project_deleted_after_failed_analysis(
        self,
        orchestrator: CleanupPrOrchestrator,
        temp_project: TempProjectMetadata,
    ) -> None:
        """The temp project is deleted when the loop (analysis) fails."""
        with (
            _preconditions_pass(orchestrator),
            patch.object(
                orchestrator.branch_analyzer,
                "get_modified_files",
                return_value=[Path("src/a.py")],
            ),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project_with_settings",
                new_callable=AsyncMock,
                return_value=temp_project,
            ),
            patch.object(
                orchestrator,
                "_run_iteration_loop",
                new_callable=AsyncMock,
                side_effect=Exception("Analysis failed"),
            ),
            patch.object(
                orchestrator.project_manager,
                "delete_project",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            result = await orchestrator.cleanup_pr()

        assert result.success is False
        assert "Analysis failed" in (result.error_message or "")
        mock_delete.assert_called_once_with(temp_project.project_key)

    @pytest.mark.asyncio
    async def test_deletion_failure_is_only_a_warning(
        self,
        orchestrator: CleanupPrOrchestrator,
        temp_project: TempProjectMetadata,
    ) -> None:
        """A temp-project deletion failure does not fail the overall run."""
        with (
            _preconditions_pass(orchestrator),
            patch.object(
                orchestrator.branch_analyzer,
                "get_modified_files",
                return_value=[Path("src/a.py")],
            ),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project_with_settings",
                new_callable=AsyncMock,
                return_value=temp_project,
            ),
            patch.object(
                orchestrator,
                "_run_iteration_loop",
                new_callable=AsyncMock,
                return_value=([FileCleanupPrResult(file_path=Path("src/a.py"), success=True)], None, []),
            ),
            patch.object(
                orchestrator.project_manager,
                "delete_project",
                new_callable=AsyncMock,
                side_effect=Exception("delete failed"),
            ),
        ):
            result = await orchestrator.cleanup_pr()

        # Deletion is a warning, not an error, so the run still succeeds.
        assert result.success is True
        assert result.total_issues_fixed == 0


class TestCreateTempProject(BaseTestCreateTempProject):
    """Tests for _create_temp_project method."""

    pass


# ----------------------------------------------------------------------
# Iteration loop (FR-2 steps 5-6)
# ----------------------------------------------------------------------

_MODULE = "dotharness.vibe_heal.cleanup_pr.orchestrator"
_FILE = Path("src/a.py")


def _issue(key: str, line: int | None, rule: str = "python:S1481") -> SonarQubeIssue:
    """Build a fixable SonarQube issue at ``line``."""
    return SonarQubeIssue(
        key=key,
        rule=rule,
        message="msg",
        component="my-project:src/a.py",
        line=line,
        severity="MAJOR",
        status="OPEN",
    )


def _dup_response(project_key: str, from_line: int, size: int = 5) -> DuplicationsResponse:
    """Build a one-group duplications response whose target block is ``from_line`` (+size)."""
    return DuplicationsResponse.model_validate({
        "duplications": [
            {"blocks": [{"from": from_line, "size": size, "_ref": "1"}, {"from": 1, "size": size, "_ref": "2"}]}
        ],
        "files": {
            "1": {"key": f"{project_key}:src/a.py", "name": "a.py", "projectName": "p"},
            "2": {"key": f"{project_key}:src/b.py", "name": "b.py", "projectName": "p"},
        },
    })


def _diff(new: set[int], strict: set[int] | None = None, rel: str = "src/a.py") -> DiffLines:
    """Build DiffLines for one file (strict defaults to the same set)."""
    return DiffLines(
        new_lines={rel: new},
        old_lines={},
        strict_new_lines={rel: new if strict is None else strict},
    )


@dataclass
class _Harness:
    """Mocks for the loop's collaborators, patched at the importing module."""

    analysis: AsyncMock
    diff_parser: MagicMock
    dup_client: MagicMock
    dedupe: MagicMock
    fixer: MagicMock
    sleep: AsyncMock


@contextmanager
def _loop_env(
    orchestrator: CleanupPrOrchestrator,
    mock_client: AsyncMock,
    temp_project: TempProjectMetadata,
    diffs: list[DiffLines],
    issues: list[list[SonarQubeIssue]] | None = None,
    dup_responses: list[DuplicationsResponse] | None = None,
    analysis_ok: bool = True,
    fixed_dups: int = 0,
    fixed_issues: int = 0,
) -> Generator[_Harness]:
    """Patch everything the loop touches; per-iteration data comes from the lists."""
    analysis = AsyncMock(
        return_value=AnalysisResult(success=analysis_ok, error_message=None if analysis_ok else "scanner boom")
    )
    orchestrator.analysis_runner.run_analysis = analysis  # type: ignore[method-assign]
    diff_parser = MagicMock()
    diff_parser.get_diff_lines.side_effect = diffs
    orchestrator.diff_parser = diff_parser

    mock_client.get_issues_for_file = AsyncMock(side_effect=issues or [[] for _ in diffs])

    dup_client = MagicMock()
    responses = iter(dup_responses or [DuplicationsResponse() for _ in diffs])
    dup_client.get_duplications_for_file = AsyncMock(side_effect=lambda _p: next(responses))
    dup_cm = MagicMock()
    dup_cm.__aenter__ = AsyncMock(return_value=dup_client)
    dup_cm.__aexit__ = AsyncMock(return_value=None)

    dedupe = MagicMock()
    dedupe.dedupe_file = AsyncMock(
        return_value=FixSummary(total_issues=1, fixed=fixed_dups, commits=["c"] * fixed_dups)
    )
    fixer = MagicMock()
    fixer.fix_file = AsyncMock(
        return_value=FixSummary(total_issues=1, fixed=fixed_issues, commits=["c"] * fixed_issues)
    )
    sleep = AsyncMock()

    with (
        patch(f"{_MODULE}.DuplicationClient", return_value=dup_cm),
        patch(f"{_MODULE}.DeduplicationOrchestrator", return_value=dedupe),
        patch(f"{_MODULE}.VibeHealOrchestrator", return_value=fixer),
        patch(f"{_MODULE}.asyncio.sleep", sleep),
        patch.object(orchestrator, "_to_repo_relative", side_effect=lambda p: p.as_posix()),
        patch.object(orchestrator.branch_analyzer, "get_head_sha", side_effect=["h0", "h1"] * 10),
    ):
        yield _Harness(analysis, diff_parser, dup_client, dedupe, fixer, sleep)


async def _run_loop(
    orchestrator: CleanupPrOrchestrator,
    temp_project: TempProjectMetadata,
    max_iterations: int = 3,
    dry_run: bool = False,
    min_severity: str | None = None,
) -> tuple[list[FileCleanupPrResult], AnalysisResult | None, list[Path]]:
    return await orchestrator._run_iteration_loop(
        modified_files=[_FILE],
        temp_project=temp_project,
        base_branch="origin/main",
        max_iterations=max_iterations,
        min_severity=min_severity,
        dry_run=dry_run,
        verbose=True,
    )


class TestIterationLoopIssueScope:
    """FR-4: issues are scoped to the trailing-window changed lines."""

    @pytest.mark.asyncio
    async def test_in_scope_and_out_of_scope_and_boundary(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """Changed 10-12: line 15 is the trailing-window boundary (in), 16 and 9 are out."""
        issues = [[_issue("in1", 11), _issue("edge", 15), _issue("after", 16), _issue("before", 9)]]
        with _loop_env(
            orchestrator,
            mock_client,
            temp_project,
            [_diff({10, 11, 12, 13, 14, 15}, {10, 11, 12})],
            issues,
            fixed_issues=2,
        ) as h:
            files, _, external = await _run_loop(orchestrator, temp_project, max_iterations=1)

        assert files[0].issues_fixed == 2
        assert files[0].issues_out_of_scope == 2
        assert external == []
        issue_filter = h.fixer.fix_file.call_args.kwargs["issue_filter"]
        assert issue_filter(_issue("in1", 11)) is True
        assert issue_filter(_issue("edge", 15)) is True
        assert issue_filter(_issue("after", 16)) is False
        assert issue_filter(_issue("before", 9)) is False
        # Queried by CWD-relative path.
        mock_client.get_issues_for_file.assert_awaited_with("src/a.py")


class TestIterationLoopDuplicationScope:
    """FR-5: duplications must intersect the strict changed lines."""

    @pytest.mark.asyncio
    async def test_dup_intersecting_is_fixed_and_defers_issues(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """An intersecting group is deduped; issues wait for the next analysis."""
        pk = temp_project.project_key
        with (
            _loop_env(
                orchestrator,
                mock_client,
                temp_project,
                [_diff({10, 11})],
                issues=[[_issue("i", 10)]],
                dup_responses=[_dup_response(pk, 8, 5)],
                fixed_dups=1,
            ) as h,
            patch.object(orchestrator.branch_analyzer, "repo", MagicMock(**{"git.diff.return_value": ""})),
        ):
            files, _, _ = await _run_loop(orchestrator, temp_project, max_iterations=1)

        assert files[0].duplications_fixed == 1
        h.dedupe.dedupe_file.assert_awaited_once()
        assert h.dedupe.dedupe_file.call_args.kwargs["file_path"] == "src/a.py"
        h.fixer.fix_file.assert_not_awaited()  # deferred: dup commits shifted lines
        mock_client.get_issues_for_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dup_not_intersecting_is_out_of_scope(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """A group that misses the strict lines is counted out of scope and not deduped."""
        pk = temp_project.project_key
        with _loop_env(
            orchestrator, mock_client, temp_project, [_diff({50})], dup_responses=[_dup_response(pk, 8, 5)]
        ) as h:
            files, _, _ = await _run_loop(orchestrator, temp_project)

        assert files[0].duplications_out_of_scope == 1
        assert files[0].duplications_fixed == 0
        h.dedupe.dedupe_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_group_filter_uses_strict_lines(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """The group_filter hook matches the strict set, not the trailing window."""
        pk = temp_project.project_key
        resp = _dup_response(pk, 10, 3)  # lines 10-12
        with _loop_env(orchestrator, mock_client, temp_project, [_diff({11})], dup_responses=[resp]) as h:
            await _run_loop(orchestrator, temp_project, max_iterations=1)

        group_filter = h.dedupe.dedupe_file.call_args.kwargs["group_filter"]
        assert group_filter(resp.duplications[0], "1") is True
        assert group_filter(resp.duplications[0], "2") is False  # block at line 1 misses line 11

    @pytest.mark.asyncio
    async def test_external_files_recorded(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """Files a dedupe commit touches outside the diff set are reported (once)."""
        pk = temp_project.project_key
        repo = MagicMock()
        repo.git.diff.return_value = "src/a.py\nsrc/util.py\nsrc/b.py\n"
        with (
            _loop_env(
                orchestrator,
                mock_client,
                temp_project,
                [_diff({10}, rel="src/a.py")],
                dup_responses=[_dup_response(pk, 9, 3)],
                fixed_dups=1,
            ),
            patch.object(orchestrator.branch_analyzer, "repo", repo),
        ):
            diff = DiffLines(
                new_lines={"src/a.py": {10}, "src/b.py": {1}}, old_lines={}, strict_new_lines={"src/a.py": {10}}
            )
            cast("MagicMock", orchestrator.diff_parser.get_diff_lines).side_effect = [diff]
            _, _, external = await _run_loop(orchestrator, temp_project, max_iterations=1)

        assert external == [Path("src/util.py")]
        repo.git.diff.assert_called_once_with("--name-only", "h0", "h1")

    @pytest.mark.asyncio
    async def test_file_touched_by_earlier_commit_is_deferred(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """A file a duplication commit modified earlier in the round is not fixed from stale analysis."""
        pk = temp_project.project_key
        repo = MagicMock()
        repo.git.diff.return_value = "src/a.py\nsrc/b.py\n"
        diff = DiffLines(
            new_lines={"src/a.py": {10}, "src/b.py": {10}},
            old_lines={},
            strict_new_lines={"src/a.py": {10}, "src/b.py": {10}},
        )
        other = Path("src/b.py")
        with (
            _loop_env(
                orchestrator,
                mock_client,
                temp_project,
                [diff],
                issues=[[_issue("i", 10)]],
                dup_responses=[_dup_response(pk, 9, 3)],
                fixed_dups=1,
            ) as h,
            patch.object(orchestrator.branch_analyzer, "repo", repo),
        ):
            cast("MagicMock", orchestrator.diff_parser.get_diff_lines).side_effect = [diff]
            files, _, _ = await orchestrator._run_iteration_loop(
                modified_files=[_FILE, other],
                temp_project=temp_project,
                base_branch="origin/main",
                max_iterations=1,
                min_severity=None,
                dry_run=False,
                verbose=True,
            )

        assert files[1].issues_fixed == 0
        h.fixer.fix_file.assert_not_awaited()
        mock_client.get_issues_for_file.assert_not_awaited()
        assert h.dedupe.dedupe_file.await_count == 1  # b.py was not processed at all


class TestIterationLoopControl:
    """Loop control: diff recompute, early stop, failures, dry-run, flag off."""

    @pytest.mark.asyncio
    async def test_diff_recomputed_each_iteration(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """The diff is recomputed every round; a shifted map changes what is in scope."""
        issues = [[_issue("i", 10)], [_issue("i", 10)], [_issue("i", 10)]]
        diffs = [_diff({10}, {10}), _diff({20}, {20}), _diff({10}, {10})]
        with _loop_env(orchestrator, mock_client, temp_project, diffs, issues, fixed_issues=1) as h:
            files, _, _ = await _run_loop(orchestrator, temp_project, max_iterations=3)

        assert h.diff_parser.get_diff_lines.call_count == 2
        h.diff_parser.get_diff_lines.assert_called_with("origin/main")
        # Iteration 2's map moved the changes away from line 10, so nothing was in scope: early stop.
        assert h.fixer.fix_file.await_count == 1
        assert h.analysis.await_count == 2
        assert files[0].issues_fixed == 1

    @pytest.mark.asyncio
    async def test_early_stop_when_nothing_in_scope(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """No in-scope work means one analysis, no fixes, no sleep."""
        with _loop_env(orchestrator, mock_client, temp_project, [_diff({10})]) as h:
            files, analysis, external = await _run_loop(orchestrator, temp_project, max_iterations=5)

        assert h.analysis.await_count == 1
        assert analysis is not None
        h.fixer.fix_file.assert_not_awaited()
        h.dedupe.dedupe_file.assert_not_awaited()
        h.sleep.assert_not_awaited()
        assert files[0].issues_fixed == 0
        assert external == []

    @pytest.mark.asyncio
    async def test_sleeps_between_iterations_only(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """Waits 5s between analyses, not after the last one."""
        issues = [[_issue("i", 10)]] * 2
        with _loop_env(orchestrator, mock_client, temp_project, [_diff({10})] * 2, issues, fixed_issues=1) as h:
            await _run_loop(orchestrator, temp_project, max_iterations=2)

        h.sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_min_severity_below_threshold_is_not_in_scope_work(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """Issues filtered out by min_severity do not keep the loop running or call fix_file."""
        with _loop_env(orchestrator, mock_client, temp_project, [_diff({10})], [[_issue("i", 10)]]) as h:
            await _run_loop(orchestrator, temp_project, max_iterations=5, min_severity="BLOCKER")

        h.fixer.fix_file.assert_not_awaited()
        assert h.analysis.await_count == 1

    @pytest.mark.asyncio
    async def test_analysis_failure_returns_failed_result(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """A failed analysis yields a failed CleanupPrResult carrying the analysis result."""
        with (
            _preconditions_pass(orchestrator),
            patch.object(orchestrator.branch_analyzer, "get_modified_files", return_value=[_FILE]),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project_with_settings",
                new_callable=AsyncMock,
                return_value=temp_project,
            ),
            patch.object(orchestrator.project_manager, "delete_project", new_callable=AsyncMock) as mock_delete,
            _loop_env(orchestrator, mock_client, temp_project, [], analysis_ok=False),
        ):
            result = await orchestrator.cleanup_pr(max_iterations=2)

        assert result.success is False
        assert "Analysis failed at iteration 1" in (result.error_message or "")
        assert result.analysis_result is not None
        assert result.files_processed[0].file_path == _FILE
        mock_delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unexpected_exception_keeps_partial_progress(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """A mid-run exception still reports fixes committed before it."""
        with (
            _preconditions_pass(orchestrator),
            patch.object(orchestrator.branch_analyzer, "get_modified_files", return_value=[_FILE]),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project_with_settings",
                new_callable=AsyncMock,
                return_value=temp_project,
            ),
            patch.object(orchestrator.project_manager, "delete_project", new_callable=AsyncMock),
            _loop_env(
                orchestrator,
                mock_client,
                temp_project,
                [_diff({10, 11}, {10, 11})],
                [[_issue("in1", 11)]],
                fixed_issues=2,
            ) as env,
        ):
            env.sleep.side_effect = RuntimeError("dirty tree")
            result = await orchestrator.cleanup_pr(max_iterations=2)

        assert result.success is False
        assert "Cleanup failed: dirty tree" in (result.error_message or "")
        assert result.files_processed[0].file_path == _FILE
        assert result.total_issues_fixed == 2

    @pytest.mark.asyncio
    async def test_component_not_found_skips_file(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """ComponentNotFoundError means 'not in the analysis': skip, do not fail."""
        with _loop_env(orchestrator, mock_client, temp_project, [_diff({10})]) as h:
            mock_client.get_issues_for_file = AsyncMock(side_effect=ComponentNotFoundError("nope"))
            h.dup_client.get_duplications_for_file = AsyncMock(side_effect=ComponentNotFoundError("nope"))
            files, _, _ = await _run_loop(orchestrator, temp_project)

        assert files[0].success is True
        h.fixer.fix_file.assert_not_awaited()
        h.dedupe.dedupe_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dry_run_passes_flag_and_runs_one_round(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """Dry-run analyzes and scopes but hands dry_run=True down; only one round runs."""
        pk = temp_project.project_key
        with _loop_env(
            orchestrator,
            mock_client,
            temp_project,
            [_diff({10})] * 3,
            issues=[[_issue("i", 10)]] * 3,
            dup_responses=[_dup_response(pk, 9, 3)] * 3,
        ) as h:
            await _run_loop(orchestrator, temp_project, max_iterations=3, dry_run=True)

        assert h.dedupe.dedupe_file.call_args.kwargs["dry_run"] is True
        assert h.fixer.fix_file.call_args.kwargs["dry_run"] is True
        assert h.analysis.await_count == 1
        h.sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_flag_off_never_queries_real_project(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """Flag off: only the temp project is analyzed/queried, never the real project."""
        with (
            _preconditions_pass(orchestrator),
            patch.object(orchestrator.branch_analyzer, "get_modified_files", return_value=[_FILE]),
            patch.object(
                orchestrator.project_manager,
                "create_temp_project_with_settings",
                new_callable=AsyncMock,
                return_value=temp_project,
            ),
            patch.object(orchestrator.project_manager, "delete_project", new_callable=AsyncMock),
            _loop_env(orchestrator, mock_client, temp_project, [_diff({10})]) as h,
        ):
            real_key = orchestrator.config.sonarqube_project_key
            issue_keys: list[str] = []
            dup_keys: list[str] = []

            def issues_side_effect(_path: str) -> list[SonarQubeIssue]:
                issue_keys.append(mock_client.config.sonarqube_project_key)
                return []

            def dup_query_side_effect(_path: str) -> DuplicationsResponse:
                dup_keys.append(orchestrator.config.sonarqube_project_key)
                return DuplicationsResponse()

            def dup_client_factory(cfg: VibeHealConfig) -> MagicMock:
                dup_keys.append(cfg.sonarqube_project_key)
                cm = MagicMock()
                cm.__aenter__ = AsyncMock(return_value=h.dup_client)
                cm.__aexit__ = AsyncMock(return_value=None)
                return cm

            mock_client.get_issues_for_file = AsyncMock(side_effect=issues_side_effect)
            h.dup_client.get_duplications_for_file = AsyncMock(side_effect=dup_query_side_effect)
            with patch(f"{_MODULE}.DuplicationClient", side_effect=dup_client_factory):
                result = await orchestrator.cleanup_pr(include_main_duplications=False)

        assert result.success is True
        temp_key = temp_project.project_key
        assert real_key != temp_key
        # Every issue and duplication query ran while the temp key was active.
        assert issue_keys
        assert set(issue_keys) == {temp_key}
        assert dup_keys
        assert set(dup_keys) == {temp_key}
        # Every scanner run targeted the temp project.
        assert h.analysis.await_args_list
        assert all(c.kwargs["project_key"] == temp_key for c in h.analysis.await_args_list)
        # No other client call (e.g. project_exists on the real key) was made.
        assert {c[0] for c in mock_client.method_calls} == {"get_issues_for_file"}
        assert result.total_main_duplications_fixed == 0
        assert orchestrator.config.sonarqube_project_key == "my-project"

    @pytest.mark.asyncio
    async def test_failed_fixes_mark_file_unsuccessful(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        """A fix_file summary with failures marks the file result as failed."""
        with _loop_env(orchestrator, mock_client, temp_project, [_diff({10})], [[_issue("i", 10)]]) as h:
            h.fixer.fix_file = AsyncMock(return_value=FixSummary(total_issues=1, failed=1))
            files, _, _ = await _run_loop(orchestrator, temp_project, max_iterations=1)

        assert files[0].success is False
        assert files[0].error_message is not None
