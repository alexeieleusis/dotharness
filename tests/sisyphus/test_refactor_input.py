"""``_Run`` refactor-input helpers: added-path listing and the per-group input file."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from dotharness.sisyphus import orchestrator, prbody
from dotharness.sisyphus.orchestrator import REFACTOR_INPUT_FILE, OrchestratorError
from dotharness.sisyphus.stack import StackGroup
from dotharness.sisyphus.subproc import CommandResult
from tests.sisyphus.test_orchestrator import ITEM, World

SHA_A = "a" * 40
SHA_B = "b" * 40


class Shell:
    def __init__(self, diff: str = "", head: str = SHA_A) -> None:
        self.diff = diff
        self.head = head
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, argv: Sequence[str], cwd: Path | None) -> CommandResult:
        self.calls.append(tuple(argv))
        if argv[1] == "diff":
            return CommandResult(0, self.diff, "")
        if argv[1:] == ["rev-parse", "HEAD"]:
            return CommandResult(0, f"{self.head}\n", "")
        raise AssertionError(argv)


def _run(tmp_path: Path, shell: Shell) -> orchestrator._Run:
    w = World(tmp_path)
    return orchestrator._Run(ITEM, w.config(), w.options(), w.agent, shell, w, w.clock, w)


def _group(name: str, files: tuple[str, ...] = ("a.py",)) -> StackGroup:
    return StackGroup(set=name, files=files, tests=("tests/test_a.py",), evidence=("e",), fallback=name == "generic")


async def test_only_added_paths_and_command(tmp_path: Path) -> None:
    diff = "A\0new.py\0M\0m.py\0D\0d.py\0R100\0old.py\0moved.py\0C75\0src.py\0copy.py\0T\0t.py\0A\0z.py\0"
    shell = Shell(diff)
    run = _run(tmp_path, shell)
    assert await run.list_added_paths() == ["new.py", "z.py"]
    cmd = shell.calls[0]
    assert cmd == ("git", "diff", "--name-status", "-z", f"origin/{run.opt.base_branch}...HEAD")
    assert "--no-renames" not in cmd


async def test_odd_names(tmp_path: Path) -> None:
    names = ["with space.py", "new\nline.py"]
    run = _run(tmp_path, Shell("".join(f"A\0{n}\0" for n in names)))
    assert await run.list_added_paths() == names


async def test_empty_writes_nothing(tmp_path: Path) -> None:
    run = _run(tmp_path, Shell("A\0README.md\0"))
    assert await run.refactor_groups() == ()
    assert not run.handoff_dir.exists()
    assert run.read_refactor_input() == []


async def test_diff_failure_raises(tmp_path: Path) -> None:
    class Bad(Shell):
        async def __call__(self, argv: Sequence[str], cwd: Path | None) -> CommandResult:
            return CommandResult(1, "", "boom")

    with pytest.raises(OrchestratorError):
        await _run(tmp_path, Bad()).list_added_paths()


async def test_record_once_and_ordering(tmp_path: Path) -> None:
    shell = Shell(head=SHA_A)
    run = _run(tmp_path, shell)
    assert not run.handoff_dir.exists()  # directory created by the first record
    first = await run.record_group_entry(_group("fastapi"))
    path = run.handoff_dir / REFACTOR_INPUT_FILE
    assert first["head_before"] == SHA_A
    assert [e["set"] for e in run.read_refactor_input()] == ["fastapi"]
    before = path.read_bytes()
    shell.head = SHA_B
    again = await run.record_group_entry(_group("fastapi", ("other.py",)))
    assert again == first
    assert path.read_bytes() == before
    second = await run.record_group_entry(_group("generic"))
    entries = run.read_refactor_input()
    assert [e["set"] for e in entries] == ["fastapi", "generic"]
    assert [e["head_before"] for e in entries] == [SHA_A, SHA_B]
    assert second == entries[1]
    assert set(entries[0]) == {"set", "files", "tests", "evidence", "fallback", "head_before"}
    assert entries[0]["tests"] == ["tests/test_a.py"]


@pytest.mark.parametrize("fail", [False, True])
async def test_no_temp_files_left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    run = _run(tmp_path, Shell())
    if fail:

        def boom(*_a: object, **_k: object) -> None:
            raise OSError("no replace")  # noqa: TRY003

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="no replace"):
            await run.record_group_entry(_group("fastapi"))
        assert not (run.handoff_dir / REFACTOR_INPUT_FILE).exists()
    else:
        await run.record_group_entry(_group("fastapi"))
    assert [p.name for p in run.handoff_dir.iterdir() if p.name.startswith(".tmp-")] == []


@pytest.mark.parametrize("text", ["{", '{"a": 1}', "[1]", '[{"set": "x"}]'])
async def test_malformed_raises_and_is_kept(tmp_path: Path, text: str) -> None:
    run = _run(tmp_path, Shell())
    run.handoff_dir.mkdir(parents=True)
    path = run.handoff_dir / REFACTOR_INPUT_FILE
    path.write_text(text)
    with pytest.raises(OrchestratorError, match="unreadable refactor input"):
        run.read_refactor_input()
    with pytest.raises(OrchestratorError):
        await run.record_group_entry(_group("x"))
    assert path.read_text() == text


def test_not_a_leaf_handoff(tmp_path: Path) -> None:
    (tmp_path / REFACTOR_INPUT_FILE).write_text("[]")
    stems = ["01-foo", "foo", "22-refactor-input"]
    assert all(v == [] for v in prbody.select_handoffs(tmp_path, stems).values())
