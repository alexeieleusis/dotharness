import contextlib
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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

P = "dotharness.pr_review.runners.ci_fix."
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
def _mocks(
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
        patch(P + "_run_step", side_effect=step_results) as run_step,
        patch(P + "run_cmd", side_effect=fake_run_cmd),
        patch(P + "Backend", return_value=backend) as backend_cls,
    ):
        yield SimpleNamespace(
            host=host,
            checkout=checkout,
            restore=restore,
            run_step=run_step,
            pushes=pushes,
            cmds=cmds,
            backend=backend,
            status_box=status_box,
            backend_cls=backend_cls,
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


async def test_conflicting_pr_is_rebase_needed_digest(tmp_path, caplog):
    with _mocks(tmp_path, get_override={"mergeable": MergeableState.CONFLICTING}) as m, caplog.at_level(logging.INFO):
        await ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    m.checkout.assert_not_called()
    assert any("rebase-needed" in d for d in _digests(caplog))


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
