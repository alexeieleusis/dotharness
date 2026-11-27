"""Harness: the sisyphus writer and the pr_review reader share one data dir.

Lives in ``tests/`` so it may import both sides; the import-boundary tests scan sisyphus source only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from dotharness.code_host import CheckStatus, MergeableState, RepositoryRef
from dotharness.code_host.testing import InMemoryCodeHost
from dotharness.pr_review.config import HarnessConfig, HarnessSection, MergeConfig, RepoConfig
from dotharness.pr_review.runners import merge
from dotharness.pr_review.runners.merge import gate_record_path, read_gate_record
from dotharness.sisyphus.gaterecord import write_gate_record

REPO_SLUG = "acme-frontend"
PR_NUMBER = 42
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def shared_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, str, int]]:
    """Redirect writer and reader to one temp data dir; yield (data_dir, repo_slug, pr_number).

    Both sides resolve through ``dotharness.paths.data_dir()`` at call time, which calls ``Path.home()``,
    so one ``Path.home`` patch moves both. Undone by ``monkeypatch`` after each test.
    """
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    yield home / ".local" / "share" / "dotharness", REPO_SLUG, PR_NUMBER


def test_writer_and_reader_resolve_the_same_path(shared_data_dir: tuple[Path, str, int]) -> None:
    data_dir, slug, pr = shared_data_dir

    writer_path = write_gate_record(slug, pr, HEAD_SHA)
    reader_path = gate_record_path(slug, pr)

    assert writer_path == reader_path
    assert writer_path.is_relative_to(data_dir)
    assert writer_path.is_file()
    assert read_gate_record(slug, pr) == HEAD_SHA


REPO = RepositoryRef("acme/frontend")


@dataclass
class MergeWorld:
    """Merge-side scaffold: stubbed host and a config; every condition but the gate record holds."""

    host: InMemoryCodeHost
    cfg: HarnessConfig
    number: int
    head_sha: str


def merged_calls(host: InMemoryCodeHost) -> list:
    """The recorded ``squash_merge`` calls, in order."""
    return [c for c in host.writes if c.operation == "ChangeRequests.squash_merge"]


async def run_merge(cfg: HarnessConfig, **kw) -> None:
    await merge.run(cfg, **kw)


@pytest.fixture
def merge_world(shared_data_dir: tuple[Path, str, int], tmp_path: Path) -> Iterator[MergeWorld]:
    """One own PR at ``HEAD_SHA`` passing every merge condition except the (unwritten) gate record.

    Reuses ``shared_data_dir`` (one fixture, one directory); writes no record and creates no state dir.
    """
    _, _slug, number = shared_data_dir
    host = InMemoryCodeHost(repository="acme/frontend", user="me")
    cfg = HarnessConfig(
        harness=HarnessSection(
            "opencode", "echo tok", knowledge_dir=tmp_path / "k", path_prepend=[], env={}, backend_timeout_seconds=10
        ),
        repo=RepoConfig("acme/frontend", tmp_path),
        merge=MergeConfig(enabled=True, max_changed_lines=500, require_all_files_viewed=True),
    )
    host.add_change_request(REPO, head_reference="feat", author="me", head_revision=HEAD_SHA, number=number)
    items = host._change_requests[REPO]
    items[:] = [
        replace(i, mergeable=MergeableState.MERGEABLE, additions=100, deletions=20) if i.number == number else i
        for i in items
    ]
    host._version += 1
    host.add_check(REPO, number, name="build", status=CheckStatus.PASS, required=True)
    host._viewed[(REPO, number)] = {"a.py": True, "b.py": True}
    with (
        patch("dotharness.pr_review.runners.merge.acquire_lock"),
        patch("dotharness.pr_review.runners.common.create_code_host", side_effect=lambda _selection: host.code_host()),
    ):
        yield MergeWorld(host, cfg, number, HEAD_SHA)


async def test_merge_without_gate_record_skips_and_makes_no_merge_call(
    merge_world: MergeWorld, caplog: pytest.LogCaptureFixture
) -> None:
    world = merge_world
    assert world.cfg.repo_slug == REPO_SLUG
    assert not gate_record_path(world.cfg.repo_slug, world.number).exists()

    with caplog.at_level(logging.INFO):
        await run_merge(world.cfg)

    assert f"PR #{world.number}: skipped: {merge.SKIP_GATE_RECORD_MISSING}" in caplog.text
    assert merged_calls(world.host) == []
    assert world.host.writes == []


async def test_scaffold_merges_once_gate_record_matches_head(merge_world: MergeWorld) -> None:
    """Scaffold self-check: with a matching record every other condition holds, so the merge call is observable."""
    world = merge_world
    write_gate_record(world.cfg.repo_slug, world.number, world.head_sha)

    await run_merge(world.cfg)

    assert [(c.args, c.kwargs) for c in merged_calls(world.host)] == [
        ((REPO, world.number), {"expected_head": world.head_sha})
    ]


async def test_sisyphus_gate_record_reaches_merge_call(
    merge_world: MergeWorld, shared_data_dir: tuple[Path, str, int]
) -> None:
    """G6: a record written by the sisyphus writer lets ``pr_review merge`` reach its one merge call, no manual step."""
    data_dir, slug, _ = shared_data_dir
    world = merge_world
    assert world.cfg.repo_slug == slug
    # Gate-record condition stays live: nothing is on disk until the writer runs.
    assert read_gate_record(slug, world.number) is None

    written = write_gate_record(slug, world.number, world.head_sha)
    await run_merge(world.cfg)

    expected = data_dir / "state" / slug / f"build_gate_{world.number}.json"
    assert written == expected
    assert gate_record_path(world.cfg.repo_slug, world.number) == expected
    assert expected.is_file()
    assert json.loads(expected.read_text()) == {"head_sha": world.head_sha}
    assert read_gate_record(world.cfg.repo_slug, world.number) == world.head_sha
    assert [c.operation for c in world.host.writes] == ["ChangeRequests.squash_merge"]
    assert [(c.args, c.kwargs) for c in merged_calls(world.host)] == [
        ((REPO, world.number), {"expected_head": world.head_sha})
    ]


async def test_stale_gate_record_skips_and_makes_no_merge_call(
    merge_world: MergeWorld, caplog: pytest.LogCaptureFixture
) -> None:
    """A record whose ``head_sha`` differs from the PR head is skipped as stale; no merge call."""
    world = merge_world
    stale_sha = "f" * 40
    assert stale_sha != world.head_sha
    written = write_gate_record(world.cfg.repo_slug, world.number, stale_sha)
    assert written == gate_record_path(world.cfg.repo_slug, world.number)
    assert read_gate_record(world.cfg.repo_slug, world.number) == stale_sha

    with caplog.at_level(logging.INFO):
        await run_merge(world.cfg)

    assert f"PR #{world.number}: skipped: {merge.SKIP_GATE_RECORD_STALE}" in caplog.text
    assert merge.SKIP_GATE_RECORD_MISSING not in caplog.text
    assert merged_calls(world.host) == []
    assert world.host.writes == []


async def test_missing_gate_record_in_shared_dir_skips_and_makes_no_merge_call(
    merge_world: MergeWorld, caplog: pytest.LogCaptureFixture
) -> None:
    """With nothing written to the shared data dir the PR is skipped as missing; no merge call."""
    world = merge_world
    assert read_gate_record(world.cfg.repo_slug, world.number) is None

    with caplog.at_level(logging.INFO):
        await run_merge(world.cfg)

    assert f"PR #{world.number}: skipped: {merge.SKIP_GATE_RECORD_MISSING}" in caplog.text
    assert merge.SKIP_GATE_RECORD_STALE not in caplog.text
    assert merged_calls(world.host) == []
    assert world.host.writes == []
