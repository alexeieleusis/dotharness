"""Wiring of the stop-text presentation step into run/resume, the digest and status (fake launcher, no backend)."""

# ruff: noqa: TRY003
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dotharness.ai_backend import RunResult
from dotharness.sisyphus import cli
from dotharness.sisyphus.digest import DigestReport, NeedsYou
from dotharness.sisyphus.orchestrator import PullRequest
from dotharness.sisyphus.orchestrator import RunResult as ItemResult
from dotharness.sisyphus.rails import Checkpoint
from dotharness.sisyphus.runrecord import RunRecord
from dotharness.sisyphus.stoprecord import (
    build_abandoned_record,
    build_stop_record,
    read_stop_text,
    stop_record_path,
    stop_text_path,
    write_stop_record,
    write_stop_text,
)
from tests.sisyphus.test_cli import make_env

NOW = datetime(2026, 1, 2, tzinfo=UTC)
HASHES = {"A-1": "a1b2c3d4e5f6", "A-2": "0123456789abcdef"}


def _resume(item: str) -> str:
    return f"sisyphus resume {item} --config /c.toml"


def _detail(item: str) -> str:
    return f"Commit {HASHES.get(item, 'abcdef1')} changed src/x/y.py. Decide."


def _rewrite(item: str) -> str:
    return f"Decision needed.\n\n- {_detail(item)}\n\n{_resume(item)}"


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake ``run_item``: per-item mode ``stop`` (C5 with stop record), ``clean``, ``abandoned``, ``error``."""
    state: dict[str, Any] = {"mode": {}}

    async def fake(item, config, options, **kwargs):
        mode = state["mode"].get(item, "clean")
        rec = RunRecord.open(Path(config.project.workspace_dir), item)
        if mode == "error":
            write_stop_record(
                rec.path,
                build_stop_record(
                    item, checkpoint=None, stage="build", stage_status=None, detail="boom", resume_command="r", now=NOW
                ),
            )
            raise RuntimeError("boom")
        if mode == "abandoned":
            write_stop_record(
                rec.path,
                build_abandoned_record(item, stage="build", stage_status=None, prior_label="l", reenable_command="r"),
            )
            return ItemResult(item, None, "", ("build",), (), "b", None, None, rec, DigestReport())
        if mode == "stop":
            stop = build_stop_record(
                item,
                checkpoint="C5",
                stage="build",
                stage_status=None,
                detail=_detail(item),
                resume_command=_resume(item),
                now=NOW,
            )
            rec.set_checkpoint("C5")
            write_stop_record(rec.path, stop)
            # The orchestrator reports error stops only; the CLI renders checkpoint stops.
            return ItemResult(item, Checkpoint.C5, _detail(item), ("build",), (), "b", None, None, rec, DigestReport())
        return ItemResult(item, None, "", ("build",), (), "b", PullRequest(7, "http://pr/7"), None, rec, DigestReport())

    monkeypatch.setattr(cli, "run_item", fake)
    return state


class Launcher:
    def __init__(self, result: RunResult | None = None, texts: dict[str, str] | None = None) -> None:
        self.result = result
        self.texts = texts
        self.calls = 0

    async def __call__(self, backend: str | None, prompt: str, **kwargs: Any) -> RunResult:
        self.calls += 1
        if self.result is not None:
            return self.result
        texts = self.texts or {i: _rewrite(i) for i in HASHES}
        return RunResult(0, json.dumps({k: v for k, v in texts.items() if f'"{k}"' in prompt}), "")


def _env(tmp_path: Path, refs: list[str], launcher: Launcher, extra: str = ""):
    env = make_env(tmp_path, refs, extra=extra)
    env.services.present_launcher = launcher
    return env


def _run(env: Any, *args: str):
    return env.invoke("run", "--config", str(env.config), "--base", "b", *args)


def _ws(tmp_path: Path) -> Path:
    return tmp_path / "ws"


def _seed_waiting(ws: Path, item: str) -> None:
    rec = RunRecord.open(ws, item)
    rec.set_checkpoint("C5")
    write_stop_record(
        rec.path,
        build_stop_record(
            item, checkpoint="C5", stage="build", stage_status=None, detail="old", resume_command="r", now=NOW
        ),
    )


def test_two_stops_one_call_two_files_and_shown_everywhere(tmp_path: Path, stops: dict[str, Any]) -> None:
    stops["mode"] = {"A-1": "stop", "A-2": "stop"}
    launcher = Launcher()
    env = _env(tmp_path, ["A-1", "A-2"], launcher, "\n[limits]\nmax_tickets_per_run = 2\n")
    result = _run(env)
    assert result.exit_code == 0
    assert launcher.calls == 1
    for item in HASHES:
        rec = RunRecord.open(_ws(tmp_path), item)
        assert read_stop_text(rec.path) == _rewrite(item)
        assert stop_text_path(rec.path).exists()
    assert "A-1: stopped at C5:\n  Decision needed." in result.output
    digest = env.digest
    assert "Decision needed." in digest
    assert "Detail:" not in digest
    assert f"Stop record: `{stop_record_path(RunRecord.open(_ws(tmp_path), 'A-1').path)}`" in digest
    status = env.invoke("status", "--config", str(env.config))
    assert "    Decision needed." in status.output
    assert "Detail:" not in status.output


def test_digest_and_status_indentation(tmp_path: Path, stops: dict[str, Any]) -> None:
    stops["mode"] = {"A-1": "stop"}
    env = _env(tmp_path, ["A-1"], Launcher())
    _run(env)
    lines = env.digest.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("1. [C5] A-1:"))
    entry = [line for line in lines[start + 1 :] if line.startswith("   ") or line == ""]
    assert "   Decision needed." in entry
    assert "   - Commit a1b2c3d4e5f6 changed src/x/y.py. Decide." in entry
    assert "" in entry  # blank line inside the rewrite carries no indentation
    assert not any(line.startswith(("2. ", "- ")) for line in lines[start + 1 : start + 8])
    out = env.invoke("status", "--config", str(env.config)).output.splitlines()
    q = next(i for i, line in enumerate(out) if line.startswith("  question:"))
    assert out[q + 1] == "    Decision needed."
    assert out[q + 3] == "    - Commit a1b2c3d4e5f6 changed src/x/y.py. Decide."
    assert out[q + 2] == ""
    assert next(line for line in out[q + 1 :] if line.startswith("  ") and not line.startswith("   ")).startswith(
        "  action:"
    )


@pytest.mark.parametrize(
    "result",
    [
        RunResult(0, "", "", timed_out=True),
        RunResult(2, "", "bad"),
        RunResult(0, "", ""),
        RunResult(0, "not json", ""),
    ],
)
def test_failing_launcher_shows_raw_with_one_stderr_line(
    tmp_path: Path, stops: dict[str, Any], result: RunResult, capfd: pytest.CaptureFixture[str]
) -> None:
    stops["mode"] = {"A-1": "stop"}
    launcher = Launcher(result)
    env = _env(tmp_path, ["A-1"], launcher)
    out = _run(env)
    assert out.exit_code == 0
    assert launcher.calls == 1
    rec = RunRecord.open(_ws(tmp_path), "A-1")
    assert not stop_text_path(rec.path).exists()
    assert f"A-1: stopped at C5: {_detail('A-1')}" in out.output
    assert f"Detail: {_detail('A-1')}" in env.digest
    assert "Detail:" in env.invoke("status", "--config", str(env.config)).output
    err = [line for line in out.stderr.splitlines() if line.strip()]
    assert len(err) == 1
    assert err[0].startswith("presentation failed:")


@pytest.mark.parametrize("mode", ["clean", "abandoned", "error"])
def test_no_launcher_call_without_checkpoint_stop(tmp_path: Path, stops: dict[str, Any], mode: str) -> None:
    stops["mode"] = {"A-1": mode}
    launcher = Launcher()
    env = _env(tmp_path, ["A-1"], launcher)
    result = _run(env)
    assert result.exit_code == (1 if mode == "error" else 0)
    assert launcher.calls == 0
    assert not stop_text_path(RunRecord.open(_ws(tmp_path), "A-1").path).exists()
    if mode == "error":
        assert "boom" in result.output


def test_raw_flag_and_switch_never_call_launcher(tmp_path: Path, stops: dict[str, Any]) -> None:
    stops["mode"] = {"A-1": "stop"}
    launcher = Launcher()
    env = _env(tmp_path, ["A-1"], launcher)
    result = _run(env, "--raw")
    assert result.exit_code == 0
    assert launcher.calls == 0
    assert f"A-1: stopped at C5: {_detail('A-1')}" in result.output
    assert result.stderr == ""
    (tmp_path / "off").mkdir()
    off = _env(tmp_path / "off", ["A-1"], launcher, "\n[presentation]\npresent_stops = false\n")
    assert _run(off).exit_code == 0
    assert launcher.calls == 0


def test_resume_presents_the_resumed_stop(tmp_path: Path, stops: dict[str, Any]) -> None:
    stops["mode"] = {"A-1": "stop"}
    launcher = Launcher()
    env = _env(tmp_path, [], launcher)
    _seed_waiting(_ws(tmp_path), "A-1")
    result = env.invoke("resume", "A-1", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 0
    assert launcher.calls == 1
    assert read_stop_text(RunRecord.open(_ws(tmp_path), "A-1").path) == _rewrite("A-1")


def test_waiting_item_with_stopped_md_shows_it_without_call(tmp_path: Path, stops: dict[str, Any]) -> None:
    ws = _ws(tmp_path)
    rec = RunRecord.open(ws, "W-1")
    rec.set_checkpoint("C3")
    write_stop_record(
        rec.path,
        build_stop_record(
            "W-1", checkpoint="C3", stage="build", stage_status=None, detail="raw words", resume_command="r", now=NOW
        ),
    )
    write_stop_text(rec.path, "Rewritten words.\nSecond line.")
    launcher = Launcher()
    env = _env(tmp_path, [], launcher)
    result = _run(env)
    assert result.exit_code == 0
    assert launcher.calls == 0
    assert "Awaiting you. " in env.digest
    assert "   Rewritten words." in env.digest
    assert "raw words" not in env.digest
    status = env.invoke("status", "--config", str(env.config)).output
    assert "    Rewritten words.\n    Second line." in status
    assert "raw words" not in status
    # with the step off, an existing file is still shown, and it is not rewritten
    assert read_stop_text(rec.path) == "Rewritten words.\nSecond line."


def test_status_prints_raw_when_file_absent(tmp_path: Path, stops: dict[str, Any]) -> None:
    rec = RunRecord.open(_ws(tmp_path), "W-1")
    rec.set_checkpoint("C3")
    write_stop_record(
        rec.path,
        build_stop_record(
            "W-1", checkpoint="C3", stage="build", stage_status=None, detail="raw words", resume_command="r", now=NOW
        ),
    )
    env = _env(tmp_path, [], Launcher())
    assert "Detail: raw words" in env.invoke("status", "--config", str(env.config)).output


def test_stop_record_identical_with_and_without_step(tmp_path: Path, stops: dict[str, Any]) -> None:
    stops["mode"] = {"A-1": "stop"}
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    on_launcher, off_launcher = Launcher(), Launcher()
    on = _env(tmp_path / "on", [], on_launcher)
    off = _env(tmp_path / "off", [], off_launcher)
    _seed_waiting(tmp_path / "on" / "ws", "A-1")
    _seed_waiting(tmp_path / "off" / "ws", "A-1")
    on_rec = RunRecord.open(tmp_path / "on" / "ws", "A-1").path
    off_rec = RunRecord.open(tmp_path / "off" / "ws", "A-1").path
    seed = stop_record_path(on_rec).read_bytes()
    on_result = on.invoke("resume", "A-1", "--config", str(on.config), "--base", "b")
    off_result = off.invoke("resume", "A-1", "--config", str(off.config), "--base", "b", "--raw")
    assert on_result.exit_code == 0
    assert off_result.exit_code == 0
    assert on_launcher.calls == 1
    assert off_launcher.calls == 0
    assert stop_text_path(on_rec).exists()
    assert not stop_text_path(off_rec).exists()
    a = stop_record_path(on_rec)
    b = stop_record_path(off_rec)
    assert a.read_bytes() != seed  # resume really ran and rewrote the stop record
    assert a.read_bytes().replace(str(tmp_path / "on").encode(), b"") == b.read_bytes().replace(
        str(tmp_path / "off").encode(), b""
    )


def test_write_failure_does_not_change_exit_or_hide_stop(
    tmp_path: Path, stops: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    stops["mode"] = {"A-1": "stop"}

    def boom(path: Path, text: str) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "write_stop_text", boom)
    env = _env(tmp_path, ["A-1"], Launcher())
    result = _run(env)
    assert result.exit_code == 0
    assert f"A-1: stopped at C5: {_detail('A-1')}" in result.output
    assert "Detail:" in env.digest
    assert "warning: cannot write stop text for A-1: disk full" in result.stderr
    assert not stop_text_path(RunRecord.open(_ws(tmp_path), "A-1").path).exists()
    assert list(RunRecord.open(_ws(tmp_path), "A-1").path.parent.glob("*.stopped.md*")) == []
    status = env.invoke("status", "--config", str(env.config)).output
    assert "Detail:" in status
    assert "Decision needed." not in status


def test_one_item_rejected_by_guard_mixes_raw_and_rewritten(tmp_path: Path, stops: dict[str, Any]) -> None:
    stops["mode"] = {"A-1": "stop", "A-2": "stop"}
    launcher = Launcher(texts={"A-1": _rewrite("A-1"), "A-2": "Decision needed."})
    env = _env(tmp_path, ["A-1", "A-2"], launcher, "\n[limits]\nmax_tickets_per_run = 2\n")
    result = _run(env)
    assert result.exit_code == 0
    assert launcher.calls == 1
    rejected = [line for line in result.stderr.splitlines() if line.startswith("presentation rejected (")]
    assert len(rejected) == 1
    assert "A-2:" in rejected[0]
    assert "A-1:" not in rejected[0]
    ok = RunRecord.open(_ws(tmp_path), "A-1").path
    bad = RunRecord.open(_ws(tmp_path), "A-2").path
    assert read_stop_text(ok) == _rewrite("A-1")
    assert not stop_text_path(bad).exists()
    assert "A-1: stopped at C5:\n  Decision needed." in result.output
    assert f"A-2: stopped at C5: {_detail('A-2')}" in result.output
    digest = env.digest
    assert _detail("A-1") in digest
    assert f"Detail: {_detail('A-2')}" in digest
    assert "Decision needed." in digest
    assert digest.count("Decision needed.") == 1
    status = env.invoke("status", "--config", str(env.config)).output
    assert "    Decision needed." in status
    assert status.count("Decision needed.") == 1
    assert f"Detail: {_detail('A-2')}" in status


def test_needsyou_entry_dataclass_default_is_raw() -> None:
    assert NeedsYou("i", "C3", "q").text == ""
