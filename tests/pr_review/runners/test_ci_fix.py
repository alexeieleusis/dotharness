import contextlib
import logging
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from dotharness.code_host import (
    ChangeRequestState,
    Check,
    CheckStatus,
    CodeHostError,
    CodeHostTimeoutError,
    MergeableState,
    RepositoryRef,
    UnsupportedCapabilityError,
)
from dotharness.code_host.testing import SECRET, InMemoryCodeHost
from dotharness.pr_review.config import CiFixConfig, HarnessConfig, HarnessSection, RepoConfig
from dotharness.pr_review.runners import ci_fix
from dotharness.pr_review.runners.ci_fix import DIGEST_PREFIX, CiFixError, _Step
from dotharness.pr_review.runners.common import FatalGitError

P = "dotharness.pr_review.runners.ci_fix."


@contextlib.contextmanager
def _patch_run_cmd(side_effect):
    """Patch ``run_cmd`` where ci_fix uses it and in ``common`` (``git_abort_rebase`` runs git from there)."""
    with (
        patch(P + "run_cmd", side_effect=side_effect),
        patch("dotharness.pr_review.runners.common.run_cmd", side_effect=side_effect),
    ):
        yield


SLUG = "acme-frontend"
REPO = RepositoryRef("acme/frontend")
LOG = "quality\tRun checks\t2024Z error"
T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


def _pr(number=1, branch="feat", author="me", draft=False, state=ChangeRequestState.OPEN):
    return {"number": number, "head_reference": branch, "author": author, "is_draft": draft, "state": state}


def _cfg(tmp_path, enabled=True, max_attempts=3):
    knowledge = tmp_path / "k" / "pr-review"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "ci-fix-prompt.md").write_text("PROMPT TEXT", encoding="utf-8")
    (knowledge / "ci-fix-rebase-prompt.md").write_text("REBASE PROMPT TEXT \u00e9", encoding="utf-8")
    return HarnessConfig(
        harness=HarnessSection(
            "opencode", "echo tok", knowledge_dir=tmp_path / "k", path_prepend=[], env={}, backend_timeout_seconds=10
        ),
        repo=RepoConfig("acme/frontend", tmp_path),
        ci_fix=CiFixConfig(enabled=enabled, max_fix_attempts=max_attempts),
    )


def _proc(returncode=0, stdout=b"", stderr=b""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _update(host, number, **fields):
    items = host._change_requests[REPO]
    for i, item in enumerate(items):
        if item.number == number:
            items[i] = replace(item, **fields)
    host._version += 1


def _seed_checks(host, number, names, *, status=CheckStatus.FAIL, log=LOG, failed_step="Run checks"):
    """Required checks named ``names`` on a PR, each with a job (id ``<number>0<index>``) carrying ``log``."""
    for index, name in enumerate(names):
        job_id = f"{number}0{index}"
        host.add_check(REPO, number, name=name, status=status, required=True, job_id=job_id)
        host.add_job(REPO, job_id=job_id, failed_step=failed_step, log=log)


class _GetOverride:
    """The ``ChangeRequests`` facet with ``get`` answering as if the PR had changed since it was listed."""

    def __init__(self, inner, fields):
        self._inner = inner
        self._fields = fields

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def get(self, repo, number):
        return replace(await self._inner.get(repo, number), **self._fields)


def _seeded_host(prs, green, failing, log, failed_step):
    host = InMemoryCodeHost(repository="acme/frontend", user="me")
    host.add_repository("acme/frontend")
    for pr in [_pr()] if prs is None else prs:
        host.add_change_request(
            REPO,
            head_reference=pr["head_reference"],
            author=pr["author"],
            is_draft=pr["is_draft"],
            state=pr["state"],
            number=pr["number"],
        )
        if green:
            _seed_checks(host, pr["number"], ["Run checks"], status=CheckStatus.PASS)
        else:
            _seed_checks(host, pr["number"], failing or ["Run checks"], log=log, failed_step=failed_step)
    return host


@contextlib.contextmanager
def _mocks(  # noqa: C901
    tmp_path,
    prs=None,
    green=False,
    repro_rc=1,
    after_rc=0,
    push_rc=0,
    push_stderr=b"! [rejected] (non-fast-forward)",
    get_override=None,
    backend_raises=None,
    status=b"",
    agent_commits=True,
    failing=None,
    log=LOG,
    failed_step="Run checks",
):
    host = _seeded_host(prs, green, failing, log, failed_step)
    code_host = host.code_host()
    if get_override:
        code_host = replace(code_host, change_requests=_GetOverride(code_host.change_requests, get_override))
    head = {"sha": "sha0"}
    status_box = [status]
    pushes: list[list[str]] = []
    cmds: list[list[str]] = []
    step_results = [_proc(repro_rc, b"boom"), _proc(after_rc)]

    def fake_run_cmd(cmd, cwd, env, timeout, check=True, shell=False):
        cmds.append(cmd)
        if cmd[:2] == ["git", "commit"]:
            head["sha"] = f"commit-{head['sha']}"
            return _proc(0)
        if cmd[:2] == ["git", "rev-parse"]:
            return _proc(0, head["sha"].encode())
        if cmd[:2] == ["git", "status"]:
            return _proc(0, status_box[0])
        if cmd[:2] == ["git", "push"]:
            pushes.append(cmd)
            return _proc(push_rc, b"", push_stderr if push_rc else b"")
        if cmd[:2] == ["git", "grep"]:
            return _proc(1)  # no conflict markers
        return _proc(0)

    def backend_run(*args, **kwargs):
        if backend_raises:
            raise backend_raises
        if agent_commits:
            head["sha"] = f"sha-{len(pushes)}-{head['sha']}"
        return _proc(0)

    backend = MagicMock()
    backend.run = AsyncMock(side_effect=backend_run)
    with (
        patch("dotharness.pr_review.state.XDG_DATA", tmp_path / "xdg"),
        patch(P + "acquire_lock", return_value=contextlib.nullcontext()),
        patch("dotharness.pr_review.runners.common.create_code_host", return_value=code_host),
        patch(
            P + "find_step_command",
            return_value=_Step("Run checks", "make check", "src/dotharness/pr_review/runners/pr-review"),
        ),
        patch(P + "git_detach_and_record", return_value="orig"),
        patch(P + "git_fetch_and_checkout") as checkout,
        patch(P + "git_restore") as restore,
        patch("dotharness.pr_review.runners.common.git_clean_working_dir", return_value=True) as clean,
        patch("dotharness.pr_review.runners.common.git_is_dirty", return_value=False) as dirty,
        patch(P + "_run_step", side_effect=step_results) as run_step,
        _patch_run_cmd(side_effect=fake_run_cmd),
        patch(P + "Backend", return_value=backend) as backend_cls,
    ):
        yield SimpleNamespace(
            host=host,
            checkout=checkout,
            restore=restore,
            clean=clean,
            dirty=dirty,
            run_step=run_step,
            pushes=pushes,
            cmds=cmds,
            backend=backend,
            status_box=status_box,
            backend_cls=backend_cls,
            continue_envs=[],
        )


def _ops(m):
    return [c.operation for c in m.host.calls]


def _gate(name):
    return Check(name, CheckStatus.FAIL, None, None, None, None, None)


def _digests(caplog):
    return [r.getMessage() for r in caplog.records if DIGEST_PREFIX in r.getMessage()]


async def test_disabled_is_noop(tmp_path):
    with _mocks(tmp_path) as m, patch(P + "open_host") as opened:
        await ci_fix.run(_cfg(tmp_path, enabled=False))
    opened.assert_not_called()
    assert m.host.calls == []


async def test_ac1_green_pr_no_push_no_checkout(tmp_path):
    with _mocks(tmp_path, green=True) as m:
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    m.checkout.assert_not_called()
    m.restore.assert_not_called()


async def test_ac2_reproduced_and_fixed_pushes_once_to_same_branch(tmp_path):
    with _mocks(tmp_path) as m:
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == [["git", "push", "origin", "feat"]]
    assert ci_fix.get_attempts(SLUG, 1) == 1
    m.checkout.assert_called_once()
    instructions = m.backend.run.call_args.args[0]
    assert instructions.startswith("PROMPT TEXT")
    assert "**Failure to fix**" in instructions
    assert "make check" in instructions
    m.restore.assert_called_once()


async def test_uncommitted_agent_edits_are_committed_before_the_single_push(tmp_path):
    with _mocks(tmp_path, status=b" M f.py", agent_commits=False) as m:
        await ci_fix.run(_cfg(tmp_path))
    add = m.cmds.index(["git", "add", "-u"])
    commit = m.cmds.index(["git", "commit", "-m", "ci-fix: Run checks"])
    assert add < commit < m.cmds.index(["git", "push", "origin", "feat"])
    assert m.pushes == [["git", "push", "origin", "feat"]]
    assert ["git", "add", "-A"] not in m.cmds


async def test_agent_changes_nothing_while_step_passes_is_not_reproduced(tmp_path, caplog):
    with _mocks(tmp_path, agent_commits=False) as m, caplog.at_level(logging.INFO):
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    assert not any(c[:2] == ["git", "commit"] for c in m.cmds)
    assert ci_fix.get_attempts(SLUG, 1) == 0  # the agent changed nothing, so no attempt
    assert any("did-not-reproduce" in d and "no change to the branch" in d for d in _digests(caplog))


async def test_noop_run_with_step_still_failing_is_not_counted(tmp_path):
    with _mocks(tmp_path, agent_commits=False, after_rc=1):
        await ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 0


async def test_repeated_noop_for_same_head_and_check_does_not_rerun_the_agent(tmp_path, caplog):
    with _mocks(tmp_path, agent_commits=False, after_rc=1) as m, caplog.at_level(logging.INFO):
        m.run_step.side_effect = [_proc(1), _proc(1)] * 3
        for _ in range(3):
            await ci_fix.run(_cfg(tmp_path))
    assert m.backend.run.call_count == 1
    assert ci_fix.get_attempts(SLUG, 1) == 0
    assert any("noop-repeat" in d for d in _digests(caplog))


async def test_noop_run_on_dirty_start_tree_is_not_counted(tmp_path):
    with _mocks(tmp_path, agent_commits=False, after_rc=1, status=b" M a.py\n"):
        await ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 0


async def test_run_that_dirties_tree_without_commit_is_counted(tmp_path):
    with _mocks(tmp_path, agent_commits=False, after_rc=1) as m:
        orig = m.backend.run.side_effect

        def run(*a, **k):
            orig(*a, **k)
            m.status_box[0] = b" M a.py\n"
            return _proc(0)

        m.backend.run = AsyncMock()
        m.backend.run.side_effect = run
        await ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 1


@pytest.mark.parametrize("exc", [RuntimeError("agent died"), TimeoutError("timeout")])
async def test_backend_crash_or_timeout_still_counts(tmp_path, exc):
    with _mocks(tmp_path, backend_raises=exc, agent_commits=False), pytest.raises(CiFixError):
        await ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 1


async def test_ac3_cap_reached_across_runs_writes_digest(tmp_path, caplog):
    cfg = _cfg(tmp_path, max_attempts=2)
    for _ in range(2):
        with _mocks(tmp_path) as m:
            await ci_fix.run(cfg)
        assert len(m.pushes) == 1
    assert ci_fix.get_attempts(SLUG, 1) == 2
    with _mocks(tmp_path) as m, caplog.at_level(logging.INFO):
        await ci_fix.run(cfg)
    assert m.pushes == []
    m.checkout.assert_not_called()
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "cap-reached" in digests[0]
    assert "pr=1" in digests[0]


async def test_ac4_not_reproduced_no_push_digest(tmp_path, caplog):
    with _mocks(tmp_path, repro_rc=0) as m, caplog.at_level(logging.INFO):
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    m.backend.run.assert_not_called()
    assert ci_fix.get_attempts(SLUG, 1) == 0
    assert any("did-not-reproduce" in d for d in _digests(caplog))
    m.restore.assert_called_once()


async def test_gate_only_failure_no_agent_no_attempt_gate_only_digest(tmp_path, caplog):
    with _mocks(tmp_path, failing=["CI Gate", "merge-reports"]) as m, caplog.at_level(logging.INFO):
        await ci_fix.run(_cfg(tmp_path))
    m.backend.run.assert_not_called()
    m.checkout.assert_not_called()
    assert m.pushes == []
    assert ci_fix.get_attempts(SLUG, 1) == 0
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "gate-only" in digests[0]


@pytest.mark.parametrize("name", ["aggregate-coverage", "propagate-labels", "investigate-flaky", "delegate-tests"])
def test_words_containing_gate_are_not_gates(name):
    assert not ci_fix._is_gate(_gate(name))


@pytest.mark.parametrize("name", ["gate", "CI Gate", "ci-gate", "merge-gate", "integration-gate-tests"])
def test_gate_names_are_gates(name):
    assert ci_fix._is_gate(_gate(name))


async def test_non_gate_picked_even_when_gate_listed_first(tmp_path):
    with _mocks(tmp_path, failing=["ci-GATE", "Run checks"]) as m:
        await ci_fix.run(_cfg(tmp_path))
    assert len(m.pushes) == 1
    assert ci_fix.get_attempts(SLUG, 1) == 1
    m.backend.run.assert_called_once()


async def test_gates_never_sent_after_non_gate_did_not_reproduce(tmp_path, caplog):
    with _mocks(tmp_path, failing=["Run checks", "ci-gate"], repro_rc=0) as m, caplog.at_level(logging.INFO):
        await ci_fix.run(_cfg(tmp_path))
    m.backend.run.assert_not_called()
    m.checkout.assert_called_once()  # only the non-gate was tried
    assert ci_fix.get_attempts(SLUG, 1) == 0
    assert not any("gate-only" in d for d in _digests(caplog))


async def test_still_failing_after_fix_no_push_digest(tmp_path, caplog):
    with _mocks(tmp_path, after_rc=1) as m, caplog.at_level(logging.INFO):
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    assert ci_fix.get_attempts(SLUG, 1) == 1  # the agent ran, so the attempt counts
    assert any("still-failing" in d for d in _digests(caplog))


async def test_ac5_dry_run_no_push_no_counter_change(tmp_path):
    with _mocks(tmp_path) as m:
        await ci_fix.run(_cfg(tmp_path), dry_run=True)
    assert m.pushes == []
    m.backend.run.assert_not_called()
    assert ci_fix.get_attempts(SLUG, 1) == 0
    m.checkout.assert_called_once()
    m.restore.assert_called_once()


@pytest.mark.parametrize(
    "pr",
    [_pr(author="someone-else"), _pr(draft=True), _pr(state=ChangeRequestState.CLOSED)],
    ids=["other-author", "draft", "closed"],
)
async def test_ac6_out_of_scope_prs_are_skipped(tmp_path, pr):
    with _mocks(tmp_path, prs=[pr]) as m:
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    assert "Checks.list_checks" not in _ops(m)
    m.checkout.assert_not_called()


async def test_ac6_fork_pr_and_head_equal_to_base_are_skipped(tmp_path):
    with _mocks(tmp_path, prs=[_pr(1, "a"), _pr(2, "main")]) as m:
        _update(m.host, 1, is_cross_repository=True)
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    assert "Checks.list_checks" not in _ops(m)


async def test_ac6_pr_found_closed_on_recheck_is_skipped(tmp_path):
    with _mocks(tmp_path, get_override={"state": ChangeRequestState.CLOSED}) as m:
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    m.checkout.assert_not_called()


async def test_ac7_pr_option_processes_only_that_pr(tmp_path):
    with _mocks(tmp_path, prs=[_pr(number=7, branch="seven")]) as m:
        await ci_fix.run(_cfg(tmp_path), pr=7)
    gets = [c for c in m.host.calls if c.operation == "ChangeRequests.get"]
    assert gets[0].args == (REPO, 7)
    assert "ChangeRequests.list" not in _ops(m)
    assert m.pushes == [["git", "push", "origin", "seven"]]


async def test_all_prs_are_processed_without_pr_option(tmp_path):
    with _mocks(tmp_path, prs=[_pr(1, "a"), _pr(2, "b")]) as m:
        m.run_step.side_effect = [_proc(1), _proc(0), _proc(1), _proc(0)]
        await ci_fix.run(_cfg(tmp_path))
    assert [p[3] for p in m.pushes] == ["a", "b"]


async def test_ac8_push_is_plain_and_to_own_branch(tmp_path):
    with _mocks(tmp_path, prs=[_pr(branch="my-branch")]) as m:
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes
    for cmd in m.pushes:
        assert not any(a.startswith("--force") or a == "-f" or ":" in a for a in cmd)
        assert cmd == ["git", "push", "origin", "my-branch"]


async def test_push_rejected_is_rebase_needed_digest_still_an_attempt(tmp_path, caplog):
    with _mocks(tmp_path, push_rc=1) as m, caplog.at_level(logging.INFO):
        await ci_fix.run(_cfg(tmp_path))
    assert len(m.pushes) == 1
    assert ci_fix.get_attempts(SLUG, 1) == 1  # counted when the agent ran
    assert any("rebase-needed" in d for d in _digests(caplog))


async def test_push_failure_other_than_rejection_raises_and_still_restores(tmp_path, caplog):
    with (
        _mocks(tmp_path, push_rc=128, push_stderr=b"fatal: Authentication failed") as m,
        caplog.at_level(logging.INFO),
        pytest.raises(CiFixError, match="push to feat failed"),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert len(m.pushes) == 1
    assert ci_fix.get_attempts(SLUG, 1) == 1  # recorded before the agent ran, so it still counts
    assert not any("rebase-needed" in d for d in _digests(caplog))
    m.restore.assert_called_once()


@pytest.mark.parametrize("green", [False, True], ids=["failing-ci", "green-ci"])
async def test_conflicting_pr_is_detected_and_hits_the_attempt_gate_not_rebase_needed(tmp_path, caplog, green):
    ci_fix._record_attempt(SLUG, 1)
    with (
        _mocks(tmp_path, green=green, get_override={"mergeable": MergeableState.CONFLICTING}) as m,
        caplog.at_level(logging.INFO),
    ):
        await ci_fix.run(_cfg(tmp_path, max_attempts=1))
    assert "ChangeRequests.get" in _ops(m)
    assert m.pushes == []
    m.checkout.assert_not_called()
    m.backend.run.assert_not_called()
    digests = _digests(caplog)
    assert not any("rebase-needed" in d for d in digests)
    assert len(digests) == 1
    assert "cap-reached" in digests[0]
    assert "attempts=1 max=1" in digests[0]


async def test_conflicting_green_pr_is_examined_and_its_counter_is_not_reset(tmp_path):
    ci_fix._record_attempt(SLUG, 1)
    with _mocks(tmp_path, green=True, get_override={"mergeable": MergeableState.CONFLICTING}) as m:
        await ci_fix.run(_cfg(tmp_path))
    assert "ChangeRequests.get" in _ops(m)
    assert ci_fix.get_attempts(SLUG, 1) == 1  # the clean rebase is not an attempt either
    assert len(m.pushes) == 1


async def test_non_conflicting_green_pr_resets_its_counter_with_no_git_call(tmp_path, caplog):
    ci_fix._record_attempt(SLUG, 1)
    with _mocks(tmp_path, green=True) as m, caplog.at_level(logging.INFO):
        await ci_fix.run(_cfg(tmp_path))
    assert "ChangeRequests.get" in _ops(m)
    assert ci_fix.get_attempts(SLUG, 1) == 0
    assert any("CI is green, nothing to fix" in r.getMessage() for r in caplog.records)
    assert m.cmds == []
    m.checkout.assert_not_called()
    assert _digests(caplog) == []


@pytest.mark.parametrize("green", [True, False], ids=["green-ci", "failing-ci"])
async def test_unknown_mergeable_has_no_rebase_path_call_or_digest(tmp_path, caplog, green):
    ci_fix._record_attempt(SLUG, 1)
    with (
        _mocks(tmp_path, green=green, get_override={"mergeable": MergeableState.UNKNOWN}) as m,
        caplog.at_level(logging.INFO),
    ):
        m.run_step.side_effect = [_proc(0)]  # failing CI: check-fix runs as before and does not reproduce
        await ci_fix.run(_cfg(tmp_path))
    assert not any("rebase" in d or "cap-reached" in d for d in _digests(caplog))
    assert not any(c[:2] == ["git", "rebase"] for c in m.cmds)
    assert m.pushes == []
    m.backend.run.assert_not_called()
    assert ci_fix.get_attempts(SLUG, 1) == (0 if green else 1)  # green resets; failing is untouched


@pytest.mark.parametrize("green", [False, True], ids=["failing-ci", "green-ci"])
async def test_conflicting_dry_run_logs_and_does_nothing_else(tmp_path, caplog, green):
    ci_fix._record_attempt(SLUG, 1)
    with (
        _mocks(tmp_path, green=green, get_override={"mergeable": MergeableState.CONFLICTING}) as m,
        caplog.at_level(logging.INFO),
    ):
        await ci_fix.run(_cfg(tmp_path, max_attempts=1), dry_run=True)
    assert any("PR #1" in r.getMessage() and "rebase would run" in r.getMessage() for r in caplog.records)
    assert _digests(caplog) == []
    m.checkout.assert_not_called()
    assert m.cmds == []
    m.run_step.assert_not_called()
    m.backend.run.assert_not_called()
    assert m.pushes == []
    assert ci_fix.get_attempts(SLUG, 1) == 1


def _rebase_cmds(m):
    return [c for c in m.cmds if c[:2] == ["git", "rebase"]]


async def test_conflicting_green_pr_is_rebased_onto_the_base_and_not_counted(tmp_path, caplog):
    with (
        _mocks(tmp_path, green=True, get_override={"mergeable": MergeableState.CONFLICTING}) as m,
        caplog.at_level(logging.INFO),
    ):
        await ci_fix.run(_cfg(tmp_path))
    m.checkout.assert_called_once_with("feat", ANY, ANY)
    assert _rebase_cmds(m) == [["git", "rebase", "origin/main"]]
    assert [d for d in _digests(caplog) if "rebased" not in d] == []
    assert m.pushes == [["git", "push", "--force-with-lease=feat:sha0", "origin", "feat"]]
    m.backend.run.assert_not_called()
    m.run_step.assert_not_called()
    assert ci_fix.get_attempts(SLUG, 1) == 0
    m.restore.assert_called_once()


async def test_conflicting_pr_with_failing_ci_verifies_the_step_then_publishes(tmp_path, caplog):
    with (
        _mocks(tmp_path, get_override={"mergeable": MergeableState.CONFLICTING}) as m,
        caplog.at_level(logging.INFO),
    ):
        m.run_step.side_effect = [_proc(0)]
        await ci_fix.run(_cfg(tmp_path))
    assert _rebase_cmds(m) == [["git", "rebase", "origin/main"]]
    m.run_step.assert_called_once()
    assert m.run_step.call_args.args[0].command == "make check"
    assert len(m.pushes) == 1
    assert "--force-with-lease=feat:sha0" in m.pushes[0]
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "rebased" in digests[0]
    assert "still-failing" not in digests[0]
    assert ci_fix.get_attempts(SLUG, 1) == 0  # a clean rebase is not an attempt


async def test_verified_rebase_commits_tracked_leftovers_before_the_push(tmp_path):
    with _mocks(tmp_path, get_override=_CONFLICTING) as m:
        m.run_step.side_effect = [_proc(0)]
        m.status_box[0] = b" M a.py\n"
        await ci_fix.run(_cfg(tmp_path))
    commit = m.cmds.index(["git", "commit", "-m", "ci-fix: rebase onto main"])
    assert m.cmds.index(["git", "add", "-u"]) < commit < m.cmds.index(m.pushes[0])


async def test_rebase_verification_failure_is_still_failing_with_abort_and_no_push(tmp_path, caplog):
    # T4: the step command exits nonzero.
    with (
        _mocks(tmp_path, get_override=_CONFLICTING) as m,
        caplog.at_level(logging.INFO),
    ):
        m.run_step.side_effect = [_proc(3, b"boom")]
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    assert ["git", "rebase", "--abort"] in m.cmds
    assert not any(c[:2] == ["git", "update-ref"] for c in m.cmds)
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "rebase-still-failing" in digests[0]
    assert "check='Run checks'" in digests[0]
    assert "step 'Run checks' still fails (exit 3)" in digests[0]
    m.restore.assert_called_once()
    m.backend.run.assert_not_called()


async def test_resolved_conflict_with_failing_step_is_aborted_and_not_pushed(tmp_path, caplog):
    with (
        _mocks(tmp_path, get_override=_CONFLICTING) as m,
        _patched_run_cmd(m, stops=1),
        caplog.at_level(logging.INFO),
    ):
        m.run_step.side_effect = [_proc(1)]
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    assert ["git", "rebase", "--abort"] in m.cmds
    assert len([d for d in _digests(caplog) if "rebase-still-failing" in d]) == 1
    assert ci_fix.get_attempts(SLUG, 1) == 1


async def test_rebase_with_no_runnable_command_is_a_parse_failure_with_abort(tmp_path, caplog):
    with (
        _mocks(tmp_path, get_override=_CONFLICTING) as m,
        patch(P + "find_step_command", return_value=None),
        caplog.at_level(logging.INFO),
    ):
        await ci_fix.run(_cfg(tmp_path))
    m.run_step.assert_not_called()
    assert m.pushes == []
    assert ["git", "rebase", "--abort"] in m.cmds
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "parse-failure" in digests[0]
    assert "no runnable command for step 'Run checks' in workflows" in digests[0]
    assert "check='Run checks'" in digests[0]


async def test_rebase_with_unreadable_failing_log_is_a_parse_failure_with_abort(tmp_path, caplog):
    with (
        _mocks(tmp_path, get_override=_CONFLICTING, log="") as m,
        caplog.at_level(logging.INFO),
    ):
        await ci_fix.run(_cfg(tmp_path))
    m.run_step.assert_not_called()
    assert m.pushes == []
    assert ["git", "rebase", "--abort"] in m.cmds
    assert any("parse-failure" in d and "could not read the failing job log or its step" in d for d in _digests(caplog))


async def test_rebase_parse_failure_names_the_first_failing_check(tmp_path, caplog):
    with (
        _mocks(tmp_path, get_override=_CONFLICTING, failing=["alpha", "beta"]) as m,
        patch(P + "find_step_command", return_value=None),
        caplog.at_level(logging.INFO),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "parse-failure" in digests[0]
    assert "check='alpha'" in digests[0]


async def test_rebase_verification_uses_the_second_candidate_when_the_first_has_no_command(tmp_path, caplog):
    step = _Step("Second step", "make second", "")
    with (
        _mocks(tmp_path, get_override=_CONFLICTING, failing=["alpha", "beta"]) as m,
        patch(P + "find_step_command", side_effect=[None, step]) as find,
        caplog.at_level(logging.INFO),
    ):
        m.run_step.side_effect = [_proc(0)]
        await ci_fix.run(_cfg(tmp_path))
    assert find.call_count == 2
    m.run_step.assert_called_once()
    assert m.run_step.call_args.args[0] is step
    assert len(m.pushes) == 1
    assert ["git", "rebase", "--abort"] not in m.cmds
    assert not any("parse-failure" in d for d in _digests(caplog))


async def test_rebase_verification_stops_at_the_first_runnable_candidate(tmp_path, caplog):
    with (
        _mocks(tmp_path, get_override=_CONFLICTING, failing=["alpha", "beta"]) as m,
        caplog.at_level(logging.INFO),
    ):
        m.run_step.side_effect = [_proc(1)]
        await ci_fix.run(_cfg(tmp_path))
    m.run_step.assert_called_once()
    assert m.pushes == []
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "rebase-still-failing" in digests[0]
    assert "check='alpha'" in digests[0]


async def test_conflicting_pr_with_only_gate_checks_failing_is_treated_as_green(tmp_path, caplog):
    with (
        _mocks(tmp_path, failing=["merge gate"], get_override={"mergeable": MergeableState.CONFLICTING}) as m,
        caplog.at_level(logging.INFO),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert _rebase_cmds(m) == [["git", "rebase", "origin/main"]]
    assert any("only gate checks are failing" in r.getMessage() for r in caplog.records)
    assert [d for d in _digests(caplog) if "rebased" not in d] == []
    assert len(m.pushes) == 1
    m.run_step.assert_not_called()


async def test_clean_rebase_commits_tracked_leftovers_with_a_rebase_message(tmp_path):
    with _mocks(tmp_path, green=True, get_override={"mergeable": MergeableState.CONFLICTING}) as m:
        m.status_box[0] = b" M a.py\n"
        await ci_fix.run(_cfg(tmp_path))
    assert ["git", "commit", "-m", "ci-fix: rebase onto main"] in m.cmds
    assert ["git", "add", "-u"] in m.cmds


async def test_stopped_rebase_is_aborted_with_no_push(tmp_path, caplog):
    with (
        _mocks(tmp_path, green=True, get_override={"mergeable": MergeableState.CONFLICTING}) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, rebase_rc=1),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert ["git", "rebase", "--abort"] in m.cmds
    assert m.pushes == []
    assert any("rebase-conflict-unresolved" in d for d in _digests(caplog))
    assert ci_fix.get_attempts(SLUG, 1) == 0
    m.restore.assert_called_once()


@contextlib.contextmanager
def _patched_run_cmd(  # noqa: C901
    m, *, rebase_rc=0, diff_out=b"", abort_rc=0, stops=0, stuck=False, grep=None, merges=b""
):
    """Wrap the mocked ``run_cmd`` so ``git rebase``/``--abort``/``diff`` answer as given.

    ``stops`` simulates a rebase that stops on that many commits (REBASE_HEAD ``stop-1``, ``stop-2``, ...), each
    ``git rebase --continue`` advancing to the next; ``stuck`` makes ``--continue`` fail on the same commit.
    ``grep`` is the ``(returncode, stdout)`` the conflict-marker ``git grep`` answers (default: no match).
    """
    inner = ci_fix.run_cmd
    left = [stops]

    def stop_sha():
        return f"stop-{stops - left[0] + 1}".encode()

    async def fake(cmd, cwd, env, timeout, check=True, shell=False):
        if cmd[:3] == ["git", "rev-list", "--merges"]:
            m.cmds.append(cmd)
            return _proc(0, merges)
        if cmd[:3] == ["git", "rebase", "--abort"]:
            m.cmds.append(cmd)
            return _proc(abort_rc, b"", b"fatal: no rebase in progress?" if abort_rc else b"")
        if cmd[:3] == ["git", "rebase", "--continue"]:
            m.cmds.append(cmd)
            m.continue_envs.append(env)
            if stuck:
                return _proc(1)
            left[0] -= 1
            return _proc(1 if left[0] > 0 else 0)
        if cmd[:2] == ["git", "rebase"]:
            m.cmds.append(cmd)
            return _proc(1 if stops else rebase_rc)
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return _proc(0, stop_sha()) if stops and left[0] > 0 else _proc(1)
        if cmd[:2] == ["git", "grep"] and grep is not None:
            m.cmds.append(cmd)
            return _proc(*grep)
        if cmd[:2] == ["git", "diff"] and "--diff-filter=U" in cmd:
            return _proc(0, b"a.py\0b.py\0")
        if cmd[:2] == ["git", "diff"]:
            m.cmds.append(cmd)
            return _proc(0, diff_out)
        return await inner(cmd, cwd=cwd, env=env, timeout=timeout, check=check, shell=shell)

    with _patch_run_cmd(side_effect=fake):
        yield


_CONFLICTING = {"mergeable": MergeableState.CONFLICTING}


async def test_conflict_resolved_by_backend_then_continue_and_one_attempt_for_two_stopped_commits(tmp_path, caplog):
    # T1 + T2: two stopped commits = two backend runs, two --continue calls, one attempt, one lease push.
    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, stops=2),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert m.backend.run.await_count == 2
    assert [c for c in m.cmds if c[:3] == ["git", "rebase", "--continue"]] == [["git", "rebase", "--continue"]] * 2
    assert all(e["GIT_EDITOR"] == "true" for e in m.continue_envs)
    assert ["git", "rebase", "--abort"] not in m.cmds
    assert ci_fix.get_attempts(SLUG, 1) == 1
    # Vetting runs after the loop completed, against the base.
    cmds = m.cmds
    assert cmds.index(["git", "rebase", "--continue"]) < cmds.index([
        "git",
        "diff",
        "-z",
        "--name-only",
        "--no-renames",
        "origin/main",
    ])
    assert m.pushes == [["git", "push", "--force-with-lease=feat:sha0", "origin", "feat"]]
    assert "stopped_commits=2" in " ".join(_digests(caplog))
    m.restore.assert_called_once()


async def test_backend_gets_the_rebase_prompt_and_the_conflicted_files(tmp_path):
    with _mocks(tmp_path, green=True, get_override=_CONFLICTING) as m, _patched_run_cmd(m, stops=1):
        await ci_fix.run(_cfg(tmp_path))
    (call,) = m.backend.run.await_args_list
    text = call.args[0]
    assert text.startswith("REBASE PROMPT TEXT \u00e9")
    assert "- a.py" in text
    assert "- b.py" in text
    assert "origin/main" in text
    assert "stop-1" in text
    assert call.kwargs["cwd"]


async def test_clean_rebase_runs_no_backend_and_counts_no_attempt(tmp_path):
    # T2: a clean rebase counts none.
    with _mocks(tmp_path, green=True, get_override=_CONFLICTING) as m, _patched_run_cmd(m):
        await ci_fix.run(_cfg(tmp_path))
    m.backend.run.assert_not_called()
    assert ["git", "rebase", "--continue"] not in m.cmds
    assert ci_fix.get_attempts(SLUG, 1) == 0


async def test_conflict_with_failing_ci_is_resolved_verified_and_published(tmp_path):
    with _mocks(tmp_path, get_override=_CONFLICTING) as m, _patched_run_cmd(m, stops=1):
        m.run_step.side_effect = [_proc(0)]
        await ci_fix.run(_cfg(tmp_path))
    assert m.backend.run.await_count == 1
    m.run_step.assert_called_once()
    assert len(m.pushes) == 1
    assert ci_fix.get_attempts(SLUG, 1) == 1


@pytest.mark.parametrize("raises", [RuntimeError("boom"), TimeoutError("slow")], ids=["error", "timeout"])
async def test_backend_failure_in_the_loop_is_one_attempt_abort_and_unresolved(tmp_path, caplog, raises):
    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING, backend_raises=raises) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, stops=2),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert ["git", "rebase", "--abort"] in m.cmds
    assert ["git", "rebase", "--continue"] not in m.cmds
    assert m.pushes == []
    assert ci_fix.get_attempts(SLUG, 1) == 1
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "rebase-conflict-unresolved" in digests[0]


async def test_cancelled_backend_run_is_recorded_and_propagates(tmp_path):
    import asyncio

    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING, backend_raises=asyncio.CancelledError()) as m,
        _patched_run_cmd(m, stops=1),
        pytest.raises(asyncio.CancelledError),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 1
    assert m.pushes == []
    m.restore.assert_called_once()


async def test_continue_that_does_not_advance_gives_up_after_one_attempt(tmp_path, caplog):
    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, stops=2, stuck=True),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert m.backend.run.await_count == 1
    assert ["git", "rebase", "--abort"] in m.cmds
    assert m.pushes == []
    assert ci_fix.get_attempts(SLUG, 1) == 1
    assert any("rebase-conflict-unresolved" in d for d in _digests(caplog))


async def test_unsafe_resolution_is_vetted_after_the_loop_and_not_pushed(tmp_path, caplog):
    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, stops=1, diff_out=b".github/workflows/ci.yml\0", abort_rc=128),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    assert any("rebase-unsafe" in d for d in _digests(caplog))
    assert ci_fix.get_attempts(SLUG, 1) == 1


def _assert_gave_up(m, caplog, outcome="rebase-conflict-unresolved"):
    assert ["git", "rebase", "--abort"] in m.cmds
    assert m.pushes == []
    assert not any(c[:2] == ["git", "update-ref"] for c in m.cmds)
    digests = _digests(caplog)
    assert len([d for d in digests if outcome in d]) == 1
    assert not any("outcome=rebased" in d or "rebased " in d for d in digests)
    m.restore.assert_called_once()


@pytest.mark.parametrize(
    "grep",
    [(0, b"a.py\0"), (0, b""), (2, b"")],
    ids=["markers-found", "markers-empty-output", "grep-error-fails-safe"],
)
async def test_backend_leaving_conflict_markers_aborts_with_no_push(tmp_path, caplog, grep):
    # T-2: markers after a backend run -> abort, no --continue, no push, digest, one attempt.
    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, stops=2, grep=grep, diff_out=b"a.py\0"),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert m.backend.run.await_count == 1
    assert ["git", "rebase", "--continue"] not in m.cmds
    assert ci_fix.get_attempts(SLUG, 1) == 1
    _assert_gave_up(m, caplog)
    if grep[0] == 0 and grep[1]:
        assert any("conflict markers left in: a.py" in d for d in _digests(caplog))


async def test_markers_are_checked_after_each_backend_run(tmp_path, caplog):
    # The first commit is resolved cleanly; the second run leaves markers.
    results = iter([(1, b""), (0, b"b.py\0")])
    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, stops=2, diff_out=b"a.py\0"),
    ):
        inner = ci_fix.run_cmd

        async def fake(cmd, cwd, env, timeout, check=True, shell=False):
            if cmd[:2] == ["git", "grep"]:
                return _proc(*next(results))
            return await inner(cmd, cwd=cwd, env=env, timeout=timeout, check=check, shell=shell)

        with _patch_run_cmd(side_effect=fake):
            await ci_fix.run(_cfg(tmp_path))
    assert m.backend.run.await_count == 2
    assert [c for c in m.cmds if c[:3] == ["git", "rebase", "--continue"]] == [["git", "rebase", "--continue"]]
    assert ci_fix.get_attempts(SLUG, 1) == 1
    _assert_gave_up(m, caplog)


async def test_marker_check_searches_only_changed_tracked_files(tmp_path):
    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING) as m,
        _patched_run_cmd(m, stops=1, diff_out=b"a.py\0", grep=(1, b"")),
    ):
        await ci_fix.run(_cfg(tmp_path))
    (grep,) = [c for c in m.cmds if c[:2] == ["git", "grep"]]
    assert grep[-2:] == ["--", ":(literal)a.py"]
    assert "-I" in grep
    assert ci_fix._CONFLICT_MARKER_RE in grep


async def test_no_changed_files_means_no_marker_search(tmp_path):
    with _mocks(tmp_path, green=True, get_override=_CONFLICTING) as m, _patched_run_cmd(m, stops=1, grep=(0, b"x\0")):
        await ci_fix.run(_cfg(tmp_path))
    # diff_out is empty, so there is nothing to search and the rebase continues to a push.
    assert not any(c[:2] == ["git", "grep"] for c in m.cmds)
    assert len(m.pushes) == 1


@pytest.mark.parametrize(
    "line,expected",
    [
        (b"<<<<<<< HEAD\n", True),
        (b"<<<<<<<\n", True),
        (b"=======\n", False),  # Setext heading underline, not a marker on its own
        (b"Changes\n=======\n", False),
        (b"<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> b\n", True),
        (b">>>>>>> abc123 (msg)\n", True),
        (b"=========\n", False),
        (b"======= banner\n", False),
        (b"<<<<<<<<< heredoc\n", False),
        (b"x = '<<<<<<< HEAD'\n", False),
    ],
)
def test_conflict_marker_pattern_against_real_git_grep(tmp_path, line, expected):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "f.txt").write_bytes(b"ok\n" + line)
    for args in (["init", "-q"], ["add", "f.txt"]):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)  # noqa: S607
    result = subprocess.run(
        ["git", "grep", "-l", "-z", "-I", "-E", "-e", ci_fix._CONFLICT_MARKER_RE, "--", ":(literal)f.txt"],  # noqa: S607
        cwd=repo,
        capture_output=True,
    )
    assert (result.returncode == 0) is expected


async def test_continue_failure_for_a_non_conflict_reason_gives_up_cleanly(tmp_path, caplog):
    # D-3: --continue fails and no next conflict is pending (REBASE_HEAD gone).
    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, stops=1),
    ):
        inner = ci_fix.run_cmd
        continued = []

        async def fake(cmd, cwd, env, timeout, check=True, shell=False):
            if cmd[:3] == ["git", "rebase", "--continue"]:
                m.cmds.append(cmd)
                continued.append(1)
                return _proc(1, b"", b"error: could not apply\nfatal: hook failed")
            if cmd[:3] == ["git", "rev-parse", "--verify"] and continued:
                return _proc(1)
            return await inner(cmd, cwd=cwd, env=env, timeout=timeout, check=check, shell=shell)

        with _patch_run_cmd(side_effect=fake):
            await ci_fix.run(_cfg(tmp_path))
    _assert_gave_up(m, caplog)
    assert ci_fix.get_attempts(SLUG, 1) == 1
    assert any("hook failed" in d for d in _digests(caplog))


async def test_abort_is_attempted_and_tolerated_when_no_rebase_is_in_progress(tmp_path, caplog):
    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING, backend_raises=RuntimeError("boom")) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, stops=1, abort_rc=128),
    ):
        await ci_fix.run(_cfg(tmp_path))
    _assert_gave_up(m, caplog)


async def test_abort_raising_still_writes_the_digest_and_pushes_nothing(tmp_path, caplog):
    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING, backend_raises=TimeoutError("slow")) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, stops=1),
    ):
        inner = ci_fix.run_cmd

        async def fake(cmd, cwd, env, timeout, check=True, shell=False):
            if cmd[:3] == ["git", "rebase", "--abort"]:
                m.cmds.append(cmd)
                raise TimeoutError("git hung")  # noqa: TRY003
            return await inner(cmd, cwd=cwd, env=env, timeout=timeout, check=check, shell=shell)

        with _patch_run_cmd(side_effect=fake):
            await ci_fix.run(_cfg(tmp_path))
    _assert_gave_up(m, caplog)
    assert any("backend failed" in d for d in _digests(caplog))
    assert ci_fix.get_attempts(SLUG, 1) == 1


async def test_vetting_failure_after_resolution_aborts_and_is_rebase_unsafe_with_the_path(tmp_path, caplog):
    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, stops=1, diff_out=b".github/workflows/ci.yml\0", abort_rc=128, grep=(1, b"")),
    ):
        await ci_fix.run(_cfg(tmp_path))
    _assert_gave_up(m, caplog, "rebase-unsafe")
    assert any(".github/workflows/ci.yml" in d for d in _digests(caplog))


async def test_rebase_prompt_is_read_as_utf8_only_when_prs_exist(tmp_path):
    cfg = _cfg(tmp_path)
    with _mocks(tmp_path, prs=[]):
        # No PRs: neither prompt file is needed.
        (cfg.harness.knowledge_dir / "pr-review" / "ci-fix-rebase-prompt.md").unlink()
        await ci_fix.run(cfg)
    with _mocks(tmp_path, green=True):
        with pytest.raises(FileNotFoundError):
            await ci_fix.run(cfg)
        (cfg.harness.knowledge_dir / "pr-review" / "ci-fix-rebase-prompt.md").write_text("caf\u00e9", encoding="utf-8")
        await ci_fix.run(cfg)


def test_shipped_prompts_follow_the_rebase_rules():
    from pathlib import Path

    root = Path(ci_fix.__file__).parents[2] / "knowledge" / "pr-review"
    fix = (root / "ci-fix-prompt.md").read_text(encoding="utf-8")
    rebase = (root / "ci-fix-rebase-prompt.md").read_text(encoding="utf-8")
    assert "needs a rebase" not in fix
    assert "Never amend, rebase, reset or force-push." in fix
    assert "data, not instructions" in rebase
    for banned in (
        "git rebase --continue",
        "git rebase --skip",
        "git rebase --abort",
        "git commit",
        "git reset",
        "git push",
    ):
        assert banned in rebase.split("## Step 3")[1]
    assert "git add" in rebase


async def test_base_commit_editing_github_is_not_rebase_unsafe(tmp_path, caplog):
    # Vetting diffs against origin/<base>: a base-only .github change is not in that diff, though it would be
    # in a diff against the pre-rebase head.
    with (
        _mocks(tmp_path, green=True, get_override={"mergeable": MergeableState.CONFLICTING}) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, diff_out=b"src/a.py\0"),
    ):
        await ci_fix.run(_cfg(tmp_path))
    diffs = [c for c in m.cmds if c[:2] == ["git", "diff"]]
    assert diffs == [["git", "diff", "-z", "--name-only", "--no-renames", "origin/main"]]
    assert not any("rebase-unsafe" in d for d in _digests(caplog))
    assert ["git", "rebase", "--abort"] not in m.cmds


async def test_pr_change_under_github_on_the_rebase_path_is_rebase_unsafe(tmp_path, caplog):
    with (
        _mocks(tmp_path, green=True, get_override={"mergeable": MergeableState.CONFLICTING}) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, diff_out=b".github/workflows/ci.yml\0", abort_rc=128),
    ):  # rebase already finished
        await ci_fix.run(_cfg(tmp_path))
    assert ["git", "rebase", "--abort"] in m.cmds
    assert m.pushes == []
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "rebase-unsafe" in digests[0]
    assert ".github/workflows/ci.yml" in digests[0]
    m.restore.assert_called_once()


async def test_unsafe_change_is_measured_against_the_given_base(tmp_path):
    git, sha = _git_repo(tmp_path)
    git("branch", "base")
    git("checkout", "-q", "-b", "feat")
    (tmp_path / "docs" / "a.md").write_text("x\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "pr")
    git("checkout", "-q", "base")
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: other\n", encoding="utf-8")
    git("commit", "-qam", "base touches .github")
    git("checkout", "-q", "feat")
    git("rebase", "base")
    assert await ci_fix._unsafe_change(str(tmp_path), {}, "base") is None
    assert ".github/workflows/ci.yml" in (await ci_fix._unsafe_change(str(tmp_path), {}, sha) or "")


async def test_green_ci_rebase_path_digest_uses_the_no_check_sentinel(tmp_path, caplog):
    ci_fix._record_attempt(SLUG, 1)
    with (
        _mocks(tmp_path, green=True, get_override={"mergeable": MergeableState.CONFLICTING}),
        caplog.at_level(logging.INFO),
    ):
        await ci_fix.run(_cfg(tmp_path, max_attempts=1))
    assert [d for d in _digests(caplog) if "check='-'" in d and "cap-reached" in d]


async def test_clean_rebase_is_one_lease_push_and_a_rebased_digest_with_no_attempt(tmp_path, caplog):
    with (
        _mocks(tmp_path, green=True, get_override={"mergeable": MergeableState.CONFLICTING}) as m,
        caplog.at_level(logging.INFO),
    ):
        await ci_fix.run(_cfg(tmp_path))
    m.backend.run.assert_not_called()
    assert len(m.pushes) == 1
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "rebased" in digests[0]
    assert "refs/harness-recovery/feat-sha0" in digests[0]
    assert "stopped_commits=0" in digests[0]
    assert ci_fix.get_attempts(SLUG, 1) == 0
    m.restore.assert_called_once()


async def test_recovery_ref_is_written_before_the_lease_push_and_kept(tmp_path):
    ref_cmd = ["git", "update-ref", "refs/harness-recovery/feat-sha0", "sha0"]
    with _mocks(tmp_path, green=True, get_override={"mergeable": MergeableState.CONFLICTING}) as m:
        seen: list[bool] = []
        inner = ci_fix.run_cmd

        async def spy(cmd, **kw):
            if cmd[:2] == ["git", "push"]:
                seen.append(ref_cmd in m.cmds)  # recorded by the mock before this push
            return await inner(cmd, **kw)

        with _patch_run_cmd(side_effect=spy):
            await ci_fix.run(_cfg(tmp_path))
    assert seen == [True]
    assert m.cmds.count(ref_cmd) == 1
    assert not any(c[:2] == ["git", "update-ref"] and "-d" in c for c in m.cmds)


async def test_rebase_path_push_is_the_lease_push_and_other_pushes_stay_plain(tmp_path):
    with _mocks(tmp_path, green=True, get_override={"mergeable": MergeableState.CONFLICTING}) as m:
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == [["git", "push", "--force-with-lease=feat:sha0", "origin", "feat"]]
    with _mocks(tmp_path, prs=[_pr(branch="my-branch")]) as m2:
        await ci_fix.run(_cfg(tmp_path))
    assert m2.pushes == [["git", "push", "origin", "my-branch"]]


async def test_stale_lease_rejection_is_rebase_needed_with_no_raise_and_the_ref_kept(tmp_path, caplog):
    stderr = b" ! [rejected]        feat -> feat (stale info)\nerror: failed to push some refs to 'origin'\n"
    with (
        _mocks(
            tmp_path, green=True, push_rc=1, push_stderr=stderr, get_override={"mergeable": MergeableState.CONFLICTING}
        ) as m,
        caplog.at_level(logging.INFO),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert len(m.pushes) == 1
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "rebase-needed" in digests[0]
    assert ["git", "update-ref", "refs/harness-recovery/feat-sha0", "sha0"] in m.cmds
    assert ci_fix.get_attempts(SLUG, 1) == 0


async def test_other_lease_push_failure_still_raises(tmp_path):
    with (
        _mocks(
            tmp_path,
            green=True,
            push_rc=128,
            push_stderr=b"fatal: Authentication failed",
            get_override={"mergeable": MergeableState.CONFLICTING},
        ),
        pytest.raises(CiFixError, match="push to feat failed"),
    ):
        await ci_fix.run(_cfg(tmp_path))


async def test_failed_recovery_ref_write_means_no_push_and_a_parse_failure_digest(tmp_path, caplog):
    with (
        _mocks(tmp_path, green=True, get_override={"mergeable": MergeableState.CONFLICTING}) as m,
        caplog.at_level(logging.INFO),
    ):
        inner = ci_fix.run_cmd

        async def failing(cmd, **kw):
            if cmd[:2] == ["git", "update-ref"]:
                return _proc(128, b"", b"fatal: cannot lock ref")
            return await inner(cmd, **kw)

        with _patch_run_cmd(side_effect=failing):
            await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "parse-failure" in digests[0]
    assert "could not write the recovery ref" in digests[0]
    assert ci_fix.get_attempts(SLUG, 1) == 0
    m.restore.assert_called_once()


def _origin_and_clone(tmp_path):
    import subprocess

    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()  # noqa: S607

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    other = tmp_path / "other"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    for d in (work, other):
        git(tmp_path, "clone", "-q", str(origin), str(d))
        git(d, "config", "user.email", "t@example.com")
        git(d, "config", "user.name", "t")
    git(work, "checkout", "-q", "-b", "feat")
    (work / "a.txt").write_text("1\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "one")
    git(work, "push", "-q", "origin", "feat")
    return git, work, other


async def test_lease_push_against_real_git_succeeds_and_keeps_the_recovery_ref(tmp_path, caplog):
    git, work, _ = _origin_and_clone(tmp_path)
    pre = git(work, "rev-parse", "HEAD")
    git(work, "commit", "-q", "--amend", "-m", "rewritten")  # what a rebase does to the head
    with caplog.at_level(logging.INFO):
        assert await ci_fix._publish_rebase(1, "feat", pre, 0, str(work), {})
    assert git(work, "rev-parse", f"refs/harness-recovery/feat-{pre[:12]}") == pre
    assert git(work, "ls-remote", "origin", "feat").split()[0] == git(work, "rev-parse", "HEAD")
    assert any("rebased pr=1" in d for d in _digests(caplog))


async def test_stale_lease_against_real_git_is_rebase_needed_and_keeps_the_recovery_ref(tmp_path, caplog):
    git, work, other = _origin_and_clone(tmp_path)
    pre = git(work, "rev-parse", "HEAD")
    git(other, "fetch", "-q", "origin")
    git(other, "checkout", "-q", "feat")
    (other / "b.txt").write_text("2\n", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "someone else")
    git(other, "push", "-q", "origin", "feat")
    moved = git(work, "ls-remote", "origin", "feat").split()[0]
    git(work, "commit", "-q", "--amend", "-m", "rewritten")
    with caplog.at_level(logging.INFO):
        assert not await ci_fix._publish_rebase(1, "feat", pre, 0, str(work), {})
    assert any("rebase-needed" in d for d in _digests(caplog))
    assert git(work, "ls-remote", "origin", "feat").split()[0] == moved  # nothing was overwritten
    assert git(work, "rev-parse", f"refs/harness-recovery/feat-{pre[:12]}") == pre


def test_rebase_outcome_names():
    assert ci_fix.OUTCOME_REBASED == "rebased"
    assert ci_fix.OUTCOME_REBASE_CONFLICT_UNRESOLVED == "rebase-conflict-unresolved"
    assert ci_fix.OUTCOME_REBASE_UNSAFE == "rebase-unsafe"
    assert ci_fix.OUTCOME_REBASE_STILL_FAILING == "rebase-still-failing"
    assert ci_fix.OUTCOME_REBASE_NEEDED == "rebase-needed"


async def test_unreadable_check_output_is_parse_failure_digest(tmp_path, caplog):
    with _mocks(tmp_path) as m, caplog.at_level(logging.INFO):
        # A failing check that names neither a job nor a link cannot be followed up.
        m.host._checks[REPO][1][:] = [replace(c, details_url=None, job_id=None) for c in m.host._checks[REPO][1]]
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    m.checkout.assert_not_called()
    assert any("parse-failure" in d for d in _digests(caplog))


async def test_step_is_looked_up_after_checkout_of_the_pr_branch(tmp_path, caplog):
    order: list[str] = []
    with (
        _mocks(tmp_path) as m,
        patch(P + "find_step_command", side_effect=lambda *a: order.append("find") or None),
        caplog.at_level(logging.INFO),
    ):
        m.checkout.side_effect = lambda *a: order.append("checkout")
        await ci_fix.run(_cfg(tmp_path))
    assert order == ["checkout", "find"]
    assert m.pushes == []
    m.restore.assert_called_once()
    assert any("parse-failure" in d for d in _digests(caplog))


async def test_checkout_failure_cleans_working_dir_with_original_sha(tmp_path):
    with _mocks(tmp_path) as m:
        m.checkout.side_effect = FatalGitError("checkout failed")
        with pytest.raises(Exception, match="checkout failed"):
            await ci_fix.run(_cfg(tmp_path))
    m.clean.assert_called_once_with(str(tmp_path), ANY, "orig")
    m.restore.assert_called_once()


def _checkout_fails_on(*branches):
    async def checkout(branch, wdir, env):
        if branch in branches:
            raise FatalGitError(f"checkout of {branch} failed")  # noqa: TRY003

    return checkout


def _three_prs():
    return [_pr(number=n, branch=f"b{n}") for n in (1, 2, 3)]


async def test_identified_git_failure_skips_pr_continues_and_raises(tmp_path, caplog):
    with _mocks(tmp_path, prs=_three_prs()) as m, caplog.at_level(logging.INFO):
        m.checkout.side_effect = _checkout_fails_on("b2")
        m.run_step.side_effect = [_proc(1, b"boom"), _proc(0), _proc(1, b"boom"), _proc(0)]
        with pytest.raises(CiFixError) as exc:
            await ci_fix.run(_cfg(tmp_path))
    assert "#2" in str(exc.value)
    assert "#1" not in str(exc.value)
    assert "#3" not in str(exc.value)
    assert [c.args[0] for c in m.checkout.call_args_list] == ["b1", "b2", "b3"]
    assert m.restore.call_count == 3
    assert m.run_step.call_count == 4  # PR 1 and PR 3 were processed
    assert len(m.pushes) == 2
    messages = [
        r.getMessage() for r in caplog.records if r.levelno == logging.ERROR and "skipped: git" in r.getMessage()
    ]
    assert messages == ["PR #2 skipped: git failure: checkout of b2 failed"]


@pytest.mark.parametrize(("cleaned", "probe"), [(False, False), (True, True), (True, None)])
async def test_unidentified_git_failure_stops_loop(tmp_path, cleaned, probe):
    with _mocks(tmp_path, prs=_three_prs()) as m:
        m.checkout.side_effect = _checkout_fails_on("b2")
        m.clean.return_value = cleaned
        m.dirty.return_value = probe
        with pytest.raises(CiFixError) as exc:
            await ci_fix.run(_cfg(tmp_path))
    assert "#2" in str(exc.value)
    assert [c.args[0] for c in m.checkout.call_args_list] == ["b1", "b2"]


async def test_skip_log_uses_first_stderr_line(tmp_path, caplog):
    err = subprocess.CalledProcessError(1, ["git", "checkout"], stderr=b"fatal: bad object\nsecond line\n")
    failure = FatalGitError("checkout failed")
    failure.__cause__ = err
    with _mocks(tmp_path) as m, caplog.at_level(logging.ERROR):
        m.checkout.side_effect = failure
        with pytest.raises(CiFixError):
            await ci_fix.run(_cfg(tmp_path))
    messages = [r.getMessage() for r in caplog.records if "skipped: git failure" in r.getMessage()]
    assert messages == ["PR #1 skipped: git failure: fatal: bad object"]


async def test_successful_checkout_does_not_clean(tmp_path):
    with _mocks(tmp_path) as m:
        await ci_fix.run(_cfg(tmp_path))
    m.checkout.assert_called_once()
    m.clean.assert_not_called()


async def test_ac9_restore_runs_when_exception_mid_run(tmp_path):
    with (
        _mocks(tmp_path, backend_raises=RuntimeError("agent died")) as m,
        pytest.raises(CiFixError, match="agent died"),
    ):
        await ci_fix.run(_cfg(tmp_path))
    m.restore.assert_called_once()
    assert m.restore.call_args.args[:2] == ("orig", "feat")
    assert m.pushes == []


async def test_one_pr_failing_does_not_stop_the_others(tmp_path):
    with _mocks(tmp_path, prs=[_pr(1, "a"), _pr(2, "b")]) as m:
        m.run_step.side_effect = [_proc(1), RuntimeError("timeout"), _proc(1), _proc(0)]
        m.backend.run = AsyncMock()
        m.backend.run.side_effect = [_proc(0), _proc(0)]
        with pytest.raises(CiFixError, match="#1"):
            await ci_fix.run(_cfg(tmp_path))
    assert m.restore.call_count == 2


async def test_counters_of_closed_prs_are_pruned(tmp_path):
    ci_fix._record_attempt(SLUG, 1)
    ci_fix._record_attempt(SLUG, 99)
    with _mocks(tmp_path, green=False, after_rc=1):
        await ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 2  # open and not green: kept, plus this run's attempt
    assert ci_fix.get_attempts(SLUG, 99) == 0


async def test_counter_reset_when_ci_green(tmp_path):
    ci_fix._record_attempt(SLUG, 1)
    ci_fix._record_attempt(SLUG, 2)
    with _mocks(tmp_path, prs=[_pr(1)], green=True):
        await ci_fix.run(_cfg(tmp_path), pr=1)
    assert ci_fix.get_attempts(SLUG, 1) == 0
    assert ci_fix.get_attempts(SLUG, 2) == 1


async def test_counter_untouched_while_not_green(tmp_path):
    ci_fix._record_attempt(SLUG, 1)
    with _mocks(tmp_path, green=False) as m:
        m.run_step.side_effect = [_proc(0)]  # does not reproduce: no agent run
        await ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 1


async def test_counter_untouched_in_dry_run_when_green(tmp_path):
    ci_fix._record_attempt(SLUG, 1)
    with _mocks(tmp_path, green=True):
        await ci_fix.run(_cfg(tmp_path), dry_run=True)
    assert ci_fix.get_attempts(SLUG, 1) == 1


def test_reset_attempts_is_idempotent_and_writes_nothing_without_key(tmp_path):
    ci_fix._reset_attempts(SLUG, 1)
    with patch("dotharness.pr_review.state.atomic_write_text") as write:
        ci_fix._reset_attempts(SLUG, 1)
    write.assert_not_called()
    ci_fix._record_attempt(SLUG, 1)
    ci_fix._reset_attempts(SLUG, 1)
    assert ci_fix.get_attempts(SLUG, 1) == 0
    with patch("dotharness.pr_review.state.atomic_write_text") as write:
        ci_fix._reset_attempts(SLUG, 1)
    write.assert_not_called()


@pytest.fixture(autouse=True)
def _state_home(tmp_path):
    with patch("dotharness.pr_review.state.XDG_DATA", tmp_path / "xdg"):
        yield


WORKFLOW = """name: Main
defaults:
  run:
    working-directory: src/dotharness/pr_review/runners/pr-review

jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - name: Check out
        uses: actions/checkout@v4

      - name: Run checks
        run: make check

  tests:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: other
    steps:
      - name: Run tests
        run: |
          uv run pytest
          uv run ty check
      - name: Templated
        run: echo ${{ matrix.v }}
      - name: Elsewhere
        working-directory: sub
        run: echo hi
"""


def test_find_step_command_reads_workflow(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "main.yml").write_text(WORKFLOW, encoding="utf-8")
    assert ci_fix.find_step_command(str(tmp_path), "Run checks") == _Step(
        "Run checks", "make check", "src/dotharness/pr_review/runners/pr-review"
    )
    assert ci_fix.find_step_command(str(tmp_path), "Run tests") == _Step(
        "Run tests", "uv run pytest\nuv run ty check", "other"
    )
    assert ci_fix.find_step_command(str(tmp_path), "Elsewhere") == _Step("Elsewhere", "echo hi", "sub")
    assert ci_fix.find_step_command(str(tmp_path), "Templated") is None
    assert ci_fix.find_step_command(str(tmp_path), "Check out") is None
    assert ci_fix.find_step_command(str(tmp_path), "Missing") is None


def test_find_step_command_picks_the_workflow_the_check_belongs_to(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    for file, name, wd in (
        ("a.yml", "Alpha", "src/dotharness/pr_review/runners/a"),
        ("b.yml", "Beta", "src/dotharness/pr_review/runners/b"),
    ):
        (wf / file).write_text(
            f"name: {name}\njobs:\n  t:\n    steps:\n      - name: Run tests\n"
            f"        working-directory: {wd}\n        run: pytest\n",
            encoding="utf-8",
        )
    (wf / "c.yml").write_text(
        "jobs:\n  t:\n    steps:\n      - name: Run tests\n        run: make c\n", encoding="utf-8"
    )
    assert ci_fix.find_step_command(str(tmp_path), "Run tests", "Beta") == _Step(
        "Run tests", "pytest", "src/dotharness/pr_review/runners/b"
    )
    assert ci_fix.find_step_command(str(tmp_path), "Run tests", ".github/workflows/c.yml") == _Step(
        "Run tests", "make c", ""
    )
    assert ci_fix.find_step_command(str(tmp_path), "Run tests", "Nope") is None
    assert ci_fix.find_step_command(str(tmp_path), "Run tests") == _Step(
        "Run tests", "pytest", "src/dotharness/pr_review/runners/a"
    )


def _fresh_host():
    host = InMemoryCodeHost(repository="acme/frontend", user="me")
    host.add_repository("acme/frontend")
    host.add_change_request(REPO, head_reference="feat", author="me", number=1)
    return host


def _put(host, checks, *, required=True):
    """Check runs exactly as given (the seeder cannot set a workflow name or timestamps)."""
    host._checks.setdefault(REPO, {}).setdefault(1, []).extend(checks)
    if required:
        host._required.update((REPO, 1, c.name) for c in checks)
    host._version += 1


def _run_of(name, status, *, job_id="2", workflow="W", started=None, completed=None):
    return Check(name, status, f"https://example.test/runs/1/job/{job_id}", workflow, started, completed, job_id)


async def test_read_failing_checks_carries_the_workflow():
    host = _fresh_host()
    _put(host, [_run_of("t (3.12)", CheckStatus.FAIL, workflow="Beta")])
    failing = await ci_fix._read_failing_checks(host.code_host(), REPO, 1)
    assert failing is not None
    assert [(c.name, c.workflow_name, c.job_id) for c in failing] == [("t (3.12)", "Beta", "2")]


async def test_read_failing_checks_ignores_superseded_failed_run():
    host = _fresh_host()
    _put(
        host,
        [
            _run_of("t", CheckStatus.FAIL, started=T0, completed=T0 + timedelta(minutes=5)),
            _run_of(
                "t", CheckStatus.PASS, job_id="4", started=T0 + timedelta(hours=1), completed=T0 + timedelta(hours=1)
            ),
        ],
    )
    assert (await ci_fix._read_failing_checks(host.code_host(), REPO, 1)) == []


async def test_read_failing_checks_keeps_latest_failed_run_and_fails_closed_without_timestamps():
    host = _fresh_host()
    _put(
        host,
        [
            _run_of("t", CheckStatus.PASS, started=T0, completed=T0 + timedelta(minutes=5)),
            _run_of(
                "t", CheckStatus.FAIL, job_id="4", started=T0 + timedelta(hours=1), completed=T0 + timedelta(hours=1)
            ),
        ],
    )
    failing = await ci_fix._read_failing_checks(host.code_host(), REPO, 1)
    assert failing is not None
    assert [c.job_id for c in failing] == ["4"]
    undated = _fresh_host()
    _put(undated, [_run_of("t", CheckStatus.FAIL), _run_of("t", CheckStatus.PASS, job_id="4")])
    assert (await ci_fix._read_failing_checks(undated.code_host(), REPO, 1)) is None


async def test_read_failing_checks_none_when_the_read_fails():
    host = _fresh_host()
    host.fail_next("Checks.list_checks", CodeHostError("boom"))
    assert (await ci_fix._read_failing_checks(host.code_host(), REPO, 1)) is None


def test_failed_step_name_from_log():
    assert ci_fix._failed_step_name("quality\tRun checks\t2024Z ##[error]x") == "Run checks"
    assert ci_fix._failed_step_name("no tabs here") is None


def test_failed_step_name_ignores_unknown_step_placeholder():
    assert ci_fix._failed_step_name("quality\tUNKNOWN STEP\t2024Z error") is None
    log = "quality\tUNKNOWN STEP\t2024Z a\nquality\tRun checks\t2024Z b"
    assert ci_fix._failed_step_name(log) == "Run checks"


def _failing_check(host, **seed):
    _seed_checks(host, 1, ["t"], **seed)
    return host._checks[REPO][1][0]


async def test_read_failed_log_returns_the_job_log():
    host = _fresh_host()
    check = _failing_check(host, log="the log")
    assert await ci_fix._read_failed_log(host.code_host(), REPO, check) == "the log"
    assert [c.operation for c in host.calls] == ["Checks.failed_log"]


async def test_read_failed_log_failure_is_a_ci_fix_error_from_the_host_error():
    host = _fresh_host()
    check = _failing_check(host)
    host.fail_next("Checks.failed_log", CodeHostError("no log"))
    with pytest.raises(CiFixError, match="no log") as exc:
        await ci_fix._read_failed_log(host.code_host(), REPO, check)
    assert isinstance(exc.value.__cause__, CodeHostError)


async def test_read_failed_log_timeout_is_a_ci_fix_error():
    host = _fresh_host()
    check = _failing_check(host)
    host.fail_next("Checks.failed_log", CodeHostTimeoutError("slow", write=False))
    with pytest.raises(CiFixError, match="slow"):
        await ci_fix._read_failed_log(host.code_host(), REPO, check)


async def test_read_failed_log_unsupported_stops_unwrapped():
    host = _fresh_host()
    check = _failing_check(host)
    host.fail_next("Checks.failed_log", UnsupportedCapabilityError("no", capability="Checks"))
    with pytest.raises(UnsupportedCapabilityError):
        await ci_fix._read_failed_log(host.code_host(), REPO, check)


async def test_failed_step_from_api_picks_the_failed_step():
    host = _fresh_host()
    check = _failing_check(host, failed_step="Run checks")
    assert await ci_fix._failed_step_from_api(host.code_host(), REPO, check) == "Run checks"
    assert host.calls[-1].operation == "Checks.get_job"
    assert host.calls[-1].args == (REPO, check.job_id)


async def test_failed_step_from_api_none_without_a_job_or_a_failed_step():
    host = _fresh_host()
    no_job = Check("t", CheckStatus.FAIL, "https://example.test/runs/9", None, None, None, None)
    assert await ci_fix._failed_step_from_api(host.code_host(), REPO, no_job) is None
    assert host.calls == []
    no_step = _failing_check(host, failed_step=None)
    assert await ci_fix._failed_step_from_api(host.code_host(), REPO, no_step) is None


async def test_failed_step_from_api_failure_is_a_ci_fix_error():
    host = _fresh_host()
    check = _failing_check(host)
    host.fail_next("Checks.get_job", CodeHostError("job gone"))
    with pytest.raises(CiFixError, match="job gone") as exc:
        await ci_fix._failed_step_from_api(host.code_host(), REPO, check)
    assert isinstance(exc.value.__cause__, CodeHostError)


async def test_failed_step_from_api_unsupported_stops_unwrapped():
    host = _fresh_host()
    check = _failing_check(host)
    host.fail_next("Checks.get_job", UnsupportedCapabilityError("no", capability="Checks"))
    with pytest.raises(UnsupportedCapabilityError):
        await ci_fix._failed_step_from_api(host.code_host(), REPO, check)


async def test_fix_check_uses_api_step_when_log_says_unknown_step(tmp_path):
    with (
        _mocks(tmp_path, log="quality\tUNKNOWN STEP\t2024Z error", failed_step="Run checks") as m,
        patch(P + "find_step_command", return_value=_Step("Run checks", "make check", "")) as find,
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert "Checks.get_job" in _ops(m)
    assert find.call_args.args[1] == "Run checks"
    assert m.pushes


async def test_log_read_failure_is_a_parse_failure_digest_and_continues(tmp_path, caplog):
    with _mocks(tmp_path, prs=[_pr(1, "a"), _pr(2, "b")]) as m:
        m.run_step.side_effect = [_proc(1), _proc(0)]
        m.host.fail_next("Checks.failed_log", CodeHostError("no log"))
        await ci_fix.run(_cfg(tmp_path))  # no CiFixError: the failure is a per-check digest
    assert "parse-failure" in caplog.text
    assert "no log" in caplog.text
    assert [p[3] for p in m.pushes] == ["b"]
    m.checkout.assert_called_once()  # PR 1 never reached the checkout


async def test_unsupported_log_read_stops_the_run(tmp_path):
    with _mocks(tmp_path, prs=[_pr(1, "a"), _pr(2, "b")]) as m:
        m.host.fail_next("Checks.failed_log", UnsupportedCapabilityError("no", capability="Checks"))
        with pytest.raises(UnsupportedCapabilityError):
            await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    assert _ops(m).count("Checks.failed_log") == 1


async def test_unsupported_checks_capability_stops_before_any_checkout(tmp_path):
    with _mocks(tmp_path) as m:
        m.host.fail_next("Checks.list_checks", UnsupportedCapabilityError("no", capability="Checks"))
        with pytest.raises(UnsupportedCapabilityError):
            await ci_fix.run(_cfg(tmp_path))
    m.checkout.assert_not_called()
    assert m.pushes == []


async def test_list_failure_is_collected_and_raised(tmp_path):
    with _mocks(tmp_path) as m:
        m.host.fail_next("ChangeRequests.list", CodeHostError("listing broke"))
        with pytest.raises(CiFixError, match="listing broke"):
            await ci_fix.run(_cfg(tmp_path))
    m.checkout.assert_not_called()


async def test_agent_gets_the_credential_through_the_host_environment(tmp_path):
    with _mocks(tmp_path) as m:
        await ci_fix.run(_cfg(tmp_path))
    env = m.backend_cls.call_args.args[3]
    assert env["GITHUB_TOKEN"] == SECRET
    assert "Session.agent_environment" in _ops(m)


async def test_no_write_reaches_the_host(tmp_path):
    with _mocks(tmp_path) as m:
        await ci_fix.run(_cfg(tmp_path))
    assert m.host.writes == []


def _git_repo(tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)  # noqa: S607

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: ci\n" * 20, encoding="utf-8")
    (tmp_path / "docs").mkdir()
    git("add", "-A")
    git("commit", "-qm", "init")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return git, sha


async def test_unsafe_change_flags_workflow_moved_out_of_github(tmp_path):
    git, sha = _git_repo(tmp_path)
    git("mv", ".github/workflows/ci.yml", "docs/ci.yml")
    result = await ci_fix._unsafe_change(str(tmp_path), {}, sha)
    assert result is not None
    assert ".github/workflows/ci.yml" in result


async def test_unsafe_change_flags_non_ascii_workflow_path(tmp_path):
    git, sha = _git_repo(tmp_path)
    (tmp_path / ".github" / "workflows" / "\u00e9.yml").write_text("x\n", encoding="utf-8")
    git("add", "-A")
    result = await ci_fix._unsafe_change(str(tmp_path), {}, sha)
    assert result is not None
    assert ".github/workflows/\u00e9.yml" in result


async def test_unsafe_change_reports_unquoted_untracked_non_ascii_path(tmp_path):
    _, sha = _git_repo(tmp_path)
    (tmp_path / "docs" / "\u00e9 file.txt").write_text("x\n", encoding="utf-8")
    result = await ci_fix._unsafe_change(str(tmp_path), {}, sha)
    assert result is not None
    assert "docs/\u00e9 file.txt" in result


async def test_unsafe_change_allows_ordinary_tracked_edit(tmp_path):
    _, sha = _git_repo(tmp_path)
    (tmp_path / "docs" / "a.md").write_text("x\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)  # noqa: S607
    assert (await ci_fix._unsafe_change(str(tmp_path), {}, sha)) is None


async def test_unsafe_change_runs_before_the_post_fix_step(tmp_path, caplog):
    with (
        _mocks(tmp_path) as m,
        patch(P + "_unsafe_change", return_value="change touches .github/: x"),
        caplog.at_level(logging.INFO),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert m.run_step.call_count == 1  # only the reproduction run; the agent's tree was never executed
    assert m.pushes == []
    assert any("unsafe-change" in d for d in _digests(caplog))


async def test_branch_with_a_merge_commit_is_not_rebased(tmp_path, caplog):
    with (
        _mocks(tmp_path, green=True, get_override=_CONFLICTING) as m,
        caplog.at_level(logging.INFO),
        _patched_run_cmd(m, merges=b"abc123\n"),
    ):
        await ci_fix.run(_cfg(tmp_path))
    assert ["git", "rev-list", "--merges", "origin/main..HEAD"] in m.cmds
    assert _rebase_cmds(m) == []
    assert m.pushes == []
    m.backend.run.assert_not_called()
    assert len([d for d in _digests(caplog) if "rebase-has-merges" in d]) == 1
