"""Tests for the cleanup-pr baseline scan (FR-2 step 3, --include-main-duplications)."""

import shutil
import subprocess
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dotharness.vibe_heal.ai_tools.base import AITool
from dotharness.vibe_heal.cleanup_pr.orchestrator import CleanupPrOrchestrator
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.git.branch_analyzer import BranchAnalyzer
from dotharness.vibe_heal.git.exceptions import GitOperationError
from dotharness.vibe_heal.sonarqube.analysis_runner import AnalysisResult, AnalysisRunner
from dotharness.vibe_heal.sonarqube.client import SonarQubeClient
from dotharness.vibe_heal.sonarqube.project_manager import TempProjectMetadata
from tests.vibe_heal.cleanup_pr.test_orchestrator import _git_result, _preconditions_pass

_FILE = Path("src/a.py")


@pytest.fixture
def config() -> VibeHealConfig:
    return VibeHealConfig(
        sonarqube_url="https://sonar.test.com",
        sonarqube_token="test-token",  # noqa: S106
        sonarqube_project_key="my-project",
    )


@pytest.fixture
def orchestrator(config: VibeHealConfig) -> CleanupPrOrchestrator:
    client = AsyncMock(spec=SonarQubeClient)
    client.config = MagicMock()
    orch = CleanupPrOrchestrator(config, client, MagicMock(spec=AITool))
    orch.analysis_runner = AsyncMock(spec=AnalysisRunner)
    return orch


@pytest.fixture
def temp_project() -> TempProjectMetadata:
    return TempProjectMetadata(
        project_key="tmp-key",
        project_name="tmp-name",
        created_at="2024-01-01T00:00:00Z",
        base_project_key="my-project",
        branch_name="feature",
        user_email="u@example.com",
    )


class _Env:
    """Records ordering of baseline / worktree / temp-project events."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.worktree_cmds: list[list[str]] = []
        self.dirs: list[Path] = []


def _setup(
    orchestrator: CleanupPrOrchestrator,
    temp_project: TempProjectMetadata,
    baseline: AnalysisResult | Exception,
    files: list[Path] | None = None,
) -> tuple[_Env, AsyncMock, AsyncMock]:
    env = _Env()

    async def fake_analysis(**kwargs: object) -> AnalysisResult:
        env.events.append("baseline")
        env.dirs.append(Path(str(kwargs["project_dir"])))
        if isinstance(baseline, Exception):
            raise baseline
        return baseline

    orchestrator.analysis_runner.run_analysis = AsyncMock(side_effect=fake_analysis)  # type: ignore[method-assign]

    async def fake_create(**kwargs: object) -> TempProjectMetadata:
        env.events.append("create_temp")
        return temp_project

    create = AsyncMock(side_effect=fake_create)
    delete = AsyncMock()
    loop = AsyncMock(return_value=([], None, []))
    orchestrator.project_manager.create_temp_project_with_settings = create  # type: ignore[method-assign]
    orchestrator.project_manager.delete_project = delete  # type: ignore[method-assign]
    orchestrator._run_iteration_loop = loop  # type: ignore[method-assign]
    orchestrator.branch_analyzer.get_modified_files = MagicMock(  # type: ignore[method-assign]
        return_value=[_FILE] if files is None else files
    )
    orchestrator.branch_analyzer.get_current_branch = MagicMock(return_value="feature")  # type: ignore[method-assign]
    orchestrator.branch_analyzer.get_user_email = MagicMock(return_value="u@example.com")  # type: ignore[method-assign]

    return env, create, delete


def _ok() -> AnalysisResult:
    return AnalysisResult(success=True, task_id="t", dashboard_url="http://d")


def _fail() -> AnalysisResult:
    return AnalysisResult(success=False, error_message="scan broke")


def _patch_subprocess(
    env: _Env,
    worktree_add_rc: int = 0,
    worktree_remove_rc: int = 0,
    worktree_remove_exc: Exception | None = None,
) -> AbstractContextManager[Any]:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if "worktree" in cmd:
            env.worktree_cmds.append(cmd)
            if "add" in cmd:
                env.events.append("worktree_add")
                return _git_result(worktree_add_rc, stderr=b"fatal: bad ref" if worktree_add_rc else b"")
            env.events.append("worktree_remove")
            if worktree_remove_exc is not None:
                raise worktree_remove_exc
            return _git_result(worktree_remove_rc)
        if cmd == ["git", "remote"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"origin\n", stderr=b"")
        if "fetch" in cmd or "merge-base" in cmd:
            return _git_result(0)
        msg = f"Unexpected command {cmd}"
        raise AssertionError(msg)

    return patch("dotharness.vibe_heal.git.branch_analyzer.subprocess.run", side_effect=fake_run)


class TestBaselineScan:
    @pytest.mark.asyncio
    async def test_flag_on_scans_once_with_real_key_and_worktree(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        env, _, delete = _setup(orchestrator, temp_project, _ok())
        with _preconditions_pass(orchestrator), _patch_subprocess(env):
            result = await orchestrator.cleanup_pr(base_branch="origin/dev", include_main_duplications=True)

        assert result.success is True
        run_analysis = cast("AsyncMock", orchestrator.analysis_runner.run_analysis)
        assert run_analysis.await_count == 1
        assert run_analysis.await_args is not None
        kwargs = run_analysis.await_args.kwargs
        assert kwargs["project_key"] == "my-project"
        assert kwargs["project_dir"] == env.dirs[0]
        assert kwargs["project_dir"] != Path.cwd()
        add_cmd = env.worktree_cmds[0]
        assert add_cmd[:4] == ["git", "worktree", "add", "--detach"]
        assert add_cmd[-1] == "origin/dev"
        assert add_cmd[4] == str(env.dirs[0])
        remove_cmd = env.worktree_cmds[1]
        assert remove_cmd[:3] == ["git", "worktree", "remove"]
        assert remove_cmd[-1] == str(env.dirs[0])
        assert not env.dirs[0].exists()
        # Ordering: worktree -> baseline -> remove, all before the temp project.
        assert env.events == ["worktree_add", "baseline", "worktree_remove", "create_temp"]
        delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_runs_in_dry_run(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        env, _, _ = _setup(orchestrator, temp_project, _ok())
        with _preconditions_pass(orchestrator), _patch_subprocess(env):
            result = await orchestrator.cleanup_pr(include_main_duplications=True, dry_run=True)

        assert result.success is True
        assert cast("AsyncMock", orchestrator.analysis_runner.run_analysis).await_count == 1

    @pytest.mark.asyncio
    async def test_flag_off_no_baseline_no_worktree(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        env, _, _ = _setup(orchestrator, temp_project, _ok())
        with _preconditions_pass(orchestrator), _patch_subprocess(env):
            result = await orchestrator.cleanup_pr(include_main_duplications=False)

        assert result.success is True
        assert env.worktree_cmds == []
        assert cast("AsyncMock", orchestrator.analysis_runner.run_analysis).await_count == 0
        assert env.events == ["create_temp"]
        # No SonarQube query may reference the real project key: assert the client
        # mock (and every child method mock) received zero calls, not just that the
        # client object was never invoked as a callable.
        assert cast("AsyncMock", orchestrator.client).mock_calls == []

    @pytest.mark.asyncio
    async def test_analysis_failure_returns_failed_result_and_removes_worktree(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        env, create, delete = _setup(orchestrator, temp_project, _fail())
        with _preconditions_pass(orchestrator), _patch_subprocess(env):
            result = await orchestrator.cleanup_pr(include_main_duplications=True)

        assert result.success is False
        assert "Baseline scan failed: scan broke" in (result.error_message or "")
        assert result.temp_project is None
        assert env.events == ["worktree_add", "baseline", "worktree_remove"]
        assert not env.dirs[0].exists()
        create.assert_not_awaited()
        delete.assert_not_awaited()
        cast("AsyncMock", orchestrator._run_iteration_loop).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_analysis_exception_removes_worktree(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        env, create, _ = _setup(orchestrator, temp_project, RuntimeError("kaboom"))
        with _preconditions_pass(orchestrator), _patch_subprocess(env):
            result = await orchestrator.cleanup_pr(include_main_duplications=True)

        assert result.success is False
        assert "kaboom" in (result.error_message or "")
        assert env.events == ["worktree_add", "baseline", "worktree_remove"]
        assert not env.dirs[0].exists()
        create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_worktree_add_failure_is_failed_result(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        env, create, delete = _setup(orchestrator, temp_project, _ok())
        leaked_dir = Path(tempfile.mkdtemp(prefix="vibe-heal-baseline-test-"))
        with (
            _preconditions_pass(orchestrator),
            _patch_subprocess(env, worktree_add_rc=128),
            patch("dotharness.vibe_heal.git.branch_analyzer.tempfile.mkdtemp", return_value=str(leaked_dir)),
        ):
            result = await orchestrator.cleanup_pr(include_main_duplications=True)

        assert result.success is False
        assert "bad ref" in (result.error_message or "")
        assert cast("AsyncMock", orchestrator.analysis_runner.run_analysis).await_count == 0
        # No worktree was created, so none is removed via git.
        assert [c[2] for c in env.worktree_cmds] == ["add"]
        # With created=False the rmtree fallback is the only thing that drops the mkdtemp dir.
        assert not leaked_dir.exists()
        create.assert_not_awaited()
        delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_worktree_remove_failure_is_warning_not_error(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        env, create, delete = _setup(orchestrator, temp_project, _ok())
        with (
            _preconditions_pass(orchestrator),
            _patch_subprocess(env, worktree_remove_rc=1),
            patch("dotharness.vibe_heal.git.branch_analyzer.warn") as warn,
            patch("dotharness.vibe_heal.git.branch_analyzer.shutil.rmtree", side_effect=shutil.rmtree) as rmtree,
        ):
            result = await orchestrator.cleanup_pr(include_main_duplications=True)

        assert result.success is True
        assert env.events == ["worktree_add", "baseline", "worktree_remove", "create_temp"]
        warn.assert_called_once()
        assert "Failed to remove worktree" in str(warn.call_args)
        # The rmtree fallback still drops the mkdtemp dir even though the git remove failed.
        rmtree.assert_called_once_with(env.dirs[0], ignore_errors=True)
        assert not env.dirs[0].exists()
        create.assert_awaited_once()
        delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_worktree_remove_oserror_is_warning_not_error(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        env, create, delete = _setup(orchestrator, temp_project, _ok())
        with (
            _preconditions_pass(orchestrator),
            _patch_subprocess(env, worktree_remove_exc=OSError("disk full")),
            patch("dotharness.vibe_heal.git.branch_analyzer.warn") as warn,
            patch("dotharness.vibe_heal.git.branch_analyzer.shutil.rmtree", side_effect=shutil.rmtree) as rmtree,
        ):
            result = await orchestrator.cleanup_pr(include_main_duplications=True)

        assert result.success is True
        assert env.events == ["worktree_add", "baseline", "worktree_remove", "create_temp"]
        warn.assert_called_once()
        assert "disk full" in str(warn.call_args)
        # The rmtree fallback still drops the mkdtemp dir even though the git remove raised.
        rmtree.assert_called_once_with(env.dirs[0], ignore_errors=True)
        assert not env.dirs[0].exists()
        create.assert_awaited_once()
        delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_modified_files_skips_baseline(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        env, _, _ = _setup(orchestrator, temp_project, _ok(), files=[])
        with _preconditions_pass(orchestrator), _patch_subprocess(env):
            result = await orchestrator.cleanup_pr(include_main_duplications=True)

        assert result.success is True
        assert env.events == []

    @pytest.mark.asyncio
    async def test_temp_project_deleted_when_later_step_fails(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        env, _, delete = _setup(orchestrator, temp_project, _ok())
        orchestrator._run_iteration_loop = AsyncMock(side_effect=Exception("boom"))  # type: ignore[method-assign]
        with _preconditions_pass(orchestrator), _patch_subprocess(env):
            result = await orchestrator.cleanup_pr(include_main_duplications=True)

        assert result.success is False
        delete.assert_awaited_once()

    def test_worktree_add_oserror_raises_git_operation_error(self) -> None:
        analyzer = BranchAnalyzer(Path.cwd())
        with (
            patch("dotharness.vibe_heal.git.branch_analyzer.subprocess.run", side_effect=OSError("no git")),
            pytest.raises(GitOperationError, match="no git"),
            analyzer.temporary_worktree("origin/main"),
        ):
            pass
