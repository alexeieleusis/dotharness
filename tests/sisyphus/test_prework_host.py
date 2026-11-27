"""R-5 over the in-memory code host: the pre-work draft PR, the stacked feature PR, and the no-commit case."""

# ruff: noqa: F811
from __future__ import annotations

import pytest

from dotharness.code_host.errors import CodeHostError
from dotharness.code_host.models import ChangeRequestState
from dotharness.code_host.testing import InMemoryCodeHost
from dotharness.sisyphus import cli, orchestrator
from dotharness.sisyphus.cli import HostRepository
from dotharness.sisyphus.rails import Checkpoint
from tests.sisyphus.test_orchestrator import ITEM, World, world  # noqa: F401  (fixture)


def _run(world: World) -> tuple[orchestrator._Run, InMemoryCodeHost, HostRepository]:
    fake = InMemoryCodeHost()
    repo = HostRepository(fake.code_host())
    run = orchestrator._Run(
        ITEM, world.config(), world.options(), world.agent, world.shell, cli.HostPullRequestClient(repo), world.clock
    )
    return run, fake, repo


async def _open(fake: InMemoryCodeHost, repo: HostRepository) -> list:
    return list(fake.change_requests.get(await repo.ref(), ()))


async def test_commits_give_one_draft_pr_against_the_resolved_base_and_the_feature_stacks_on_it(world: World) -> None:
    run, fake, repo = _run(world)
    world.ahead = "1"
    await run.prework_publish(("tools/pkg/a.py",))
    (pr,) = await _open(fake, repo)
    assert pr.head_reference == run.prework_branch
    assert pr.base_reference == "base-branch"
    assert pr.title.startswith("Prework: ")
    assert pr.is_draft
    assert pr.state is ChangeRequestState.OPEN
    assert pr.body.splitlines()[0].startswith(f"Code-health preparation for {ITEM}")
    assert f"Closes {ITEM}" not in pr.body
    assert not [w for w in fake.writes if w.operation != "ChangeRequests.create_draft"]  # no label, reviewer, merge

    await run.stage_open_pr()
    prs = await _open(fake, repo)
    assert len(prs) == 2
    feature = next(p for p in prs if p.head_reference == run.branch)
    assert feature.base_reference == run.prework_branch
    assert sum("pre-work PR" in line for line in feature.body.splitlines()) == 1
    assert pr.url in feature.body or f"#{pr.number}" in feature.body


async def test_no_commits_open_no_pr_and_the_feature_targets_the_resolved_base(world: World) -> None:
    run, fake, repo = _run(world)
    world.ahead = "0"
    await run.prework_publish(("tools/pkg/a.py",))
    assert await _open(fake, repo) == []
    await run.stage_open_pr()
    (feature,) = await _open(fake, repo)
    assert feature.head_reference == run.branch
    assert feature.base_reference == "base-branch"
    assert "pre-work" not in feature.body


async def test_publishing_twice_leaves_one_pr(world: World) -> None:
    run, fake, repo = _run(world)
    world.ahead = "1"
    await run.prework_publish(("tools/pkg/a.py",))
    await run.prework_publish(("tools/pkg/a.py",))
    assert len(await _open(fake, repo)) == 1


async def test_a_failing_host_stops_at_c8_and_keeps_the_resolved_base(world: World) -> None:
    run, fake, _repo = _run(world)
    fake.fail_always("ChangeRequests.create_draft", CodeHostError("boom"), apply=False)
    world.ahead = "1"
    with pytest.raises(orchestrator._Stop) as exc:
        await run.prework_publish(("tools/pkg/a.py",))
    assert exc.value.checkpoint is Checkpoint.C8
    assert run.rec.base_branch is None
