"""Stop record, auto-resume detection, status and resume CLI tests (follow-up 18). All external effects are fakes."""

from __future__ import annotations

import builtins
import dataclasses
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dotharness.sisyphus import cli
from dotharness.sisyphus.config import SisyphusConfig
from dotharness.sisyphus.intake import resolve_brief_path
from dotharness.sisyphus.lock import clone_lock
from dotharness.sisyphus.orchestrator import BRANCH_CREATED_KEY, OPEN_PR_STAGE, PullRequest, branch_name
from dotharness.sisyphus.resume import (
    AUTO_RETRY_KEY,
    _append_line,
    answer_path,
    answers_log_path,
    append_answer_log,
    build_answer_log_record,
    cleared,
    iter_records,
    list_waiting,
)
from dotharness.sisyphus.runrecord import RunRecord
from dotharness.sisyphus.stoprecord import (
    AUTO_RETRY_DELAY_MINUTES,
    AUTO_RETRY_MAX,
    build_stop_record,
    clear_stop_record,
    read_stop_record,
    stop_record_path,
    write_stop_record,
)
from tests.sisyphus.test_cli import FakePrs, items, make_env

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
    from dotharness.sisyphus.config import load_config

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


def test_open_pr_of_earlier_item_does_not_stop_resume_or_new_picks(
    tmp_path: Path, fake_run_item: dict[str, Any]
) -> None:
    prs = FakePrs({branch_name("OLD-1"): PullRequest(3, "http://pr/3")})
    env = make_env(tmp_path, ["NEW-1"], prs=prs, extra="\n[limits]\nmax_tickets_per_run = 2\n")
    rec = RunRecord.open(tmp_path / "ws", "OLD-1")
    rec.mark_stage(OPEN_PR_STAGE, "complete")
    rec.increment_retry(BRANCH_CREATED_KEY)
    stopped(env, "RES-1", "C1", brief=CLEAR)
    run(env)
    assert items(fake_run_item) == ["RES-1", "NEW-1"]


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


def test_status_all_items_states(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    stopped(env, "OLD-1", "C5", brief=None)
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
    from dotharness.sisyphus.intake import IntakeResult, OpenDecision

    env = make_env(tmp_path)
    brief = tmp_path / "ws" / "b.md"
    results = [
        IntakeResult(brief, False, (OpenDecision(1, "Which tracker?"),)),
        IntakeResult(brief, False, ()),
    ]

    async def fake_intake(*_a: object, **_k: object) -> IntakeResult:
        return results.pop(0)

    monkeypatch.setattr(cli, "run_intake", fake_intake)
    assert env.invoke("intake", "ENG-1", "--config", str(env.config)).exit_code == 0
    rec_path = RunRecord.open(tmp_path / "ws", "ENG-1").path
    stop = read_stop_record(rec_path)
    assert stop is not None
    assert stop.checkpoint == "C1"
    assert stop.questions == ("1. Which tracker?",) or "Which tracker?" in stop.questions[0]
    assert stop.resume_command.startswith("sisyphus resume ENG-1 --config ")
    assert env.invoke("intake", "ENG-1", "--config", str(env.config)).exit_code == 0
    assert read_stop_record(rec_path) is None


LOG_KEYS = [
    "item", "checkpoint", "stage", "stage_status", "leaf", "questions", "detail", "stopped_at", "answer",
    "answered_at",
]  # fmt: skip


def _stop(rec: Path, checkpoint: str | None, detail: str, questions: tuple[str, ...] = ()) -> None:
    write_stop_record(
        rec,
        build_stop_record(
            "Q-1", checkpoint=checkpoint, stage="build", stage_status="blocked", detail=detail,
            resume_command="sisyphus resume Q-1", questions=questions, now=NOW,
        ),
    )  # fmt: skip


def test_answers_log_path_is_jsonl_sibling(tmp_path: Path) -> None:
    assert answers_log_path(tmp_path / "x.json") == tmp_path / "x.answers.jsonl"


def test_append_answer_log_two_appends_in_order(tmp_path: Path) -> None:
    rec = tmp_path / "Q-1.json"
    _stop(rec, "C3", "d1", ("Q1?",))
    append_answer_log(rec, "Q-1", "A1\n")
    _stop(rec, "C3", "d2", ("Q2?",))
    append_answer_log(rec, "Q-1", "A2")
    lines = answers_log_path(rec).read_text(encoding="utf-8").splitlines()
    objs = [json.loads(line) for line in lines]
    assert [list(o) for o in objs] == [LOG_KEYS, LOG_KEYS]
    assert [(o["questions"], o["answer"]) for o in objs] == [(["Q1?"], "A1"), (["Q2?"], "A2")]
    assert objs[0]["checkpoint"] == "C3" and objs[0]["stage"] == "build" and objs[0]["leaf"] is None


def test_append_answer_log_missing_stop_record(tmp_path: Path) -> None:
    rec = tmp_path / "Q-1.json"
    append_answer_log(rec, "Q-1", "hi")
    obj = json.loads(answers_log_path(rec).read_text(encoding="utf-8"))
    assert obj["item"] == "Q-1" and obj["questions"] == []
    for key in ("checkpoint", "stage", "stage_status", "leaf", "detail", "stopped_at"):
        assert obj[key] is None


def test_append_answer_log_error_stop_logs_detail_as_questions(tmp_path: Path) -> None:
    rec = tmp_path / "Q-1.json"
    _stop(rec, "C5", "tests keep failing")
    append_answer_log(rec, "Q-1", "retry")
    obj = json.loads(answers_log_path(rec).read_text(encoding="utf-8"))
    assert obj["questions"] == ["tests keep failing"]


def test_append_answer_log_unicode_multiline_one_line(tmp_path: Path) -> None:
    rec = tmp_path / "Q-1.json"
    append_answer_log(rec, "Q-1", " line one\nl\u00edne two \u2713\n")
    text = answers_log_path(rec).read_text(encoding="utf-8")
    assert text.count("\n") == 1 and text.endswith("\n")
    assert json.loads(text)["answer"] == "line one\nl\u00edne two \u2713"


def test_build_answer_log_record_normalises_string_questions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = tmp_path / "Q-1.json"
    _stop(rec, "C3", "d")
    stop = read_stop_record(rec)
    assert stop is not None
    for raw, expected in (("free text", ["free text"]), ("", [])):
        odd = dataclasses.replace(stop, questions=raw)  # type: ignore[arg-type]
        monkeypatch.setattr("dotharness.sisyphus.resume.read_stop_record", lambda _p, odd=odd: odd)
        assert build_answer_log_record(rec, "Q-1", "a")["questions"] == expected


def test_build_answer_log_record_leaf_is_none(tmp_path: Path) -> None:
    rec = tmp_path / "Q-1.json"
    _stop(rec, "C3", "d")
    assert build_answer_log_record(rec, "Q-1", "a")["leaf"] is None


def _tolerant_read(path: Path) -> list[Any]:
    """Parse each non-empty line; skip lines that do not parse.

    A truncated line is only ever last when read before the next append; once an
    append repairs the newline, the truncated bytes sit mid-file and are skipped too.
    """
    out: list[Any] = []
    for ln in path.read_bytes().decode("utf-8").split("\n"):
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def test_append_line_creates_missing_parent(tmp_path: Path) -> None:
    p = tmp_path / "a" / "b" / "x.answers.jsonl"
    _append_line(p, '{"a":1}')
    assert p.read_bytes() == b'{"a":1}\n'


def test_append_line_does_not_double_supplied_newline(tmp_path: Path) -> None:
    p = tmp_path / "x.answers.jsonl"
    _append_line(p, '{"a":1}\n')
    assert p.read_bytes() == b'{"a":1}\n'


def test_append_line_truncated_tail_is_preserved_and_new_line_separate(tmp_path: Path) -> None:
    p = tmp_path / "x.answers.jsonl"
    original = b'{"a":1}\n{"a":'
    p.write_bytes(original)
    _append_line(p, '{"a":2}')
    data = p.read_bytes()
    assert data.startswith(original)
    assert data.split(b"\n")[-2] == b'{"a":2}'
    assert _tolerant_read(p) == [{"a": 1}, {"a": 2}]


def test_append_line_after_newline_adds_no_blank_line(tmp_path: Path) -> None:
    p = tmp_path / "x.answers.jsonl"
    p.write_bytes(b'{"a":1}\n')
    _append_line(p, '{"a":2}')
    assert b"\n\n" not in p.read_bytes()
    assert _tolerant_read(p) == [{"a": 1}, {"a": 2}]


def test_append_line_oserror_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(OSError):
        _append_line(blocker / "x.answers.jsonl", "{}")

    def boom(fd: int) -> None:
        raise OSError(5, "fsync failed")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="fsync failed"):
        _append_line(tmp_path / "y.answers.jsonl", "{}")


def test_append_line_single_write_and_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "x.answers.jsonl"
    p.write_bytes(b'{"a":')  # forces the repair prefix into the same write
    writes: list[bytes] = []
    fsyncs: list[int] = []
    real_open = builtins.open

    class Counting:
        def __init__(self, fh: Any) -> None:
            self._fh = fh

        def write(self, data: bytes) -> int:
            writes.append(data)
            return self._fh.write(data)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._fh, name)

        def __enter__(self) -> Counting:
            return self

        def __exit__(self, *exc: object) -> None:
            self._fh.close()

    def fake_open(file: Any, mode: str = "r", *a: Any, **kw: Any) -> Any:
        fh = real_open(file, mode, *a, **kw)
        return Counting(fh) if mode == "ab" else fh

    real_fsync = os.fsync

    def fake_fsync(fd: int) -> None:
        fsyncs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(os, "fsync", fake_fsync)
    _append_line(p, '{"a":2}')
    assert writes == [b'\n{"a":2}\n']
    assert len(fsyncs) >= 1


def test_clear_stop_record_leaves_answers_log(tmp_path: Path) -> None:
    rec = tmp_path / "enc.json"
    write_stop_record(
        rec, build_stop_record("X-1", checkpoint="C3", stage=None, stage_status=None, detail="d", resume_command="r")
    )
    log = answers_log_path(rec)
    log.write_bytes(b'{"a":1}\n')
    clear_stop_record(rec)
    assert not stop_record_path(rec).exists()
    assert log.read_bytes() == b'{"a":1}\n'
