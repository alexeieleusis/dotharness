"""Tests for main-duplication detection, prompt and commits (FR-6, --include-main-duplications)."""

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from git import Repo

from dotharness.vibe_heal.ai_tools.base import AITool
from dotharness.vibe_heal.ai_tools.models import FixResult
from dotharness.vibe_heal.cleanup_pr.main_duplications import (
    MainDuplicationTask,
    build_main_duplication_commit_message,
    build_main_duplication_prompt,
    build_main_duplication_task,
    extract_branch_hunks,
    format_block_snippet,
    read_main_file_lines,
)
from dotharness.vibe_heal.cleanup_pr.orchestrator import (
    CleanupPrAnalysisError,
    CleanupPrOrchestrator,
    FileCleanupPrResult,
)
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.deduplication.models import DuplicationsResponse
from dotharness.vibe_heal.git.diff_parser import DiffLines
from dotharness.vibe_heal.git.exceptions import DirtyWorkingDirectoryError
from dotharness.vibe_heal.models import FixSummary
from dotharness.vibe_heal.review.models import DuplicationLocation, ResolvedDuplication
from dotharness.vibe_heal.sonarqube.analysis_runner import AnalysisRunner
from dotharness.vibe_heal.sonarqube.client import SonarQubeClient
from dotharness.vibe_heal.sonarqube.project_manager import TempProjectMetadata
from tests.vibe_heal.cleanup_pr.test_orchestrator import _diff, _dup_response, _loop_env, _preconditions_pass

_MODULE = "dotharness.vibe_heal.cleanup_pr.orchestrator"
_FILE = Path("src/a.py")


@pytest.fixture
def config() -> VibeHealConfig:
    return VibeHealConfig(
        sonarqube_url="https://sonar.test.com",
        sonarqube_token="test-token",  # noqa: S106
        sonarqube_project_key="my-project",
    )


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock(spec=SonarQubeClient)
    client.config = MagicMock()
    return client


@pytest.fixture
def ai_tool() -> MagicMock:
    tool = MagicMock(spec=AITool)
    tool.fix_duplication = AsyncMock(return_value=FixResult(success=True))
    tool.tool_type = MagicMock(display_name="Claude Code")
    return tool


@pytest.fixture
def orchestrator(config: VibeHealConfig, mock_client: AsyncMock, ai_tool: MagicMock) -> CleanupPrOrchestrator:
    orch = CleanupPrOrchestrator(config, mock_client, ai_tool)
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


def _resolved(main_from: int = 5, main_to: int = 9, anchor: int = 20) -> ResolvedDuplication:
    return ResolvedDuplication(
        main_from_line=main_from,
        main_to_line=main_to,
        anchor_new_line=anchor,
        other_locations=[DuplicationLocation(file_path="src/b.py", from_line=1, to_line=5)],
    )


def _result() -> FileCleanupPrResult:
    return FileCleanupPrResult(file_path=_FILE, success=True)


def _task(resolved: ResolvedDuplication | None = None, file_path: Path = _FILE) -> MainDuplicationTask:
    return MainDuplicationTask(
        file_path=file_path,
        repo_relative=file_path.as_posix(),
        resolved=resolved or _resolved(),
        main_block_text="Code block:\n5: old_code()",
        branch_hunk="@@ -6 +20 @@\n-old\n+new",
        branch_range=(20, 20),
    )


class TestReadMainFileLines:
    def test_splits_on_newline_only(self) -> None:
        repo = MagicMock()
        repo.git.show.return_value = "a\fb\u2028c\nline2\r\nline3\n"
        assert read_main_file_lines(repo, "abc", "x.py") == ["a\fb\u2028c", "line2", "line3"]


class TestFormatBlockSnippet:
    def test_short_block_shown_in_full(self) -> None:
        lines = [f"l{i}" for i in range(1, 11)]
        text = format_block_snippet(lines, 2, 5)
        assert text == "Code block:\n2: l2\n3: l3\n4: l4\n5: l5"

    def test_six_line_block_still_full(self) -> None:
        lines = [f"l{i}" for i in range(1, 11)]
        assert "omitted" not in format_block_snippet(lines, 1, 6)

    def test_long_block_uses_first_and_last_three(self) -> None:
        lines = [f"l{i}" for i in range(1, 21)]
        text = format_block_snippet(lines, 3, 12)  # 10 lines
        assert "3: l3" in text
        assert "5: l5" in text
        assert "6: l6" not in text
        assert "(4 lines omitted)" in text
        assert "10: l10" in text
        assert "12: l12" in text
        assert "9: l9" not in text


_DIFF = (
    "diff --git a/src/a.py b/src/a.py\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -6,2 +6,1 @@\n"
    "-old1\n"
    "-old2\n"
    "+new1\n"
    "@@ -50 +49,2 @@\n"
    "-far\n"
    "+far1\n"
    "+far2\n"
)


class TestExtractBranchHunks:
    def test_keeps_only_hunk_intersecting_main_block(self) -> None:
        result = extract_branch_hunks(_DIFF, 5, 9, 6)
        assert result is not None
        text, rng = result
        assert "old1" in text
        assert "far" not in text
        assert text.startswith("--- a/src/a.py")
        assert rng == (6, 6)

    def test_keeps_hunk_containing_anchor(self) -> None:
        result = extract_branch_hunks(_DIFF, 100, 110, 50)
        assert result is not None
        text, rng = result
        assert "far1" in text
        assert "old1" not in text
        assert rng == (49, 50)

    def test_no_matching_hunk_returns_none_instead_of_nearest(self) -> None:
        # Neither hunk touches [100, 110] on the old side nor contains anchor 45 on the
        # new side. The former nearest-hunk fallback kept the unrelated "far" hunk; now
        # the group must be skipped so it is counted in main_duplications_skipped.
        assert extract_branch_hunks(_DIFF, 100, 110, 45) is None

    def test_unrelated_far_hunk_is_not_kept(self) -> None:
        # P1 repro from review: main block 10-30, the only branch hunk is at line 400 and
        # the anchor (12) is not in it. Keeping that hunk would re-apply an unrelated edit.
        far_only = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -400,1 +400,1 @@\n-old\n+new\n"
        assert extract_branch_hunks(far_only, 10, 30, 12) is None

    def test_no_hunks_returns_none(self) -> None:
        assert extract_branch_hunks("", 1, 2, 1) is None


class TestPromptAndCommit:
    def test_prompt_contents_and_instruction_order(self) -> None:
        prompt = build_main_duplication_prompt(_task())
        assert "5: old_code()" in prompt
        assert "src/b.py (lines 1-5)" in prompt
        assert "@@ -6 +20 @@" in prompt
        a = prompt.index("shared helper")
        b = prompt.index("re-apply the branch's change")
        c = prompt.index("Do not change behavior")
        assert a < b < c

    def test_commit_message(self) -> None:
        msg = build_main_duplication_commit_message(_task(), "Claude Code")
        first = msg.splitlines()[0]
        assert first == "refactor: [duplication] extract shared code from removed main duplication at line 5"
        assert "src/a.py (lines 5-9, 5 lines)" in msg
        assert "src/b.py (lines 1-5)" in msg
        assert "lines 20-20" in msg
        assert "AI tool: Claude Code" in msg
        assert msg.endswith("[vibe_heal](https://github.com/alexeieleusis/dotharness)")
        assert "Co-Authored-By" not in msg


class TestBuildTaskFromGit:
    def test_reads_main_text_and_branch_hunk(self, tmp_path: Path) -> None:
        repo = Repo.init(tmp_path)
        repo.git.config("user.email", "t@example.com")
        repo.git.config("user.name", "T")
        src = tmp_path / "a.py"
        src.write_text("".join(f"line{i}\n" for i in range(1, 11)))
        repo.git.add("a.py")
        repo.git.commit("-m", "base")
        merge_base = repo.head.commit.hexsha
        src.write_text("".join(f"line{i}\n" for i in range(1, 11)).replace("line6\n", "changed6\n"))
        repo.git.add("a.py")
        repo.git.commit("-m", "branch")

        task = build_main_duplication_task(repo, merge_base, Path("a.py"), "a.py", _resolved(4, 7, 6))

        assert task is not None
        assert "4: line4" in task.main_block_text
        assert "6: line6" in task.main_block_text
        assert "+changed6" in task.branch_hunk
        assert "-line6" in task.branch_hunk
        assert task.branch_range == (6, 6)

    def test_missing_file_at_merge_base_returns_none(self, tmp_path: Path) -> None:
        repo = Repo.init(tmp_path)
        repo.git.config("user.email", "t@example.com")
        repo.git.config("user.name", "T")
        (tmp_path / "x.py").write_text("x\n")
        repo.git.add("x.py")
        repo.git.commit("-m", "base")

        assert build_main_duplication_task(repo, repo.head.commit.hexsha, Path("new.py"), "new.py", _resolved()) is None


def _diff_with_old(new: set[int], old: set[int], rel: str = "src/a.py") -> DiffLines:
    base = _diff(new, rel=rel)
    return DiffLines(new_lines=base.new_lines, old_lines={rel: old}, strict_new_lines=base.strict_new_lines)


@contextmanager
def _main_env(
    orchestrator: CleanupPrOrchestrator,
    mock_client: AsyncMock,
    temp_project: TempProjectMetadata,
    diffs: list[DiffLines],
    tasks: list[MainDuplicationTask] | None = None,
    commit_results: list[str | None] | None = None,
    rebuild_tasks: list[MainDuplicationTask] | None = None,
) -> Generator[MagicMock]:
    """Loop harness plus mocks for the main-duplication phase (detection patched out).

    ``rebuild_tasks`` stands in for the lazy task rebuild from the current HEAD that runs
    right before each AI call (matched to the task being fixed by ``resolved``); it
    defaults to the detection tasks. An empty list simulates no branch diff remaining.
    """
    events: list[str] = []
    detection_tasks = tasks if tasks is not None else []
    rebuild_source = rebuild_tasks if rebuild_tasks is not None else detection_tasks

    def rebuild(
        _repo: object, _merge_base: str, _file_path: Path, _repo_relative: str, resolved: object
    ) -> MainDuplicationTask | None:
        for t in rebuild_source:
            if t.resolved == resolved:
                return t
        return None

    with (
        _loop_env(orchestrator, mock_client, temp_project, diffs) as h,
        patch.object(orchestrator, "_detect_main_duplications", new_callable=AsyncMock) as detect,
        patch.object(orchestrator.branch_analyzer, "repo", MagicMock()) as repo,
        patch.object(orchestrator.git_manager, "require_clean_working_directory") as clean,
        patch.object(orchestrator.git_manager, "create_commit", side_effect=commit_results or ["sha"] * 5) as commit,
        patch(f"{_MODULE}.build_main_duplication_task", side_effect=rebuild) as build,
    ):
        detect.return_value = detection_tasks
        repo.git.merge_base.return_value = "mergebase\n"
        repo.git.diff.return_value = ""
        original_analysis = h.analysis.side_effect

        async def analysis_spy(**kwargs: object) -> object:
            events.append("analysis")
            if original_analysis:
                return await original_analysis(**kwargs)
            return h.analysis.return_value

        h.analysis.side_effect = analysis_spy
        h.dedupe.dedupe_file = AsyncMock(return_value=FixSummary(total_issues=0))
        env = MagicMock()
        env.h, env.detect, env.repo, env.clean, env.commit, env.events = h, detect, repo, clean, commit, events
        env.build = build
        yield env


async def _loop(
    orchestrator: CleanupPrOrchestrator,
    temp_project: TempProjectMetadata,
    dry_run: bool = False,
    max_iterations: int = 1,
) -> tuple[list[FileCleanupPrResult], object, list[Path]]:
    return await orchestrator._run_iteration_loop(
        modified_files=[_FILE],
        temp_project=temp_project,
        base_branch="origin/main",
        max_iterations=max_iterations,
        min_severity=None,
        dry_run=dry_run,
        verbose=True,
        include_main_duplications=True,
        original_project_key="my-project",
    )


class TestMainDuplicationPhase:
    @pytest.mark.asyncio
    async def test_success_commits_and_counts(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
        temp_project: TempProjectMetadata,
        ai_tool: MagicMock,
    ) -> None:
        diffs = [_diff_with_old({20}, {6})] * 3
        with _main_env(orchestrator, mock_client, temp_project, diffs, tasks=[_task()]) as env:
            files, _, _ = await _loop(orchestrator, temp_project)

        ai_tool.fix_duplication.assert_awaited_once()
        prompt, path = ai_tool.fix_duplication.await_args.args
        assert "old_code()" in prompt
        assert path == "src/a.py"
        msg = env.commit.call_args.args[0]
        assert msg.startswith("refactor: [duplication] extract shared code from removed main duplication at line 5")
        assert env.commit.call_args.kwargs == {"include_untracked": True}
        assert env.clean.called
        assert files[0].main_duplications_fixed == 1
        assert files[0].main_duplications_skipped == 0
        assert files[0].success is True

    @pytest.mark.asyncio
    async def test_commit_makes_analysis_stale_so_loop_reanalyzes(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        with _main_env(orchestrator, mock_client, temp_project, [_diff_with_old({20}, {6})] * 3, [_task()]) as env:
            await _loop(orchestrator, temp_project)
        # one analysis for detection, one fresh analysis for the loop's first iteration
        assert env.events.count("analysis") == 2

    @pytest.mark.asyncio
    async def test_prompt_uses_fresh_diff_reread_from_head(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
        temp_project: TempProjectMetadata,
        ai_tool: MagicMock,
    ) -> None:
        original = _task()
        fresh = MainDuplicationTask(
            file_path=original.file_path,
            repo_relative=original.repo_relative,
            resolved=original.resolved,
            main_block_text=original.main_block_text,
            branch_hunk="@@ -6 +40 @@\n-fresh\n+fresh-new",
            branch_range=(40, 40),
        )
        with _main_env(
            orchestrator,
            mock_client,
            temp_project,
            [_diff_with_old({20}, {6})] * 3,
            tasks=[original],
            rebuild_tasks=[fresh],
        ) as env:
            files, _, _ = await _loop(orchestrator, temp_project)

        env.build.assert_called_once()
        ai_tool.fix_duplication.assert_awaited_once()
        prompt, _ = ai_tool.fix_duplication.await_args.args
        assert "+fresh-new" in prompt
        assert "lines 40-40" in prompt
        assert "-old" not in prompt
        assert "lines 40-40" in env.commit.call_args.args[0]
        assert files[0].main_duplications_fixed == 1

    @pytest.mark.asyncio
    async def test_no_fresh_diff_after_earlier_fix_is_skipped(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
        temp_project: TempProjectMetadata,
        ai_tool: MagicMock,
    ) -> None:
        with _main_env(
            orchestrator,
            mock_client,
            temp_project,
            [_diff_with_old({20}, {6})] * 3,
            tasks=[_task()],
            rebuild_tasks=[],
        ) as env:
            files, _, _ = await _loop(orchestrator, temp_project)

        ai_tool.fix_duplication.assert_not_awaited()
        env.commit.assert_not_called()
        assert files[0].main_duplications_skipped == 1
        assert files[0].main_duplications_fixed == 0
        assert files[0].success is True
        assert env.events.count("analysis") == 1  # nothing committed: analysis still fresh

    @pytest.mark.asyncio
    async def test_no_tasks_reuses_analysis(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        with _main_env(orchestrator, mock_client, temp_project, [_diff_with_old({20}, {6})] * 3, []) as env:
            await _loop(orchestrator, temp_project)
        assert env.events.count("analysis") == 1

    @pytest.mark.asyncio
    async def test_main_duplications_run_before_active_duplications(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
        temp_project: TempProjectMetadata,
        ai_tool: MagicMock,
    ) -> None:
        order: list[str] = []

        async def fix(*_a: object) -> FixResult:
            order.append("main")
            return FixResult(success=True)

        ai_tool.fix_duplication = AsyncMock(side_effect=fix)
        with _main_env(orchestrator, mock_client, temp_project, [_diff_with_old({20}, {6})] * 3, [_task()]) as env:

            async def dedupe(**kwargs: object) -> FixSummary:
                order.append("active")
                return FixSummary(total_issues=0)

            env.h.dedupe.dedupe_file = AsyncMock(side_effect=dedupe)
            # give the file an in-scope active duplication so the active phase runs
            env.h.dup_client.get_duplications_for_file = AsyncMock(return_value=_dup_response("tmp-key", 18))
            await _loop(orchestrator, temp_project)

        assert order[0] == "main"
        assert "active" in order

    @pytest.mark.asyncio
    async def test_failed_ai_attempt_counts_failed_and_no_commit(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
        temp_project: TempProjectMetadata,
        ai_tool: MagicMock,
    ) -> None:
        ai_tool.fix_duplication = AsyncMock(return_value=FixResult(success=False, error_message="nope"))
        with _main_env(orchestrator, mock_client, temp_project, [_diff_with_old({20}, {6})] * 3, [_task()]) as env:
            files, _, _ = await _loop(orchestrator, temp_project)

        env.commit.assert_not_called()
        assert files[0].main_duplications_fixed == 0
        assert files[0].success is False
        assert env.events.count("analysis") == 1  # nothing committed: analysis still fresh

    @pytest.mark.asyncio
    async def test_dirty_tree_after_failed_attempt_raises_for_next_task(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
        temp_project: TempProjectMetadata,
        ai_tool: MagicMock,
    ) -> None:
        ai_tool.fix_duplication = AsyncMock(return_value=FixResult(success=False, error_message="nope"))
        tasks = [_task(_resolved(30, 34)), _task(_resolved(5, 9))]
        with _main_env(orchestrator, mock_client, temp_project, [_diff_with_old({20}, {6})] * 3, tasks) as env:
            env.clean.side_effect = [None, DirtyWorkingDirectoryError("dirty")]
            with pytest.raises(DirtyWorkingDirectoryError):
                await _loop(orchestrator, temp_project)

        assert ai_tool.fix_duplication.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_commit_counts_skipped(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        with _main_env(
            orchestrator, mock_client, temp_project, [_diff_with_old({20}, {6})] * 3, [_task()], commit_results=[None]
        ) as env:
            files, _, _ = await _loop(orchestrator, temp_project)

        assert files[0].main_duplications_skipped == 1
        assert files[0].main_duplications_fixed == 0
        assert env.events.count("analysis") == 1

    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_ai_or_commit(
        self,
        orchestrator: CleanupPrOrchestrator,
        mock_client: AsyncMock,
        temp_project: TempProjectMetadata,
        ai_tool: MagicMock,
    ) -> None:
        with _main_env(orchestrator, mock_client, temp_project, [_diff_with_old({20}, {6})] * 3, [_task()]) as env:
            files, _, _ = await _loop(orchestrator, temp_project, dry_run=True)

        ai_tool.fix_duplication.assert_not_awaited()
        env.commit.assert_not_called()
        assert files[0].main_duplications_fixed == 1

    @pytest.mark.asyncio
    async def test_external_files_recorded(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        with _main_env(orchestrator, mock_client, temp_project, [_diff_with_old({20}, {6})] * 3, [_task()]) as env:
            env.repo.git.diff.return_value = "src/a.py\nsrc/shared_helper.py\n"
            _, _, external = await _loop(orchestrator, temp_project)

        assert external == [Path("src/shared_helper.py")]

    @pytest.mark.asyncio
    async def test_analysis_failure_raises(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        with (
            _loop_env(orchestrator, mock_client, temp_project, [_diff_with_old({20}, {6})], analysis_ok=False),
            pytest.raises(CleanupPrAnalysisError),
        ):
            await _loop(orchestrator, temp_project)

    @pytest.mark.asyncio
    async def test_flag_off_never_runs_main_phase(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        with (
            _loop_env(orchestrator, mock_client, temp_project, [_diff({10})]),
            patch(f"{_MODULE}.get_resolved_duplications") as review_cls,
            patch.object(orchestrator, "_run_main_duplication_phase", new_callable=AsyncMock) as phase,
        ):
            await orchestrator._run_iteration_loop(
                modified_files=[_FILE],
                temp_project=temp_project,
                base_branch="origin/main",
                max_iterations=1,
                min_severity=None,
                dry_run=False,
                verbose=False,
            )

        phase.assert_not_awaited()
        review_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_requires_original_key(
        self, orchestrator: CleanupPrOrchestrator, mock_client: AsyncMock, temp_project: TempProjectMetadata
    ) -> None:
        with (
            _loop_env(orchestrator, mock_client, temp_project, [_diff({10})]),
            pytest.raises(ValueError, match="original_project_key"),
        ):
            await orchestrator._run_iteration_loop(
                modified_files=[_FILE],
                temp_project=temp_project,
                base_branch="origin/main",
                max_iterations=1,
                min_severity=None,
                dry_run=False,
                verbose=False,
                include_main_duplications=True,
            )


class TestMainDuplicationDetection:
    """``_detect_main_duplications`` against a mocked real-project duplications API."""

    @staticmethod
    @contextmanager
    def _patched(
        orchestrator: CleanupPrOrchestrator,
        real_response: DuplicationsResponse,
        temp_result: tuple[Sequence[object], str | None] = ([], None),
        build_side_effect: object | None = None,
    ) -> Generator[tuple[MagicMock, MagicMock, MagicMock]]:
        dup_client = MagicMock()
        dup_client.get_duplications_for_file = AsyncMock(return_value=real_response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=dup_client)
        cm.__aexit__ = AsyncMock(return_value=None)
        if build_side_effect is None:
            build_side_effect = lambda _r, _m, fp, rel, resolved: _task(resolved, fp)
        build = MagicMock(side_effect=build_side_effect)
        with (
            patch("dotharness.vibe_heal.review.duplication_scope.DuplicationClient", return_value=cm) as client_cls,
            patch.object(orchestrator, "_fetch_duplications", new_callable=AsyncMock, return_value=temp_result),
            patch.object(orchestrator, "_to_repo_relative", side_effect=lambda p: p.as_posix()),
            patch(f"{_MODULE}.build_main_duplication_task", build),
        ):
            yield client_cls, dup_client, build

    @pytest.mark.asyncio
    async def test_queries_real_project_by_repo_relative_path(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        real = _dup_response("my-project", 5)
        diff = _diff_with_old({20}, {6})
        with self._patched(orchestrator, real) as (client_cls, dup_client, _build):
            tasks = await orchestrator._detect_main_duplications(
                _FILE, diff, temp_project, "my-project", "mb", _result(), verbose=True
            )

        assert client_cls.call_args.args[0].sonarqube_project_key == "my-project"
        dup_client.get_duplications_for_file.assert_awaited_once_with("src/a.py")
        assert len(tasks) == 1
        assert (tasks[0].resolved.main_from_line, tasks[0].resolved.main_to_line) == (5, 9)
        assert tasks[0].resolved.anchor_new_line == 20
        assert [loc.file_path for loc in tasks[0].resolved.other_locations] == ["src/b.py"]

    @pytest.mark.asyncio
    async def test_block_not_touching_old_lines_does_not_qualify(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        with self._patched(orchestrator, _dup_response("my-project", 5)):
            tasks = await orchestrator._detect_main_duplications(
                _FILE, _diff_with_old({20}, {40}), temp_project, "my-project", "mb", _result(), verbose=False
            )
        assert tasks == []

    @pytest.mark.asyncio
    async def test_overlapping_active_duplication_suppresses(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        temp = _dup_response("tmp-key", 7)  # active block 7-11 intersects strict line 8
        diff = _diff_with_old({8, 20}, {6})
        with self._patched(orchestrator, _dup_response("my-project", 5), (temp.duplications, "1")):
            tasks = await orchestrator._detect_main_duplications(
                _FILE, diff, temp_project, "my-project", "mb", _result(), verbose=False
            )
        assert tasks == []

    @pytest.mark.asyncio
    async def test_no_old_lines_skips_without_querying(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        with self._patched(orchestrator, _dup_response("my-project", 5)) as (client_cls, _, _build):
            tasks = await orchestrator._detect_main_duplications(
                _FILE, _diff({20}), temp_project, "my-project", "mb", _result(), verbose=False
            )
        assert tasks == []
        client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_tasks_in_reverse_line_order(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        real = DuplicationsResponse.model_validate({
            "duplications": [
                {"blocks": [{"from": 5, "size": 3, "_ref": "1"}, {"from": 1, "size": 3, "_ref": "2"}]},
                {"blocks": [{"from": 30, "size": 3, "_ref": "1"}, {"from": 9, "size": 3, "_ref": "2"}]},
            ],
            "files": {
                "1": {"key": "my-project:src/a.py", "name": "a.py", "projectName": "p"},
                "2": {"key": "my-project:src/b.py", "name": "b.py", "projectName": "p"},
            },
        })
        with self._patched(orchestrator, real):
            tasks = await orchestrator._detect_main_duplications(
                _FILE, _diff_with_old({50}, {6, 31}), temp_project, "my-project", "mb", _result(), verbose=False
            )
        assert [t.resolved.main_from_line for t in tasks] == [30, 5]

    @pytest.mark.asyncio
    async def test_unbuildable_task_is_counted_skipped(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        # P1 fix: when build_main_duplication_task returns None (no branch hunk relates
        # to the main block), the group is skipped and counted in main_duplications_skipped.
        result = _result()
        with self._patched(orchestrator, _dup_response("my-project", 5), build_side_effect=lambda *a: None):
            tasks = await orchestrator._detect_main_duplications(
                _FILE, _diff_with_old({20}, {6}), temp_project, "my-project", "mb", result, verbose=False
            )
        assert tasks == []
        assert result.main_duplications_skipped == 1


class TestCleanupPrPassesThrough:
    @pytest.mark.asyncio
    async def test_cleanup_pr_passes_flag_and_real_key_to_loop(
        self, orchestrator: CleanupPrOrchestrator, temp_project: TempProjectMetadata
    ) -> None:
        loop = AsyncMock(return_value=([], None, []))
        with (
            _preconditions_pass(orchestrator),
            patch.object(orchestrator.branch_analyzer, "get_modified_files", return_value=[_FILE]),
            patch.object(orchestrator, "_run_baseline_scan", new_callable=AsyncMock) as baseline,
            patch.object(
                orchestrator.project_manager,
                "create_temp_project_with_settings",
                new_callable=AsyncMock,
                return_value=temp_project,
            ),
            patch.object(orchestrator.project_manager, "delete_project", new_callable=AsyncMock),
            patch.object(orchestrator, "_run_iteration_loop", loop),
        ):
            baseline.return_value = MagicMock(success=True)
            result = await orchestrator.cleanup_pr(include_main_duplications=True)

        assert result.success is True
        assert loop.call_args.kwargs["include_main_duplications"] is True
        assert loop.call_args.kwargs["original_project_key"] == "my-project"
