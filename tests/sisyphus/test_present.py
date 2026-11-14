"""Presentation core: prompt, backend call, split and guard (injectable launcher, no real backend)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dotharness.ai_backend import ClaudeOptions, OpencodeOptions, RunResult
from dotharness.opencode_guards import OpencodePluginError
from dotharness.sisyphus.present import (
    build_prompt,
    commit_hashes,
    file_paths,
    guard,
    present_stops,
    skill_body,
    split_output,
)
from dotharness.sisyphus.stoprecord import StopRecord

HASH_A = "a1b2c3d4e5f6"
HASH_B = "0123456789abcdef"


def _record(item: str, detail: str, resume: str | None = None) -> StopRecord:
    return StopRecord(
        item=item,
        kind="checkpoint",
        stage="build",
        stage_status=None,
        checkpoint="C5",
        questions=(),
        detail=detail,
        action="Answer it.",
        resume_command=resume or f"sisyphus resume '{item}' --config /c.toml",
        stopped_at="2026-01-01T00:00:00Z",
    )


R1 = _record("#194", f"Commit {HASH_A} changed src/a/b.py. May be safe. Decide.")
R2 = _record("#195", f"Commit {HASH_B} touched docs/x.md.")


def _good(records: list[StopRecord]) -> str:
    return json.dumps({r.item: f"Decision needed: x. {r.detail} {r.resume_command}" for r in records})


class Fake:
    def __init__(self, result: RunResult | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.cwd_was_empty: bool | None = None

    async def __call__(self, *args: Any, **kw: Any) -> RunResult:
        self.calls.append((args, kw))
        self.cwd_was_empty = not any(Path(kw["cwd"]).iterdir())
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _ok(stdout: str, **kw: Any) -> RunResult:
    return RunResult(
        kw.pop("returncode", 0),
        stdout,
        kw.pop("stderr", ""),
        timed_out=kw.pop("timed_out", False),
        not_found=kw.pop("not_found", False),
    )


async def _run(fake: Fake, records: list[StopRecord] | None = None, backend: str | None = "claude") -> Any:
    return await present_stops(records or [R1, R2], backend=backend, launcher=fake, timeout_minutes=5)


async def test_two_items_one_call_and_both_kept() -> None:
    fake = Fake(_ok(_good([R1, R2])))
    result = await _run(fake)
    assert len(fake.calls) == 1
    assert result.failure is None
    assert set(result.texts) == {"#194", "#195"}
    assert fake.calls[0][1]["timeout"] == 300


def test_prompt_contents() -> None:
    body = skill_body()
    prompt = build_prompt([R1, R2], body)
    assert body in prompt
    assert not body.startswith("---")
    assert "version: 2.1.0" not in prompt
    assert "name: asd-ste100" not in prompt
    for rec in (R1, R2):
        for part in (rec.item, "C5", "build", rec.detail, f"Action: {rec.action}", f"Resume: {rec.resume_command}"):
            assert part in prompt
    assert '"#194", "#195"' in prompt
    assert "Strict" in prompt
    assert "this section wins" in prompt
    assert "Decision needed" in prompt


async def test_opencode_options_and_empty_cwd() -> None:
    fake = Fake(_ok(_good([R1, R2])))
    await _run(fake, backend="opencode")
    kw = fake.calls[0][1]
    assert fake.calls[0][0][0] == "opencode"
    assert kw["backend_options"] == OpencodeOptions(check_plugins=True)
    assert fake.cwd_was_empty is True


async def test_claude_options_and_empty_cwd_removed_after() -> None:
    fake = Fake(_ok(_good([R1, R2])))
    await _run(fake)
    kw = fake.calls[0][1]
    assert kw["backend_options"] == ClaudeOptions(
        disable_slash_commands=True, skip_permissions=False, allowed_tools=None
    )
    assert fake.cwd_was_empty is True
    assert not Path(kw["cwd"]).exists()


async def test_tempdir_removed_on_failure() -> None:
    fake = Fake(RuntimeError("boom"))
    result = await _run(fake)
    assert result.failure
    assert not Path(fake.calls[0][1]["cwd"]).exists()


@pytest.mark.parametrize(
    ("result", "needle"),
    [
        (_ok("", returncode=124, timed_out=True), "timed out after 5 minutes"),
        (_ok("", returncode=3), "exited with status 3"),
        (_ok("", returncode=127, not_found=True), "cannot start"),
        (_ok("  \n"), "no output"),
        (_ok("not json"), "not valid JSON"),
        (_ok("[1]"), "not a JSON object"),
        (_ok(json.dumps({"#194": "x"})), "lacks a key"),
        (_ok(json.dumps({"#194": "x", "#195": 3})), "non-string"),
    ],
)
async def test_failure_causes_do_not_raise(result: RunResult, needle: str) -> None:
    out = await _run(Fake(result))
    assert out.failure is not None
    assert needle in out.failure
    assert out.texts == {}


@pytest.mark.parametrize(
    "exc", [OpencodePluginError("plugins found"), ValueError("bad"), TypeError("bad"), RuntimeError("x")]
)
async def test_launcher_exceptions_become_failure(exc: Exception) -> None:
    out = await _run(Fake(exc))
    assert out.failure


async def test_unset_backend_is_failure_without_call() -> None:
    fake = Fake(_ok(_good([R1, R2])))
    out = await _run(fake, backend=None)
    assert out.failure is not None
    assert "backend" in out.failure
    assert fake.calls == []


async def test_keyboard_interrupt_propagates() -> None:
    with pytest.raises(KeyboardInterrupt):
        await _run(Fake(KeyboardInterrupt()))


async def test_typer_exit_propagates() -> None:
    import typer

    with pytest.raises(typer.Exit):
        await _run(Fake(typer.Exit(2)))


async def test_extra_key_ignored_and_fenced_json_accepted() -> None:
    data = json.loads(_good([R1, R2]))
    data["#999"] = "extra"
    out = await _run(Fake(_ok("```json\n" + json.dumps(data) + "\n```")))
    assert out.failure is None
    assert set(out.texts) == {"#194", "#195"}


async def test_dropped_hash_rejects_only_that_item() -> None:
    data = json.loads(_good([R1, R2]))
    data["#194"] = data["#194"].replace(HASH_A, "xxxx")
    out = await _run(Fake(_ok(json.dumps(data))))
    assert out.failure is None
    assert set(out.texts) == {"#195"}
    assert "hash" in out.rejected["#194"]


def test_guard_path_and_resume() -> None:
    ok = f"{HASH_A} src/a/b.py {R1.resume_command}"
    assert guard(R1, ok) is None
    assert "path" in (guard(R1, f"{HASH_A} {R1.resume_command}") or "")
    assert "resume" in (guard(R1, f"{HASH_A} src/a/b.py") or "")
    assert guard(R1, f"{HASH_A.upper()} src/a/b.py {R1.resume_command}") is None
    assert guard(R1, "  ") is not None


def test_hash_pattern() -> None:
    assert commit_hashes("see abcdef1, ABCDEF12 and abcdef1.") == ["abcdef1", "abcdef12"]
    assert commit_hashes("abcdef (6) and xabcdef1 and abcdef1_ and " + "a" * 41) == []
    assert commit_hashes("(" + "a" * 40 + ")") == ["a" * 40]


def test_path_pattern() -> None:
    text = 'edit `src/a/b.py`, (docs/x.md); "tests/t.test.ts". and/or dir/sub ./noext/ x.py a/b.'
    assert file_paths(text) == ["src/a/b.py", "docs/x.md", "tests/t.test.ts"]


def test_split_output_direct() -> None:
    assert split_output('{"a": "x", "b": "y", "c": "z"}', ["a", "b"]) == {"a": "x", "b": "y"}
    with pytest.raises(ValueError, match="no output"):
        split_output("", ["a"])
