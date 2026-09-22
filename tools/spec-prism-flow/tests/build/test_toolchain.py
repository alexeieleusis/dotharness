from pathlib import Path
from unittest.mock import Mock

from conftest import git_commit as _commit
from conftest import init_git_repo as _init_repo

from spec_prism_flow.build import (
    completion_log,
    gh_ops,
    git_ops,
    harness_integration,
    manual_test,
    merge_gates,
    scope_guard,
    vibe_heal_integration,
)
from spec_prism_flow.build.claude_backend import ClaudeBackend
from spec_prism_flow.build.completion_log import CompletionRecord
from spec_prism_flow.build.gh_ops import PRHandle
from spec_prism_flow.build.manual_test import ManualTestOutcome
from spec_prism_flow.build.opencode_backend import OpencodeBackend
from spec_prism_flow.build.toolchain import (
    DiffStat,
    Toolchain,
    build_dry_run_toolchain,
    build_live_toolchain,
    diff_stat,
)
from spec_prism_flow.config import (
    AgentConfig,
    BuildConfig,
    HarnessSection,
    PlanConfig,
    ReviewConfig,
    SpecPrismFlowConfig,
    VibeHealConfig,
)

_EXPECTED_FIELDS = {
    "checkout_fresh_branch",
    "agent_run",
    "commit_all",
    "diff_paths",
    "scope_check",
    "push_branch",
    "fetch_resync",
    "pr_create",
    "pr_view",
    "static_analysis_scan",
    "static_analysis_post",
    "review_self_review",
    "review_address_comments",
    "unresolved_thread_count",
    "manual_test_prompt",
    "diff_stat",
    "merge_gates_run",
    "pr_merge",
    "completion_log_append",
}


def _make_cfg(tmp_path, *, backend: str = "claude", review_enabled: bool = True, vibe_heal_enabled: bool = True):
    return SpecPrismFlowConfig(
        agent=AgentConfig(backend=backend),
        plan=PlanConfig(workspace_dir=tmp_path / "workspace", phase_dir=tmp_path / "phases"),
        review=ReviewConfig(
            enabled=review_enabled,
            tool_dir=tmp_path / "pr-review",
            harness_config=tmp_path / ".harness.toml",
        ),
        vibe_heal=VibeHealConfig(enabled=vibe_heal_enabled, tool_dir=tmp_path / "vibe-heal"),
        build=BuildConfig(state_dir=tmp_path / "state"),
        harness=HarnessSection(knowledge_dir=tmp_path / "knowledge"),
    )


# --- Toolchain dataclass shape -----------------------------------------------------


def test_toolchain_has_exactly_the_documented_fields():
    assert {f.name for f in Toolchain.__dataclass_fields__.values()} == _EXPECTED_FIELDS


# --- build_dry_run_toolchain --------------------------------------------------------


def test_dry_run_commit_all_returns_true_once_then_false():
    toolchain = build_dry_run_toolchain()

    results = [toolchain.commit_all(Path("/x"), "msg") for _ in range(4)]

    assert results == [True, False, False, False]


def test_dry_run_commit_all_counter_is_independent_per_toolchain_instance():
    first = build_dry_run_toolchain()
    second = build_dry_run_toolchain()

    assert first.commit_all(Path("/x"), "msg") is True
    assert second.commit_all(Path("/x"), "msg") is True


def test_dry_run_manual_test_prompt_always_passes_on_first_try():
    toolchain = build_dry_run_toolchain()

    outcome = toolchain.manual_test_prompt(1, "name", ["item"], False)

    assert outcome == ManualTestOutcome(passed=True, retry=False, notes=None)


def test_dry_run_unresolved_thread_count_always_zero():
    toolchain = build_dry_run_toolchain()

    assert toolchain.unresolved_thread_count("acme/repo", 1) == 0


def test_dry_run_pr_view_always_reports_open():
    toolchain = build_dry_run_toolchain()

    status = toolchain.pr_view("acme/repo", 1)

    assert status.state == "OPEN"


def test_dry_run_pr_create_returns_a_fixed_pr_handle():
    toolchain = build_dry_run_toolchain()

    pr = toolchain.pr_create(Path("/x"), "branch", "title", "body")

    assert isinstance(pr, PRHandle)
    assert pr.number == 1


def test_dry_run_diff_stat_returns_a_fixed_diff_stat():
    toolchain = build_dry_run_toolchain()

    stat = toolchain.diff_stat(Path("/x"), "origin/main")

    assert isinstance(stat, DiffStat)


def test_dry_run_static_analysis_and_review_stubs_are_inert_no_ops():
    toolchain = build_dry_run_toolchain()

    assert toolchain.static_analysis_scan(Path("/x")) is None
    toolchain.static_analysis_post(Path("/x"))  # must not raise
    assert toolchain.review_self_review(Path("/x")) == ""
    assert toolchain.review_address_comments(Path("/x")) == ""
    toolchain.checkout_fresh_branch(Path("/x"), "b", "main")  # must not raise
    toolchain.push_branch(Path("/x"), "b")  # must not raise
    toolchain.fetch_resync(Path("/x"), "b")  # must not raise
    toolchain.scope_check(["a"], ["*"], "origin/main")  # must not raise
    toolchain.merge_gates_run(Path("/x"), ["true"])  # must not raise
    toolchain.pr_merge("acme/repo", 1)  # must not raise
    record = CompletionRecord(
        phase_number=1,
        phase_name="x",
        pr_number=1,
        pr_url="https://example.invalid/pull/1",
        pr_opened_at=None,
        pr_merged_at=None,
        manual_test_first_try_pass=True,
        escalation_reason=None,
    )
    toolchain.completion_log_append(Path("/x"), record)  # must not raise


def test_dry_run_agent_run_returns_a_string_without_shelling_out():
    toolchain = build_dry_run_toolchain()

    assert isinstance(toolchain.agent_run("instructions", Path("/x")), str)


# --- build_live_toolchain: direct wiring --------------------------------------------


def test_live_toolchain_wires_git_ops_functions_directly(tmp_path):
    toolchain = build_live_toolchain(_make_cfg(tmp_path))

    assert toolchain.checkout_fresh_branch is git_ops.checkout_fresh_branch
    assert toolchain.commit_all is git_ops.commit_all
    assert toolchain.diff_paths is git_ops.diff_name_only
    assert toolchain.push_branch is git_ops.push_branch
    assert toolchain.fetch_resync is git_ops.fetch_resync


def test_live_toolchain_wires_scope_guard_directly(tmp_path):
    toolchain = build_live_toolchain(_make_cfg(tmp_path))

    assert toolchain.scope_check is scope_guard.check


def test_live_toolchain_wires_gh_ops_functions_directly(tmp_path):
    toolchain = build_live_toolchain(_make_cfg(tmp_path))

    assert toolchain.pr_create is gh_ops.pr_create
    assert toolchain.pr_view is gh_ops.pr_view
    assert toolchain.unresolved_thread_count is gh_ops.unresolved_thread_count
    assert toolchain.pr_merge is gh_ops.pr_merge


def test_live_toolchain_wires_diff_stat_and_merge_gates_directly(tmp_path):
    toolchain = build_live_toolchain(_make_cfg(tmp_path))

    assert toolchain.diff_stat is diff_stat
    assert toolchain.merge_gates_run is merge_gates.run_merge_gates


def test_live_toolchain_agent_run_is_bound_to_the_configured_backend(tmp_path, monkeypatch):
    claude_invoke = Mock(return_value="claude output")
    opencode_invoke = Mock(return_value="opencode output")
    monkeypatch.setattr(ClaudeBackend, "invoke", claude_invoke)
    monkeypatch.setattr(OpencodeBackend, "invoke", opencode_invoke)
    claude_toolchain = build_live_toolchain(_make_cfg(tmp_path, backend="claude"))
    opencode_toolchain = build_live_toolchain(_make_cfg(tmp_path, backend="opencode"))

    assert claude_toolchain.agent_run("instructions", tmp_path) == "claude output"
    opencode_invoke.assert_not_called()

    assert opencode_toolchain.agent_run("instructions", tmp_path) == "opencode output"
    claude_invoke.assert_called_once()


# --- build_live_toolchain: closures over config -------------------------------------


def test_live_toolchain_manual_test_prompt_forwards_strict_as_keyword(tmp_path, monkeypatch):
    prompt_mock = Mock(return_value=ManualTestOutcome(passed=True, retry=False, notes=None))
    monkeypatch.setattr(manual_test, "prompt", prompt_mock)
    toolchain = build_live_toolchain(_make_cfg(tmp_path))

    toolchain.manual_test_prompt(7, "phase-name", ["item"], True)

    prompt_mock.assert_called_once_with(7, "phase-name", ["item"], strict=True)


def test_live_toolchain_static_analysis_scan_wires_vibe_heal_config_and_paths(tmp_path, monkeypatch):
    scan_mock = Mock(return_value={"issues": []})
    monkeypatch.setattr(vibe_heal_integration, "scan", scan_mock)
    cfg = _make_cfg(tmp_path)
    toolchain = build_live_toolchain(cfg)
    clone = tmp_path / "clone"

    report = toolchain.static_analysis_scan(clone)

    assert report == {"issues": []}
    args = scan_mock.call_args.args
    assert args[0] is cfg.vibe_heal
    assert args[1] == clone
    assert cfg.build.state_dir in args[2].parents
    assert cfg.build.state_dir in args[3].parents


def test_live_toolchain_static_analysis_post_wires_vibe_heal_config_and_paths(tmp_path, monkeypatch):
    post_mock = Mock()
    monkeypatch.setattr(vibe_heal_integration, "post", post_mock)
    cfg = _make_cfg(tmp_path)
    toolchain = build_live_toolchain(cfg)
    clone = tmp_path / "clone"

    toolchain.static_analysis_post(clone)

    args = post_mock.call_args.args
    assert args[0] is cfg.vibe_heal
    assert args[1] == clone


def test_live_toolchain_static_analysis_paths_are_outside_the_clone(tmp_path, monkeypatch):
    """Scan artifacts must live outside `clone` -- `commit_all` stages everything
    unconditionally, so a report file written inside the clone would pollute the
    phase's own diff and trip the next cycle's scope check."""
    scan_mock = Mock(return_value=None)
    monkeypatch.setattr(vibe_heal_integration, "scan", scan_mock)
    cfg = _make_cfg(tmp_path)
    toolchain = build_live_toolchain(cfg)
    clone = tmp_path / "clone"

    toolchain.static_analysis_scan(clone)

    report_path = scan_mock.call_args.args[2]
    assert clone not in report_path.parents


def test_live_toolchain_review_self_review_wires_review_config(tmp_path, monkeypatch):
    self_review_mock = Mock(return_value="notes")
    monkeypatch.setattr(harness_integration, "self_review", self_review_mock)
    cfg = _make_cfg(tmp_path)
    toolchain = build_live_toolchain(cfg)
    clone = tmp_path / "clone"

    output = toolchain.review_self_review(clone)

    assert output == "notes"
    self_review_mock.assert_called_once_with(cfg.review, clone)


def test_live_toolchain_review_address_comments_wires_review_config(tmp_path, monkeypatch):
    address_comments_mock = Mock(return_value="fixed")
    monkeypatch.setattr(harness_integration, "address_comments", address_comments_mock)
    cfg = _make_cfg(tmp_path)
    toolchain = build_live_toolchain(cfg)
    clone = tmp_path / "clone"

    output = toolchain.review_address_comments(clone)

    assert output == "fixed"
    address_comments_mock.assert_called_once_with(cfg.review, clone)


def test_live_toolchain_completion_log_append_wires_docs_paths_and_base_branch(tmp_path, monkeypatch):
    append_mock = Mock()
    monkeypatch.setattr(completion_log, "append_and_commit", append_mock)
    toolchain = build_live_toolchain(_make_cfg(tmp_path))
    clone = tmp_path / "clone"
    record = CompletionRecord(
        phase_number=1,
        phase_name="x",
        pr_number=1,
        pr_url="https://example.invalid/pull/1",
        pr_opened_at=None,
        pr_merged_at=None,
        manual_test_first_try_pass=True,
        escalation_reason=None,
    )

    toolchain.completion_log_append(clone, record)

    append_mock.assert_called_once_with(
        clone,
        clone / "docs" / "completion-log.json",
        clone / "docs" / "completion-log.md",
        record,
        base_branch="main",
    )


# --- diff_stat: real scratch git repo ------------------------------------------------


def test_diff_stat_counts_files_and_lines_added_and_removed(tmp_path):
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\nline2\n")
    _commit(clone, "initial")

    (clone / "a.txt").write_text("line1\nline2-changed\nline3\n")
    (clone / "b.txt").write_text("new file\n")
    _commit(clone, "second")

    stat = diff_stat(clone, "HEAD~1")

    assert stat.files == 2
    assert stat.lines_added == 3  # 1 changed line (add) + 1 new line in a.txt + 1 line in b.txt
    assert stat.lines_removed == 1  # the replaced line in a.txt


def test_diff_stat_returns_zero_when_no_changes(tmp_path):
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\n")
    _commit(clone, "initial")

    stat = diff_stat(clone, "HEAD")

    assert stat == DiffStat(files=0, lines_added=0, lines_removed=0)


def test_diff_stat_counts_binary_files_without_line_counts(tmp_path):
    clone = _init_repo(tmp_path)
    (clone / "a.txt").write_text("line1\n")
    _commit(clone, "initial")

    (clone / "binary.dat").write_bytes(b"\x00\x01\x02binary")
    _commit(clone, "add binary")

    stat = diff_stat(clone, "HEAD~1")

    assert stat.files == 1
    assert stat.lines_added == 0
    assert stat.lines_removed == 0
