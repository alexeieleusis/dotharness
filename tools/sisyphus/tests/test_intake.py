from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

from sisyphus import intake
from sisyphus.config import ProjectConfig, SisyphusConfig
from sisyphus.intake import (
    BoundaryViolation,
    IntakeError,
    build_prompt,
    parse_open_decisions,
    parse_resolved_decisions,
    repo_unchanged,
    resolve_brief_path,
    run_intake,
    snapshot_git_status,
    split_resolved_decision,
)
from sisyphus.paths import find_checkout_root, knowledge_file
from sisyphus.rails import Checkpoint
from sisyphus.runrecord import RunRecord

BRIEF_NONE = "# Brief\n\n## Open decisions log\n\nNone\n\n## Next step\n\nGo.\n"
BRIEF_OPEN = (
    "# Brief\n\n## Open decisions log\n\n1. Which tracker?\n   Context line.\n2. Resolved: use GitHub (see D-1).\n"
    "3. What label?\n\n## Next step\n\n1. Not a decision.\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)  # noqa: S603, S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "tracked.txt").write_text("one\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def config(tmp_path: Path, repo: Path) -> SisyphusConfig:
    ws = tmp_path / "ws"
    return SisyphusConfig(
        project=ProjectConfig(
            repo_dir=str(repo), workspace_dir=str(ws), brief_path_template="<workspace_dir>/<item-id>/brief.md"
        )
    )


class FakeRunner:
    def __init__(self, brief_text: str = BRIEF_NONE, side_effect=None) -> None:
        self.brief_text = brief_text
        self.side_effect = side_effect
        self.prompts: list[str] = []
        self.config: SisyphusConfig | None = None

    def __call__(self, prompt: str) -> None:
        self.prompts.append(prompt)
        match = re.search(r"Save the brief file to (\S+)\.", prompt)
        assert match
        Path(match.group(1)).write_text(self.brief_text)
        if self.side_effect:
            self.side_effect()


def test_prompt_file_has_exactly_two_placeholders() -> None:
    root = find_checkout_root()
    text = knowledge_file("sisyphus/intake-prompt.md", checkout_root=root).read_text()
    assert text.count("<work-item reference>") == 1
    assert text.count("<brief path>") == 1
    assert len(re.findall(r"<[^>\n]+>", text)) == 2
    drafting = knowledge_file("spec-prism-flow/requirements-doc-drafting-prompt.md", checkout_root=root).read_text()
    for line in drafting.splitlines():
        if len(line.strip()) > 40:
            assert line.strip() not in text


def test_both_placeholders_substituted(config: SisyphusConfig) -> None:
    runner = FakeRunner()
    result = run_intake("ENG-1", config, runner)
    (prompt,) = runner.prompts
    assert "<work-item reference>" not in prompt
    assert "<brief path>" not in prompt
    assert "ENG-1" in prompt
    assert str(result.brief_path) in prompt
    assert "requirements-doc-drafting-prompt.md is at /" in prompt
    assert result.brief_path.is_file()


def test_build_prompt_unit() -> None:
    out = build_prompt("A <work-item reference> B <brief path>", "X-1", "/p/b.md", "/k/d.md")
    assert out.startswith("A X-1 B /p/b.md")
    assert out.rstrip().endswith("/k/d.md")


def test_resolve_brief_path_errors(tmp_path: Path) -> None:
    with pytest.raises(IntakeError):
        resolve_brief_path(SisyphusConfig(project=ProjectConfig()), "X")
    with pytest.raises(IntakeError, match="workspace_dir"):
        resolve_brief_path(SisyphusConfig(project=ProjectConfig(brief_path_template="<workspace_dir>/b.md")), "X")


def test_parse_open_decisions() -> None:
    decisions = parse_open_decisions(BRIEF_OPEN)
    assert [d.number for d in decisions] == [1, 3]
    assert "Context line." in decisions[0].text
    assert parse_open_decisions(BRIEF_NONE) == ()
    assert parse_open_decisions("# No such section\n1. x\n") == ()


BRIEF_RESOLVED = (
    "# Brief\n\n## Open decisions log\n\n- D-1: Who owns the dir? Status: resolved - the item. Done.\n"
    "- D-2: Resolved: use GitHub.\n- D-3: Still open?\n\n## Next\n\n- D-9: not here. Status: resolved\n"
)


def test_parse_resolved_decisions() -> None:
    resolved = parse_resolved_decisions(BRIEF_RESOLVED)
    assert [d.number for d in resolved] == [1, 2]
    assert [d.number for d in parse_open_decisions(BRIEF_RESOLVED)] == [3]
    assert [d.number for d in parse_resolved_decisions(BRIEF_OPEN)] == [2]


@pytest.mark.parametrize("text", [BRIEF_NONE, "# No such section\n1. x\n", "## Open decisions\n1. Q?\n", ""])
def test_parse_resolved_decisions_empty(text: str) -> None:
    assert parse_resolved_decisions(text) == ()


def test_split_resolved_decision() -> None:
    q, a = split_resolved_decision("Who owns it? Status: resolved - the item. Done.")
    assert q == "Who owns it?"
    assert a == "the item. Done."
    assert split_resolved_decision("Q? status: RESOLVED. Answer") == ("Q?", "Answer")
    assert split_resolved_decision("Resolved: use GitHub.") == ("", "Resolved: use GitHub.")
    assert split_resolved_decision("**Resolved** x") == ("", "**Resolved** x")
    assert split_resolved_decision("Q? Status: resolved") == ("Q?", "")


@pytest.mark.parametrize(
    "text",
    [
        "## Open decisions (2)\n1. Which auth provider?\n",
        "## Open decisions (D-1..D-5)\n1. Q?\n",
        "**Open decisions**\n\n1. Q?\n",
        "## Open questions\n1. Q?\n",
        "## Open decisions log\n- D-3: Q?\n",
        "## Open decisions log\n  1. Q?\n",
        "Open decisions\n---\n1. Q?\n",
        "## Open decisions log\nWe still need to pick a provider.\n",
    ],
)
def test_parse_open_decisions_fails_closed(text: str) -> None:
    assert parse_open_decisions(text)


def test_c1_result_when_open_decisions(config: SisyphusConfig) -> None:
    result = run_intake("ENG-2", config, FakeRunner(BRIEF_OPEN))
    assert result.blocked
    assert result.checkpoint is Checkpoint.C1
    rec = RunRecord.open(Path(config.project.workspace_dir or ""), "ENG-2")
    assert rec.checkpoint == "C1"
    assert (rec.stage, rec.stage_status) == ("intake", "complete")


def test_no_c1_when_no_open_decisions(config: SisyphusConfig) -> None:
    result = run_intake("ENG-3", config, FakeRunner(BRIEF_NONE))
    assert not result.blocked
    assert result.checkpoint is None
    rec = RunRecord.open(Path(config.project.workspace_dir or ""), "ENG-3")
    assert rec.checkpoint is None


def test_skip_when_unchanged_and_c1_reparsed(config: SisyphusConfig) -> None:
    first = FakeRunner(BRIEF_OPEN)
    run_intake("ENG-4", config, first)
    second = FakeRunner()
    again = run_intake("ENG-4", config, second)
    assert again.skipped
    assert second.prompts == []
    assert again.blocked
    # a human resolves the log in the existing brief: the skip still sees it, clears C1
    again.brief_path.write_text(BRIEF_NONE)
    third = run_intake("ENG-4", config, second)
    assert third.skipped
    assert not third.blocked
    assert RunRecord.open(Path(config.project.workspace_dir or ""), "ENG-4").checkpoint is None


def test_reparse_only_never_redrafts_existing_brief(config: SisyphusConfig) -> None:
    first = run_intake("ENG-4b", config, FakeRunner(BRIEF_NONE))
    # the sidecar no longer matches (as after editing the prompt), and a human edited the brief
    Path(str(first.brief_path) + intake.SIDECAR_SUFFIX).write_text("{}")
    first.brief_path.write_text(BRIEF_OPEN)
    runner = FakeRunner()
    result = run_intake("ENG-4b", config, runner, reparse_only=True)
    assert runner.prompts == []
    assert result.skipped
    assert result.blocked
    assert [d.number for d in result.open_decisions] == [1, 3]
    assert first.brief_path.read_text() == BRIEF_OPEN


def test_reparse_only_runs_when_brief_missing(config: SisyphusConfig) -> None:
    runner = FakeRunner()
    result = run_intake("ENG-4c", config, runner, reparse_only=True)
    assert len(runner.prompts) == 1
    assert not result.skipped
    with pytest.raises(IntakeError, match="did not write"):
        run_intake("ENG-4d", config, lambda _prompt: None, reparse_only=True)


def test_rerun_when_brief_missing(config: SisyphusConfig) -> None:
    run_intake("ENG-5", config, FakeRunner())
    brief = resolve_brief_path(config, "ENG-5")
    brief.unlink()
    runner = FakeRunner()
    assert not run_intake("ENG-5", config, runner).skipped
    assert len(runner.prompts) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda real: {**real, "item": "OTHER"},
        lambda real: {**real, "prompt_sha256": "bad"},
        lambda real: "not json",
        lambda real: "[]",
    ],
    ids=["item-differs", "digest-differs", "corrupt-json", "not-a-dict"],
)
def test_rerun_when_sidecar_does_not_vouch(config: SisyphusConfig, mutate) -> None:
    first = run_intake("ENG-5", config, FakeRunner())
    sidecar = Path(str(first.brief_path) + intake.SIDECAR_SUFFIX)
    real = json.loads(sidecar.read_text())
    assert real["item"] == "ENG-5"
    changed = mutate(real)
    sidecar.write_text(changed if isinstance(changed, str) else json.dumps(changed))
    runner = FakeRunner()
    assert not run_intake("ENG-5", config, runner).skipped
    assert len(runner.prompts) == 1


def test_rerun_when_sidecar_missing(config: SisyphusConfig) -> None:
    first = run_intake("ENG-5", config, FakeRunner())
    Path(str(first.brief_path) + intake.SIDECAR_SUFFIX).unlink()
    runner = FakeRunner()
    assert not run_intake("ENG-5", config, runner).skipped
    assert len(runner.prompts) == 1


def test_missing_brief_after_runner_raises(config: SisyphusConfig) -> None:
    with pytest.raises(IntakeError, match="did not write"):
        run_intake("ENG-6", config, lambda _prompt: None)
    rec = RunRecord.open(Path(config.project.workspace_dir or ""), "ENG-6")
    assert rec.stage_status == "in_progress"


def test_stale_brief_not_accepted_when_runner_writes_nothing(config: SisyphusConfig) -> None:
    first = run_intake("ENG-7", config, FakeRunner(BRIEF_OPEN))
    # the prompt digest no longer matches (as after editing the prompt): the sidecar is stale
    Path(str(first.brief_path) + intake.SIDECAR_SUFFIX).write_text("{}")
    with pytest.raises(IntakeError, match="did not write"):
        run_intake("ENG-7", config, lambda _prompt: None)
    # no fresh sidecar vouches for the stale brief
    assert Path(str(first.brief_path) + intake.SIDECAR_SUFFIX).read_text() == "{}"


def test_runner_modifying_tracked_file_fails_stage(config: SisyphusConfig, repo: Path) -> None:
    runner = FakeRunner(side_effect=lambda: (repo / "tracked.txt").write_text("two\n"))
    with pytest.raises(BoundaryViolation):
        run_intake("ENG-7", config, runner)
    rec = RunRecord.open(Path(config.project.workspace_dir or ""), "ENG-7")
    assert rec.stage_status == "in_progress"
    assert not intake._sidecar(resolve_brief_path(config, "ENG-7")).exists()


def test_boundary_detects_untracked_and_second_edit(repo: Path) -> None:
    with pytest.raises(BoundaryViolation), repo_unchanged(repo):
        (repo / "new.txt").write_text("x")
    (repo / "tracked.txt").write_text("dirty\n")
    with pytest.raises(BoundaryViolation), repo_unchanged(repo):
        (repo / "tracked.txt").write_text("dirtier\n")


def test_boundary_detects_commit_branch_and_ignored_changes(repo: Path) -> None:
    (repo / ".gitignore").write_text(".env\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    with pytest.raises(BoundaryViolation), repo_unchanged(repo):
        (repo / "tracked.txt").write_text("two\n")
        _git(repo, "commit", "-q", "-am", "agent commit")
    with pytest.raises(BoundaryViolation), repo_unchanged(repo):
        _git(repo, "switch", "-q", "-c", "other")
    with pytest.raises(BoundaryViolation), repo_unchanged(repo):
        (repo / ".env").write_text("SECRET=1\n")


def test_boundary_violation_reported_when_runner_raises(repo: Path) -> None:
    with pytest.raises(BoundaryViolation) as info, repo_unchanged(repo):
        (repo / "new.txt").write_text("x")
        raise RuntimeError
    assert isinstance(info.value.__cause__, RuntimeError)
    with pytest.raises(RuntimeError), repo_unchanged(repo):
        raise RuntimeError


def test_boundary_passes_without_change(repo: Path) -> None:
    with repo_unchanged(repo):
        pass
    before = snapshot_git_status(repo)
    assert before == snapshot_git_status(repo)


def test_boundary_ignores_listed_path(repo: Path) -> None:
    with repo_unchanged(repo, ignore=(repo / "brief.md",)):
        (repo / "brief.md").write_text("b")


def test_git_failure_is_intake_error(tmp_path: Path) -> None:
    with pytest.raises(IntakeError):
        snapshot_git_status(tmp_path / "not-a-repo")


def test_required_config(config: SisyphusConfig) -> None:
    with pytest.raises(IntakeError, match="repo_dir"):
        run_intake("X", SisyphusConfig(project=ProjectConfig()), FakeRunner())


def test_no_harness_imports() -> None:
    tree = ast.parse(Path(intake.__file__).read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in mods if m.split(".")[0] in ("harness", "spec_prism_flow")}
