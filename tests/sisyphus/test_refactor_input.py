"""``_Run`` refactor-input helpers: added-path listing and the per-group input file."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from dotharness.sisyphus import orchestrator, prbody
from dotharness.sisyphus.orchestrator import REFACTOR_INPUT_FILE, OrchestratorError
from dotharness.sisyphus.stack import StackGroup, is_heavily_modified
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
    assert await run.refactor_groups() == ((), {})
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
    assert set(entries[0]) == {"set", "files", "tests", "evidence", "fallback", "head_before", "modified"}
    assert entries[0]["modified"] == []
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


class GitShell:
    """Scripted git: name-status listing, per-path numstat and blob text."""

    def __init__(self, status: str, numstat: dict[str, str], blobs: dict[str, str | None], head: str = SHA_A) -> None:
        self.status = status
        self.numstat = numstat
        self.blobs = blobs
        self.head = head
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, argv: Sequence[str], cwd: Path | None) -> CommandResult:
        a = tuple(argv)
        self.calls.append(a)
        if a[1] == "diff" and "--name-status" in a:
            return CommandResult(0, self.status, "")
        if a[1] == "diff" and "--numstat" in a:
            out = self.numstat.get(a[-1])
            return CommandResult(1, "", "bad") if out is None else CommandResult(0, out, "")
        if a[1] == "show":
            blob = self.blobs.get(a[2].split(":", 1)[1])
            return CommandResult(128, "", "missing") if blob is None else CommandResult(0, blob, "")
        if a[1:] == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{self.head}\n", "")
        raise AssertionError(a)


def _lines(n: int, trailing: bool = True) -> str:
    return "x\n" * n if trailing else "x\n" * (n - 1) + "x"


@pytest.mark.parametrize(
    ("added", "total", "expected"),
    [(50, 100, True), (49, 100, False), (1, 2, True), (1, 3, False), (0, 0, False), (5, 0, False), (100, 100, True)],
)
def test_share_rule(added: int, total: int, expected: bool) -> None:
    assert is_heavily_modified(added, total) is expected


async def test_modified_listing_boundaries(tmp_path: Path) -> None:
    names = ["a50.py", "a49.py", "h1of2.py", "t1of3.py", "nonl.py", "zero.py", "bin.py", "foo.test.ts", "README.md"]
    status = "".join(f"M\0{n}\0" for n in names)
    status += "R090\0old.py\0moved.py\0D\0gone.py\0A\0new.py\0C75\0s.py\0c.py\0"
    numstat = {
        "a50.py": "50\t3\ta50.py\n",
        "a49.py": "49\t3\ta49.py\n",
        "h1of2.py": "1\t0\th1of2.py\n",
        "t1of3.py": "1\t0\tt1of3.py\n",
        "nonl.py": "50\t0\tnonl.py\n",
        "zero.py": "5\t0\tzero.py\n",
        "bin.py": "-\t-\tbin.py\n",
        "foo.test.ts": "100\t0\tfoo.test.ts\n",
        "README.md": "100\t0\tREADME.md\n",
    }
    blobs: dict[str, str | None] = {
        "a50.py": _lines(100),
        "a49.py": _lines(100),
        "h1of2.py": _lines(2),
        "t1of3.py": _lines(3),
        "nonl.py": _lines(100, trailing=False),
        "zero.py": "",
        "bin.py": "x",
    }
    shell = GitShell(status, numstat, blobs)
    run = _run(tmp_path, shell)  # type: ignore[arg-type]
    units = await run.list_modified_code_units()
    assert units == {"a50.py": 0.5, "h1of2.py": 0.5, "nonl.py": 0.5}
    assert not any("--no-renames" in c for c in shell.calls)
    assert shell.calls[0] == ("git", "diff", "--name-status", "-z", f"origin/{run.opt.base_branch}...HEAD")
    # Non-source and test paths never reach numstat.
    assert not any(c[-1] in ("README.md", "foo.test.ts") for c in shell.calls)


async def test_modified_git_failure_skips_path(tmp_path: Path) -> None:
    status = "M\0bad_numstat.py\0M\0no_blob.py\0M\0ok.py\0"
    numstat = {"no_blob.py": "9\t0\tno_blob.py\n", "ok.py": "9\t0\tok.py\n"}
    blobs: dict[str, str | None] = {"ok.py": _lines(10)}
    run = _run(tmp_path, GitShell(status, numstat, blobs))  # type: ignore[arg-type]
    assert await run.list_modified_code_units() == {"ok.py": 0.9}


async def test_ratio_rounded_two_places(tmp_path: Path) -> None:
    shell = GitShell("M\0a.py\0", {"a.py": "2\t0\ta.py\n"}, {"a.py": _lines(3)})
    run = _run(tmp_path, shell)  # type: ignore[arg-type]
    assert await run.list_modified_code_units() == {"a.py": 0.67}


async def test_union_and_ratios(tmp_path: Path) -> None:
    status = "A\0new.py\0M\0old.py\0A\0tests/test_new.py\0M\0tests/test_old.py\0"
    shell = GitShell(status, {"old.py": "8\t0\told.py\n", "tests/test_old.py": "9\t0\tx\n"}, {"old.py": _lines(10)})
    run = _run(tmp_path, shell)  # type: ignore[arg-type]
    groups, ratios = await run.refactor_groups(pattern_set="fastapi")
    assert ratios == {"old.py": 0.8}
    assert len(groups) == 1
    assert groups[0].files == ("new.py", "old.py")
    assert groups[0].tests == ("tests/test_new.py",)
    assert groups[0].fallback is False


async def test_union_two_stacks_in_set_order(tmp_path: Path) -> None:
    status = "A\0api.py\0M\0web/old.tsx\0"
    shell = GitShell(status, {"web/old.tsx": "10\t0\tx\n"}, {"web/old.tsx": _lines(10)})
    run = _run(tmp_path, shell)  # type: ignore[arg-type]
    groups, _ = await run.refactor_groups()
    assert [g.set for g in groups] == ["generic"]
    assert groups[0].files == ("api.py", "web/old.tsx")


async def test_resume_measures_at_recorded_head(tmp_path: Path) -> None:
    shell = GitShell("M\0a.py\0", {"a.py": "9\t0\ta.py\n"}, {"a.py": _lines(10)}, head=SHA_A)
    run = _run(tmp_path, shell)  # type: ignore[arg-type]
    await run.record_group_entry(_group("generic", ("a.py",)), {"a.py": 0.9})
    shell.calls.clear()
    shell.head = SHA_B
    await run.refactor_groups()
    names = [c for c in shell.calls if "--name-status" in c]
    assert names and all(c[-1] == f"origin/{run.opt.base_branch}...{SHA_A}" for c in names)
    shows = [c for c in shell.calls if c[1] == "show"]
    assert shows == [("git", "show", f"{SHA_A}:a.py")]


async def test_modified_key_per_group_sorted_rounded(tmp_path: Path) -> None:
    run = _run(tmp_path, Shell())
    ratios = {"z.py": 0.5, "b.py": 0.666, "other.py": 0.9}
    entry = await run.record_group_entry(_group("generic", ("z.py", "b.py", "n.py")), ratios)
    assert entry["modified"] == [{"path": "b.py", "ratio": 0.67}, {"path": "z.py", "ratio": 0.5}]
    assert run.read_refactor_input()[0]["modified"] == entry["modified"]


async def test_old_entries_load_strict(tmp_path: Path) -> None:
    run = _run(tmp_path, Shell())
    run.handoff_dir.mkdir(parents=True)
    old = '[{"set":"x","files":[],"tests":[],"evidence":[],"fallback":false,"head_before":"h"}]'
    (run.handoff_dir / REFACTOR_INPUT_FILE).write_text(old)
    assert run.read_refactor_input()[0].get("modified", []) == []
