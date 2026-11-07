import contextlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from dotharness.pr_review.config import CiFixConfig, HarnessConfig, HarnessSection, RepoConfig
from dotharness.pr_review.runners import ci_fix
from dotharness.pr_review.runners.auto_approve_github import SafetyResult, SafetyStatus
from dotharness.pr_review.runners.ci_fix import DIGEST_PREFIX, CiFixError, _Check, _Step

P = "dotharness.pr_review.runners.ci_fix."
SLUG = "acme-frontend"


def _pr(number=1, branch="feat", author="me", draft=False, state="OPEN"):
    return {
        "number": number,
        "headRefName": branch,
        "baseRefName": "main",
        "isDraft": draft,
        "isCrossRepository": False,
        "state": state,
        "author": {"login": author},
    }


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


@contextlib.contextmanager
def _mocks(
    tmp_path,
    prs=None,
    green=False,
    repro_rc=1,
    after_rc=0,
    push_rc=0,
    push_stderr=b"! [rejected] (non-fast-forward)",
    pr_state=None,
    backend_raises=None,
    status=b"",
    agent_commits=True,
    failing=None,
):
    prs = [_pr()] if prs is None else prs
    head = {"sha": "sha0"}
    status_box = [status]
    pushes: list[list[str]] = []
    cmds: list[list[str]] = []
    step_results = [_proc(repro_rc, b"boom"), _proc(after_rc)]

    def fake_gh(args, env):
        data = prs if args[1] == "list" else prs[0]
        return _proc(0, json.dumps(data).encode())

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
    backend.run.side_effect = backend_run
    safety = None if green else SafetyResult(SafetyStatus.SKIP, "ci-blocked")
    state = pr_state or {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "headRefOid": "sha0"}
    with (
        patch("dotharness.pr_review.state.XDG_DATA", tmp_path / "xdg"),
        patch(P + "acquire_lock", return_value=contextlib.nullcontext()),
        patch(P + "get_gh_token", return_value="tok"),
        patch(P + "build_subprocess_env", return_value={"GITHUB_TOKEN": "tok"}),
        patch(P + "get_current_user", return_value="me"),
        patch(P + "_gh", side_effect=fake_gh) as gh,
        patch(P + "_check_ci", return_value=safety) as check_ci,
        patch(P + "_read_pr_state", return_value=state),
        patch(
            P + "_read_failing_checks",
            return_value=failing or [_Check("Run checks", "https://x/actions/runs/9/job/8")],
        ),
        patch(P + "_read_failed_log", return_value="quality\tRun checks\t2024Z error"),
        patch(P + "find_step_command", return_value=_Step("Run checks", "make check", "tools/pr-review")),
        patch(P + "git_detach_and_record", return_value="orig"),
        patch(P + "git_fetch_and_checkout") as checkout,
        patch(P + "git_restore") as restore,
        patch(P + "_run_step", side_effect=step_results) as run_step,
        patch(P + "run_cmd", side_effect=fake_run_cmd),
        patch(P + "Backend", return_value=backend) as backend_cls,
    ):
        yield SimpleNamespace(
            gh=gh,
            check_ci=check_ci,
            checkout=checkout,
            restore=restore,
            run_step=run_step,
            pushes=pushes,
            cmds=cmds,
            backend=backend,
            status_box=status_box,
            backend_cls=backend_cls,
        )


def _digests(caplog):
    return [r.getMessage() for r in caplog.records if DIGEST_PREFIX in r.getMessage()]


def test_disabled_is_noop(tmp_path):
    with _mocks(tmp_path) as m, patch(P + "get_gh_token") as tok:
        ci_fix.run(_cfg(tmp_path, enabled=False))
    tok.assert_not_called()
    m.gh.assert_not_called()


def test_ac1_green_pr_no_push_no_checkout(tmp_path):
    with _mocks(tmp_path, green=True) as m:
        ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    m.checkout.assert_not_called()
    m.restore.assert_not_called()


def test_ac2_reproduced_and_fixed_pushes_once_to_same_branch(tmp_path):
    with _mocks(tmp_path) as m:
        ci_fix.run(_cfg(tmp_path))
    assert m.pushes == [["git", "push", "origin", "feat"]]
    assert ci_fix.get_attempts(SLUG, 1) == 1
    m.checkout.assert_called_once()
    instructions = m.backend.run.call_args.args[0]
    assert instructions.startswith("PROMPT TEXT")
    assert "**Failure to fix**" in instructions
    assert "make check" in instructions
    m.restore.assert_called_once()


def test_uncommitted_agent_edits_are_committed_before_the_single_push(tmp_path):
    with _mocks(tmp_path, status=b" M f.py", agent_commits=False) as m:
        ci_fix.run(_cfg(tmp_path))
    add = m.cmds.index(["git", "add", "-u"])
    commit = m.cmds.index(["git", "commit", "-m", "ci-fix: Run checks"])
    assert add < commit < m.cmds.index(["git", "push", "origin", "feat"])
    assert m.pushes == [["git", "push", "origin", "feat"]]
    assert ["git", "add", "-A"] not in m.cmds


def test_agent_changes_nothing_while_step_passes_is_not_reproduced(tmp_path, caplog):
    with _mocks(tmp_path, agent_commits=False) as m, caplog.at_level(logging.INFO):
        ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    assert not any(c[:2] == ["git", "commit"] for c in m.cmds)
    assert ci_fix.get_attempts(SLUG, 1) == 0  # the agent changed nothing, so no attempt
    assert any("did-not-reproduce" in d and "no change to the branch" in d for d in _digests(caplog))


def test_noop_run_with_step_still_failing_is_not_counted(tmp_path):
    with _mocks(tmp_path, agent_commits=False, after_rc=1):
        ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 0


def test_repeated_noop_for_same_head_and_check_does_not_rerun_the_agent(tmp_path, caplog):
    with _mocks(tmp_path, agent_commits=False, after_rc=1) as m, caplog.at_level(logging.INFO):
        m.run_step.side_effect = [_proc(1), _proc(1)] * 3
        for _ in range(3):
            ci_fix.run(_cfg(tmp_path))
    assert m.backend.run.call_count == 1
    assert ci_fix.get_attempts(SLUG, 1) == 0
    assert any("noop-repeat" in d for d in _digests(caplog))


def test_noop_run_on_dirty_start_tree_is_not_counted(tmp_path):
    with _mocks(tmp_path, agent_commits=False, after_rc=1, status=b" M a.py\n"):
        ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 0


def test_run_that_dirties_tree_without_commit_is_counted(tmp_path):
    with _mocks(tmp_path, agent_commits=False, after_rc=1) as m:
        orig = m.backend.run.side_effect

        def run(*a, **k):
            orig(*a, **k)
            m.status_box[0] = b" M a.py\n"
            return _proc(0)

        m.backend.run.side_effect = run
        ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 1


@pytest.mark.parametrize("exc", [RuntimeError("agent died"), TimeoutError("timeout")])
def test_backend_crash_or_timeout_still_counts(tmp_path, exc):
    with _mocks(tmp_path, backend_raises=exc, agent_commits=False), pytest.raises(CiFixError):
        ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 1


def test_ac3_cap_reached_across_runs_writes_digest(tmp_path, caplog):
    cfg = _cfg(tmp_path, max_attempts=2)
    for _ in range(2):
        with _mocks(tmp_path) as m:
            ci_fix.run(cfg)
        assert len(m.pushes) == 1
    assert ci_fix.get_attempts(SLUG, 1) == 2
    with _mocks(tmp_path) as m, caplog.at_level(logging.INFO):
        ci_fix.run(cfg)
    assert m.pushes == []
    m.checkout.assert_not_called()
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "cap-reached" in digests[0]
    assert "pr=1" in digests[0]


def test_ac4_not_reproduced_no_push_digest(tmp_path, caplog):
    with _mocks(tmp_path, repro_rc=0) as m, caplog.at_level(logging.INFO):
        ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    m.backend.run.assert_not_called()
    assert ci_fix.get_attempts(SLUG, 1) == 0
    assert any("did-not-reproduce" in d for d in _digests(caplog))
    m.restore.assert_called_once()


def test_gate_only_failure_no_agent_no_attempt_gate_only_digest(tmp_path, caplog):
    gates = [
        _Check("CI Gate", "https://x/actions/runs/9/job/1"),
        _Check("merge-reports", "https://x/actions/runs/9/job/2"),
    ]
    with _mocks(tmp_path, failing=gates) as m, caplog.at_level(logging.INFO):
        ci_fix.run(_cfg(tmp_path))
    m.backend.run.assert_not_called()
    m.checkout.assert_not_called()
    assert m.pushes == []
    assert ci_fix.get_attempts(SLUG, 1) == 0
    digests = _digests(caplog)
    assert len(digests) == 1
    assert "gate-only" in digests[0]


@pytest.mark.parametrize("name", ["aggregate-coverage", "propagate-labels", "investigate-flaky", "delegate-tests"])
def test_words_containing_gate_are_not_gates(name):
    assert not ci_fix._is_gate(_Check(name, "https://x/actions/runs/9/job/1"))


@pytest.mark.parametrize("name", ["gate", "CI Gate", "ci-gate", "merge-gate", "integration-gate-tests"])
def test_gate_names_are_gates(name):
    assert ci_fix._is_gate(_Check(name, "https://x/actions/runs/9/job/1"))


def test_non_gate_picked_even_when_gate_listed_first(tmp_path):
    failing = [
        _Check("ci-GATE", "https://x/actions/runs/9/job/1"),
        _Check("Run checks", "https://x/actions/runs/9/job/8"),
    ]
    with _mocks(tmp_path, failing=failing) as m:
        ci_fix.run(_cfg(tmp_path))
    assert len(m.pushes) == 1
    assert ci_fix.get_attempts(SLUG, 1) == 1
    m.backend.run.assert_called_once()


def test_gates_never_sent_after_non_gate_did_not_reproduce(tmp_path, caplog):
    failing = [
        _Check("Run checks", "https://x/actions/runs/9/job/8"),
        _Check("ci-gate", "https://x/actions/runs/9/job/1"),
    ]
    with _mocks(tmp_path, failing=failing, repro_rc=0) as m, caplog.at_level(logging.INFO):
        ci_fix.run(_cfg(tmp_path))
    m.backend.run.assert_not_called()
    m.checkout.assert_called_once()  # only the non-gate was tried
    assert ci_fix.get_attempts(SLUG, 1) == 0
    assert not any("gate-only" in d for d in _digests(caplog))


def test_still_failing_after_fix_no_push_digest(tmp_path, caplog):
    with _mocks(tmp_path, after_rc=1) as m, caplog.at_level(logging.INFO):
        ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    assert ci_fix.get_attempts(SLUG, 1) == 1  # the agent ran, so the attempt counts
    assert any("still-failing" in d for d in _digests(caplog))


def test_ac5_dry_run_no_push_no_counter_change(tmp_path):
    with _mocks(tmp_path) as m:
        ci_fix.run(_cfg(tmp_path), dry_run=True)
    assert m.pushes == []
    m.backend.run.assert_not_called()
    assert ci_fix.get_attempts(SLUG, 1) == 0
    m.checkout.assert_called_once()
    m.restore.assert_called_once()


@pytest.mark.parametrize(
    "pr",
    [_pr(author="someone-else"), _pr(draft=True), _pr(state="CLOSED")],
    ids=["other-author", "draft", "closed"],
)
def test_ac6_out_of_scope_prs_are_skipped(tmp_path, pr):
    with _mocks(tmp_path, prs=[pr]) as m:
        ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    m.check_ci.assert_not_called()
    m.checkout.assert_not_called()


def test_ac6_pr_found_closed_on_recheck_is_skipped(tmp_path):
    state = {"state": "CLOSED", "isDraft": False, "mergeable": "MERGEABLE", "headRefOid": "sha0"}
    with _mocks(tmp_path, pr_state=state) as m:
        ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    m.checkout.assert_not_called()


def test_ac7_pr_option_processes_only_that_pr(tmp_path):
    with _mocks(tmp_path, prs=[_pr(number=7, branch="seven")]) as m:
        ci_fix.run(_cfg(tmp_path), pr=7)
    assert m.gh.call_args.args[0][:3] == ["pr", "view", "7"]
    assert m.check_ci.call_args.args[1] == 7
    assert m.check_ci.call_args.args[2] == "main"
    assert m.pushes == [["git", "push", "origin", "seven"]]


def test_all_prs_are_processed_without_pr_option(tmp_path):
    with _mocks(tmp_path, prs=[_pr(1, "a"), _pr(2, "b")]) as m:
        m.run_step.side_effect = [_proc(1), _proc(0), _proc(1), _proc(0)]
        ci_fix.run(_cfg(tmp_path))
    assert [p[3] for p in m.pushes] == ["a", "b"]


def test_ac8_push_is_plain_and_to_own_branch(tmp_path):
    with _mocks(tmp_path, prs=[_pr(branch="my-branch")]) as m:
        ci_fix.run(_cfg(tmp_path))
    assert m.pushes
    for cmd in m.pushes:
        assert not any(a.startswith("--force") or a == "-f" or ":" in a for a in cmd)
        assert cmd == ["git", "push", "origin", "my-branch"]


def test_push_rejected_is_rebase_needed_digest_still_an_attempt(tmp_path, caplog):
    with _mocks(tmp_path, push_rc=1) as m, caplog.at_level(logging.INFO):
        ci_fix.run(_cfg(tmp_path))
    assert len(m.pushes) == 1
    assert ci_fix.get_attempts(SLUG, 1) == 1  # counted when the agent ran
    assert any("rebase-needed" in d for d in _digests(caplog))


def test_push_failure_other_than_rejection_raises_and_still_restores(tmp_path, caplog):
    with (
        _mocks(tmp_path, push_rc=128, push_stderr=b"fatal: Authentication failed") as m,
        caplog.at_level(logging.INFO),
        pytest.raises(CiFixError, match="push to feat failed"),
    ):
        ci_fix.run(_cfg(tmp_path))
    assert len(m.pushes) == 1
    assert ci_fix.get_attempts(SLUG, 1) == 1  # recorded before the agent ran, so it still counts
    assert not any("rebase-needed" in d for d in _digests(caplog))
    m.restore.assert_called_once()


def test_conflicting_pr_is_rebase_needed_digest(tmp_path, caplog):
    state = {"state": "OPEN", "isDraft": False, "mergeable": "CONFLICTING", "headRefOid": "sha0"}
    with _mocks(tmp_path, pr_state=state) as m, caplog.at_level(logging.INFO):
        ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    m.checkout.assert_not_called()
    assert any("rebase-needed" in d for d in _digests(caplog))


def test_unreadable_check_output_is_parse_failure_digest(tmp_path, caplog):
    with _mocks(tmp_path) as m, patch(P + "_read_failing_checks", return_value=None), caplog.at_level(logging.INFO):
        ci_fix.run(_cfg(tmp_path))
    assert m.pushes == []
    m.checkout.assert_not_called()
    assert any("parse-failure" in d for d in _digests(caplog))


def test_step_is_looked_up_after_checkout_of_the_pr_branch(tmp_path, caplog):
    order: list[str] = []
    with (
        _mocks(tmp_path) as m,
        patch(P + "find_step_command", side_effect=lambda *a: order.append("find") or None),
        caplog.at_level(logging.INFO),
    ):
        m.checkout.side_effect = lambda *a: order.append("checkout")
        ci_fix.run(_cfg(tmp_path))
    assert order == ["checkout", "find"]
    assert m.pushes == []
    m.restore.assert_called_once()
    assert any("parse-failure" in d for d in _digests(caplog))


def test_ac9_restore_runs_when_exception_mid_run(tmp_path):
    with (
        _mocks(tmp_path, backend_raises=RuntimeError("agent died")) as m,
        pytest.raises(CiFixError, match="agent died"),
    ):
        ci_fix.run(_cfg(tmp_path))
    m.restore.assert_called_once()
    assert m.restore.call_args.args[:2] == ("orig", "feat")
    assert m.pushes == []


def test_one_pr_failing_does_not_stop_the_others(tmp_path):
    with _mocks(tmp_path, prs=[_pr(1, "a"), _pr(2, "b")]) as m:
        m.run_step.side_effect = [_proc(1), RuntimeError("timeout"), _proc(1), _proc(0)]
        m.backend.run.side_effect = [_proc(0), _proc(0)]
        with pytest.raises(CiFixError, match="#1"):
            ci_fix.run(_cfg(tmp_path))
    assert m.restore.call_count == 2


def test_counters_of_closed_prs_are_pruned(tmp_path):
    ci_fix._record_attempt(SLUG, 1)
    ci_fix._record_attempt(SLUG, 99)
    with _mocks(tmp_path, green=False, after_rc=1):
        ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 2  # open and not green: kept, plus this run's attempt
    assert ci_fix.get_attempts(SLUG, 99) == 0


def test_counter_reset_when_ci_green(tmp_path):
    ci_fix._record_attempt(SLUG, 1)
    ci_fix._record_attempt(SLUG, 2)
    with _mocks(tmp_path, prs=[_pr(1)], green=True):
        ci_fix.run(_cfg(tmp_path), pr=1)
    assert ci_fix.get_attempts(SLUG, 1) == 0
    assert ci_fix.get_attempts(SLUG, 2) == 1


def test_counter_untouched_while_not_green(tmp_path):
    ci_fix._record_attempt(SLUG, 1)
    with _mocks(tmp_path, green=False) as m:
        m.run_step.side_effect = [_proc(0)]  # does not reproduce: no agent run
        ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 1


def test_counter_untouched_in_dry_run_when_green(tmp_path):
    ci_fix._record_attempt(SLUG, 1)
    with _mocks(tmp_path, green=True):
        ci_fix.run(_cfg(tmp_path), dry_run=True)
    assert ci_fix.get_attempts(SLUG, 1) == 1


def test_reset_attempts_is_idempotent_and_writes_nothing_without_key(tmp_path):
    ci_fix._reset_attempts(SLUG, 1)
    with patch("dotharness.pr_review.state._atomic_write") as write:
        ci_fix._reset_attempts(SLUG, 1)
    write.assert_not_called()
    ci_fix._record_attempt(SLUG, 1)
    ci_fix._reset_attempts(SLUG, 1)
    assert ci_fix.get_attempts(SLUG, 1) == 0
    with patch("dotharness.pr_review.state._atomic_write") as write:
        ci_fix._reset_attempts(SLUG, 1)
    write.assert_not_called()


@pytest.fixture(autouse=True)
def _state_home(tmp_path):
    with patch("dotharness.pr_review.state.XDG_DATA", tmp_path / "xdg"):
        yield


WORKFLOW = """name: Main
defaults:
  run:
    working-directory: tools/pr-review

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
    assert ci_fix.find_step_command(str(tmp_path), "Run checks") == _Step("Run checks", "make check", "tools/pr-review")
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
    for file, name, wd in (("a.yml", "Alpha", "tools/a"), ("b.yml", "Beta", "tools/b")):
        (wf / file).write_text(
            f"name: {name}\njobs:\n  t:\n    steps:\n      - name: Run tests\n"
            f"        working-directory: {wd}\n        run: pytest\n",
            encoding="utf-8",
        )
    (wf / "c.yml").write_text(
        "jobs:\n  t:\n    steps:\n      - name: Run tests\n        run: make c\n", encoding="utf-8"
    )
    assert ci_fix.find_step_command(str(tmp_path), "Run tests", "Beta") == _Step("Run tests", "pytest", "tools/b")
    assert ci_fix.find_step_command(str(tmp_path), "Run tests", ".github/workflows/c.yml") == _Step(
        "Run tests", "make c", ""
    )
    assert ci_fix.find_step_command(str(tmp_path), "Run tests", "Nope") is None
    assert ci_fix.find_step_command(str(tmp_path), "Run tests") == _Step("Run tests", "pytest", "tools/a")


def test_read_failing_checks_carries_the_workflow():
    out = b'[{"name":"t (3.12)","bucket":"fail","link":"https://x/actions/runs/1/job/2","workflow":"Beta"}]'
    with patch("dotharness.pr_review.runners.ci_checks.gh", return_value=_proc(0, out)):
        assert ci_fix._read_failing_checks("o/r", 1, {}) == [
            _Check("t (3.12)", "https://x/actions/runs/1/job/2", "Beta")
        ]


def test_read_failing_checks_ignores_superseded_failed_run():
    out = (
        b'[{"name":"t","bucket":"fail","link":"https://x/actions/runs/1/job/2","workflow":"W",'
        b'"startedAt":"2026-01-01T10:00:00Z","completedAt":"2026-01-01T10:05:00Z"},'
        b'{"name":"t","bucket":"pass","link":"https://x/actions/runs/3/job/4","workflow":"W",'
        b'"startedAt":"2026-01-01T11:00:00Z","completedAt":"2026-01-01T11:05:00Z"}]'
    )
    with patch("dotharness.pr_review.runners.ci_checks.gh", return_value=_proc(0, out)):
        assert ci_fix._read_failing_checks("o/r", 1, {}) == []


def test_read_failing_checks_keeps_latest_failed_run_and_fails_closed_without_timestamps():
    base = '{"name":"t","bucket":"%s","link":"https://x/actions/runs/%d/job/2","workflow":"W"%s}'
    ts = lambda a, b: f',"startedAt":"{a}","completedAt":"{b}"'
    newer_fail = f"[{base % ('pass', 1, ts('2026-01-01T10:00:00Z', '2026-01-01T10:05:00Z'))},"
    newer_fail += f"{base % ('fail', 3, ts('2026-01-01T11:00:00Z', '2026-01-01T11:05:00Z'))}]"
    with patch("dotharness.pr_review.runners.ci_checks.gh", return_value=_proc(0, newer_fail.encode())):
        assert ci_fix._read_failing_checks("o/r", 1, {}) == [_Check("t", "https://x/actions/runs/3/job/2", "W")]
    no_ts = f"[{base % ('fail', 1, '')},{base % ('pass', 3, '')}]"
    with patch("dotharness.pr_review.runners.ci_checks.gh", return_value=_proc(0, no_ts.encode())):
        assert ci_fix._read_failing_checks("o/r", 1, {}) is None


def test_failed_step_name_from_log():
    assert ci_fix._failed_step_name("quality\tRun checks\t2024Z ##[error]x") == "Run checks"
    assert ci_fix._failed_step_name("no tabs here") is None


def test_failed_step_name_ignores_unknown_step_placeholder():
    assert ci_fix._failed_step_name("quality\tUNKNOWN STEP\t2024Z error") is None
    log = "quality\tUNKNOWN STEP\t2024Z a\nquality\tRun checks\t2024Z b"
    assert ci_fix._failed_step_name(log) == "Run checks"


def test_failed_step_from_api_picks_first_failed_step():
    steps = [{"name": "Set up", "conclusion": "success"}, {"name": "Run checks", "conclusion": "failure"}]
    with patch(P + "_gh", return_value=_proc(0, json.dumps({"steps": steps}).encode())) as gh:
        assert ci_fix._failed_step_from_api("o/r", "https://x/actions/runs/9/job/8", {}) == "Run checks"
    assert gh.call_args.args[0] == ["api", "repos/o/r/actions/jobs/8"]


@pytest.mark.parametrize(
    ("link", "result"),
    [
        ("https://x/actions/runs/9", _proc(0, b"{}")),  # no job id: no call at all
        ("https://x/actions/runs/9/job/8", _proc(1)),
        ("https://x/actions/runs/9/job/8", _proc(0, b"not json")),
        ("https://x/actions/runs/9/job/8", _proc(0, b"[]")),
        ("https://x/actions/runs/9/job/8", _proc(0, b'{"steps":[{"name":"a","conclusion":"success"}]}')),
    ],
)
def test_failed_step_from_api_none_when_unavailable(link, result):
    with patch(P + "_gh", return_value=result):
        assert ci_fix._failed_step_from_api("o/r", link, {}) is None


def test_fix_check_uses_api_step_when_log_says_unknown_step(tmp_path):
    unknown = "quality\tUNKNOWN STEP\t2024Z error"
    with (
        _mocks(tmp_path) as m,
        patch(P + "_read_failed_log", return_value=unknown),
        patch(P + "_failed_step_from_api", return_value="Run checks") as api,
        patch(P + "find_step_command", return_value=_Step("Run checks", "make check", "")) as find,
    ):
        ci_fix.run(_cfg(tmp_path))
    api.assert_called_once()
    assert find.call_args.args[1] == "Run checks"
    assert m.pushes


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


def test_unsafe_change_flags_workflow_moved_out_of_github(tmp_path):
    git, sha = _git_repo(tmp_path)
    git("mv", ".github/workflows/ci.yml", "docs/ci.yml")
    result = ci_fix._unsafe_change(str(tmp_path), {}, sha)
    assert result is not None
    assert ".github/workflows/ci.yml" in result


def test_unsafe_change_flags_non_ascii_workflow_path(tmp_path):
    git, sha = _git_repo(tmp_path)
    (tmp_path / ".github" / "workflows" / "\u00e9.yml").write_text("x\n", encoding="utf-8")
    git("add", "-A")
    result = ci_fix._unsafe_change(str(tmp_path), {}, sha)
    assert result is not None
    assert ".github/workflows/\u00e9.yml" in result


def test_unsafe_change_reports_unquoted_untracked_non_ascii_path(tmp_path):
    _, sha = _git_repo(tmp_path)
    (tmp_path / "docs" / "\u00e9 file.txt").write_text("x\n", encoding="utf-8")
    result = ci_fix._unsafe_change(str(tmp_path), {}, sha)
    assert result is not None
    assert "docs/\u00e9 file.txt" in result


def test_unsafe_change_allows_ordinary_tracked_edit(tmp_path):
    _, sha = _git_repo(tmp_path)
    (tmp_path / "docs" / "a.md").write_text("x\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)  # noqa: S607
    assert ci_fix._unsafe_change(str(tmp_path), {}, sha) is None


def test_unsafe_change_runs_before_the_post_fix_step(tmp_path, caplog):
    with (
        _mocks(tmp_path) as m,
        patch(P + "_unsafe_change", return_value="change touches .github/: x"),
        caplog.at_level(logging.INFO),
    ):
        ci_fix.run(_cfg(tmp_path))
    assert m.run_step.call_count == 1  # only the reproduction run; the agent's tree was never executed
    assert m.pushes == []
    assert any("unsafe-change" in d for d in _digests(caplog))
