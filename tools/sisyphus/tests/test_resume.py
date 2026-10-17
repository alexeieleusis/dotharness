"""Stop record, auto-resume detection, status and resume CLI tests (follow-up 18). All external effects are fakes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sisyphus import cli
from sisyphus.config import SisyphusConfig
from sisyphus.intake import resolve_brief_path
from sisyphus.lock import clone_lock
from sisyphus.orchestrator import BRANCH_CREATED_KEY, OPEN_PR_STAGE, PullRequest, branch_name
from sisyphus.resume import AUTO_RETRY_KEY, answer_path, cleared, iter_records, list_waiting
from sisyphus.runrecord import RunRecord
from sisyphus.stoprecord import (
    AUTO_RETRY_DELAY_MINUTES,
    AUTO_RETRY_MAX,
    build_stop_record,
    clear_stop_record,
    read_stop_record,
    stop_record_path,
    write_stop_record,
)
from tests.test_cli import FakePrs, items, make_env

NOW = datetime(2026, 1, 2, 12, tzinfo=UTC)
OPEN = "# B\n\n## Open decisions log\n\n1. Which tracker?\n"
CLEAR = "# B\n\n## Open decisions log\n\nNone\n"


def stopped(
    env: Any, item: str, cp: str, at: datetime = NOW, brief: str | None = OPEN, stage: str = "intake"
) -> RunRecord:
    ws = env.root / "ws"
    rec = RunRecord.open(ws, item)
    rec.mark_stage(stage, "complete")
    rec.set_checkpoint(cp)
    write_stop_record(
        rec.path,
        build_stop_record(
            item, checkpoint=cp, stage=stage, stage_status="complete", detail="why", resume_command="cmd", now=at
        ),
    )
    if brief is not None:
        path = resolve_brief_path(load(env), item)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(brief)
    return rec


def load(env: Any) -> SisyphusConfig:
    from sisyphus.config import load_config

    return load_config(env.config)


def run(env: Any, *extra: str):
    return env.invoke("run", "--config", str(env.config), "--base", "b", *extra)


# ----------------------------------------------------------------- stop record


def test_stop_record_round_trip_and_clear(tmp_path: Path) -> None:
    rec_path = tmp_path / "x.json"
    stop = build_stop_record(
        "X-1", checkpoint="C3", stage="build", stage_status="in_progress", detail="d", resume_command="r", now=NOW
    )
    write_stop_record(rec_path, stop)
    assert stop_record_path(rec_path).name == "x.json.stopped.json"
    assert read_stop_record(rec_path) == stop
    clear_stop_record(rec_path)
    assert read_stop_record(rec_path) is None


def test_corrupt_stop_record_reads_as_none(tmp_path: Path) -> None:
    stop_record_path(tmp_path / "x.json").write_text("{nope")
    assert read_stop_record(tmp_path / "x.json") is None


def test_stop_record_leaves_run_record_schema_untouched(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X-1")
    before = rec.to_dict().keys()
    write_stop_record(
        rec.path,
        build_stop_record("X-1", checkpoint="C1", stage=None, stage_status=None, detail="d", resume_command="r"),
    )
    assert RunRecord.open(tmp_path, "X-1").to_dict().keys() == before
    assert [r.item for r in iter_records(tmp_path)] == ["X-1"]  # the sidecar is not a record


# ------------------------------------------------------------ cleared detection


def test_cleared_rules_per_checkpoint(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    cfg = load(env)
    c1 = stopped(env, "A-1", "C1")
    assert not cleared(c1, read_stop_record(c1.path), cfg, NOW)
    resolve_brief_path(cfg, "A-1").write_text(CLEAR)
    assert cleared(c1, read_stop_record(c1.path), cfg, NOW)
    for cp in ("C2", "C3", "C4"):
        rec = stopped(env, f"B-{cp}", cp, brief=None)
        assert not cleared(rec, read_stop_record(rec.path), cfg, NOW)
        answer_path(rec.path).write_text("  \n")
        assert not cleared(rec, read_stop_record(rec.path), cfg, NOW)  # blank is not an answer
        answer_path(rec.path).write_text("Use B")
        assert cleared(rec, read_stop_record(rec.path), cfg, NOW)
    for cp in ("C5", "C6", "C7"):  # explicit only, even with an answer file and a clear brief
        rec = stopped(env, f"E-{cp}", cp, brief=CLEAR)
        answer_path(rec.path).write_text("go")
        assert not cleared(rec, read_stop_record(rec.path), cfg, NOW + timedelta(days=9))


def test_c8_retries_after_delay_and_is_bounded(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    cfg = load(env)
    rec = stopped(env, "N-1", "C8", brief=None)
    stop = read_stop_record(rec.path)
    delay = timedelta(minutes=AUTO_RETRY_DELAY_MINUTES)
    assert not cleared(rec, stop, cfg, NOW + delay - timedelta(seconds=1))
    assert cleared(rec, stop, cfg, NOW + delay)
    for _ in range(AUTO_RETRY_MAX):
        rec.increment_retry(AUTO_RETRY_KEY)
    assert not cleared(rec, stop, cfg, NOW + timedelta(days=1))


def test_error_stop_is_never_auto_resumed(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = RunRecord.open(tmp_path / "ws", "Z-1")
    write_stop_record(
        rec.path,
        build_stop_record(
            "Z-1", checkpoint=None, stage="build", stage_status="in_progress", detail="boom", resume_command="r"
        ),
    )
    assert list_waiting(tmp_path / "ws") == []  # no checkpoint set: not waiting, not auto
    assert not cleared(rec, read_stop_record(rec.path), load(env), NOW)


# -------------------------------------------------------------- run: auto-resume


def test_run_auto_resumes_a_cleared_c1_item(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    stopped(env, "OLD-1", "C1", brief=CLEAR)
    result = run(env)
    assert result.exit_code == 0, result.output
    assert items(fake_run_item) == ["OLD-1"]
    assert fake_run_item["calls"][0][2]["lock_held"] is True
    assert fake_run_item["calls"][0][1].resume_command.startswith("sisyphus resume {item} --config ")


def test_waiting_item_does_not_block_picking_and_is_listed(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["NEW-1"])
    stopped(env, "OLD-1", "C1", brief=OPEN)
    result = run(env)
    assert result.exit_code == 0
    assert items(fake_run_item) == ["NEW-1"]
    needs = env.digest.split("## Needs you")[1].split("## Done")[0]
    assert "OLD-1" in needs
    assert "Awaiting you" in needs
    assert "Action:" in needs
    assert "Resume: `cmd`" in needs


def test_only_waiting_items_run_nothing_but_digest_lists_them(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    stopped(env, "OLD-1", "C5", brief=None)
    assert run(env).exit_code == 0
    assert fake_run_item["calls"] == []
    assert "Awaiting you" in env.digest


def test_resumed_and_new_share_max_tickets_per_run(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["NEW-1", "NEW-2"], extra="\n[limits]\nmax_tickets_per_run = 2\n")
    stopped(env, "OLD-1", "C1", brief=CLEAR)
    run(env)
    assert items(fake_run_item) == ["OLD-1", "NEW-1"]


def test_resumed_item_uses_the_whole_budget(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["NEW-1"])  # default max_tickets_per_run = 1
    stopped(env, "OLD-1", "C1", brief=CLEAR)
    run(env)
    assert items(fake_run_item) == ["OLD-1"]


def test_cleared_items_resume_oldest_first_and_overflow_waits(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, extra="\n[limits]\nmax_tickets_per_run = 2\n")
    stopped(env, "C-3", "C1", at=NOW - timedelta(hours=1), brief=CLEAR)
    stopped(env, "A-1", "C1", at=NOW - timedelta(hours=9), brief=CLEAR)
    stopped(env, "B-2", "C1", at=NOW - timedelta(hours=5), brief=CLEAR)
    run(env)
    assert items(fake_run_item) == ["A-1", "B-2"]
    assert "C-3: blocker cleared; resumes on a later run" in env.digest


def test_c8_auto_resume_counts_the_attempt(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    env.services.now = lambda: NOW + timedelta(hours=1)
    rec = stopped(env, "N-1", "C8", brief=None)
    run(env)
    assert items(fake_run_item) == ["N-1"]
    assert RunRecord.open(tmp_path / "ws", "N-1").retries[AUTO_RETRY_KEY] == 1
    assert rec.retries.get(AUTO_RETRY_KEY) is None  # stale object, the counter lives in the file


def test_c8_too_recent_is_not_resumed(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    env.services.now = lambda: NOW + timedelta(minutes=1)
    stopped(env, "N-1", "C8", brief=None)
    run(env)
    assert fake_run_item["calls"] == []


def test_answer_file_clears_c3_in_run(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    rec = stopped(env, "Q-1", "C3", brief=None, stage="build")
    run(env)
    assert fake_run_item["calls"] == []
    answer_path(rec.path).write_text("Use B")
    run(env)
    assert items(fake_run_item) == ["Q-1"]


def test_checkpoint_item_with_open_pr_does_not_trigger_the_review_rule(
    tmp_path: Path, fake_run_item: dict[str, Any]
) -> None:
    prs = FakePrs({branch_name("OLD-1"): PullRequest(3, "http://pr/3")})
    env = make_env(tmp_path, ["NEW-1"], prs=prs)
    rec = stopped(env, "OLD-1", "C6", brief=None, stage=OPEN_PR_STAGE)
    rec.increment_retry(BRANCH_CREATED_KEY)
    run(env)
    assert items(fake_run_item) == ["NEW-1"]


def test_pr_awaiting_review_still_skips_picking_new_items(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    prs = FakePrs({branch_name("OLD-1"): PullRequest(3, "http://pr/3")})
    env = make_env(tmp_path, ["NEW-1"], prs=prs)
    rec = RunRecord.open(tmp_path / "ws", "OLD-1")
    rec.mark_stage(OPEN_PR_STAGE, "complete")
    rec.increment_retry(BRANCH_CREATED_KEY)
    run(env)
    assert fake_run_item["calls"] == []
    assert "awaits review" in env.digest


def test_pr_rule_still_allows_resuming_cleared_items(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    prs = FakePrs({branch_name("OLD-1"): PullRequest(3, "http://pr/3")})
    env = make_env(tmp_path, ["NEW-1"], prs=prs)
    rec = RunRecord.open(tmp_path / "ws", "OLD-1")
    rec.mark_stage(OPEN_PR_STAGE, "complete")
    rec.increment_retry(BRANCH_CREATED_KEY)
    stopped(env, "RES-1", "C1", brief=CLEAR)
    run(env)
    assert items(fake_run_item) == ["RES-1"]


# ----------------------------------------------------------------- resume command


def test_resume_runs_the_stopped_item_under_the_held_lock(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    stopped(env, "OLD-1", "C5", brief=None)
    result = env.invoke("resume", "OLD-1", "--config", str(env.config), "--base", "feat")
    assert result.exit_code == 0, result.output
    assert items(fake_run_item) == ["OLD-1"]
    _item, options, kwargs = fake_run_item["calls"][0]
    assert kwargs["lock_held"] is True
    assert options.base_branch == "feat"
    assert options.resume_command == f"sisyphus resume {{item}} --config {env.config.resolve()} --base feat"


def test_resume_ignores_the_cleared_check(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    stopped(env, "OLD-1", "C1", brief=OPEN)  # not cleared; explicit resume still runs (and stops again)
    assert env.invoke("resume", "OLD-1", "--config", str(env.config)).exit_code == 0
    assert items(fake_run_item) == ["OLD-1"]


def test_resume_without_record_errors(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    result = env.invoke("resume", "NOPE-1", "--config", str(env.config))
    assert result.exit_code == 1
    assert "no run record for NOPE-1" in result.output
    assert fake_run_item["calls"] == []


def test_resume_of_a_not_stopped_item_errors(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    RunRecord.open(tmp_path / "ws", "DONE-1").mark_stage("open-pr", "complete")
    result = env.invoke("resume", "DONE-1", "--config", str(env.config))
    assert result.exit_code == 1
    assert "not stopped" in result.output
    assert fake_run_item["calls"] == []


def test_resume_accepts_an_errored_item(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    rec = RunRecord.open(tmp_path / "ws", "ERR-1")
    write_stop_record(
        rec.path,
        build_stop_record(
            "ERR-1", checkpoint=None, stage="build", stage_status="in_progress", detail="boom", resume_command="r"
        ),
    )
    assert env.invoke("resume", "ERR-1", "--config", str(env.config)).exit_code == 0
    assert items(fake_run_item) == ["ERR-1"]


def test_resume_under_a_held_lock_exits_zero_quietly(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    stopped(env, "OLD-1", "C5", brief=None)
    with clone_lock(tmp_path / "repo", tmp_path / "locks"):
        result = env.invoke("resume", "OLD-1", "--config", str(env.config))
    assert (result.exit_code, result.output) == (0, "")
    assert fake_run_item["calls"] == []


def test_resume_lock_is_released_and_digest_written(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    stopped(env, "OLD-1", "C5", brief=None)
    env.invoke("resume", "OLD-1", "--config", str(env.config))
    with clone_lock(tmp_path / "repo", tmp_path / "locks"):
        pass
    assert (tmp_path / "digests" / "2026-01-02.md").exists()


def test_resume_run_error_exits_one(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    stopped(env, "OLD-1", "C5", brief=None)
    fake_run_item["raise"]["OLD-1"] = RuntimeError("boom")
    result = env.invoke("resume", "OLD-1", "--config", str(env.config))
    assert result.exit_code == 1
    assert "boom" in result.output


# ------------------------------------------------------------------------ status


def test_status_none(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    result = env.invoke("status", "--config", str(env.config))
    assert (result.exit_code, result.output.strip()) == (0, "no items")


def test_status_one_item_shows_everything(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    stopped(env, "OLD-1", "C1")
    result = env.invoke("status", "OLD-1", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    for text in (
        "OLD-1",
        "stage: intake",
        "stage_status: complete",
        "checkpoint: C1",
        "awaiting: you",
        "question:",
        "action:",
        "resume: cmd",
        "auto-resume: brief (cleared now: no)",
    ):
        assert text in result.output


def test_status_all_items_and_review_state(tmp_path: Path) -> None:
    prs = FakePrs({branch_name("REV-1"): PullRequest(3, "http://pr/3")})
    env = make_env(tmp_path, prs=prs)
    stopped(env, "OLD-1", "C5", brief=None)
    rec = RunRecord.open(tmp_path / "ws", "REV-1")
    rec.mark_stage(OPEN_PR_STAGE, "complete")
    rec.increment_retry(BRANCH_CREATED_KEY)
    RunRecord.open(tmp_path / "ws", "IDLE-1")
    err = RunRecord.open(tmp_path / "ws", "ERR-1")
    write_stop_record(
        err.path,
        build_stop_record(
            "ERR-1", checkpoint=None, stage="build", stage_status="in_progress", detail="boom", resume_command="r"
        ),
    )
    out = env.invoke("status", "--config", str(env.config)).output
    assert "OLD-1" in out
    assert out.count("awaiting: you") == 1
    assert out.count("awaiting: review") == 1
    assert out.count("awaiting: error") == 1
    assert out.count("awaiting: none") == 1
    assert "auto-resume: explicit" in out


def test_status_unknown_ref_errors(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    result = env.invoke("status", "NOPE-1", "--config", str(env.config))
    assert result.exit_code == 1
    assert "no run record for NOPE-1" in result.output


def test_status_is_read_only_and_takes_no_lock(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rec = stopped(env, "OLD-1", "C1")
    before = (rec.path.read_text(), stop_record_path(rec.path).read_text())
    with clone_lock(tmp_path / "repo", tmp_path / "locks"):
        assert env.invoke("status", "--config", str(env.config)).exit_code == 0
    assert (rec.path.read_text(), stop_record_path(rec.path).read_text()) == before


def test_intake_command_writes_and_clears_the_c1_stop_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sisyphus.intake import IntakeResult, OpenDecision

    env = make_env(tmp_path)
    brief = tmp_path / "ws" / "b.md"
    results = [
        IntakeResult(brief, False, (OpenDecision(1, "Which tracker?"),)),
        IntakeResult(brief, False, ()),
    ]
    monkeypatch.setattr(cli, "run_intake", lambda *a, **k: results.pop(0))
    assert env.invoke("intake", "ENG-1", "--config", str(env.config)).exit_code == 0
    rec_path = RunRecord.open(tmp_path / "ws", "ENG-1").path
    stop = read_stop_record(rec_path)
    assert stop is not None
    assert stop.checkpoint == "C1"
    assert stop.questions == ("1. Which tracker?",) or "Which tracker?" in stop.questions[0]
    assert stop.resume_command.startswith("sisyphus resume ENG-1 --config ")
    assert env.invoke("intake", "ENG-1", "--config", str(env.config)).exit_code == 0
    assert read_stop_record(rec_path) is None
