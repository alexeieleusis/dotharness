import contextlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from harness.config import CiFixConfig, HarnessConfig, HarnessSection, RepoConfig
from harness.runners import ci_fix
from harness.runners.auto_approve_github import SafetyResult, SafetyStatus
from harness.runners.ci_fix import DIGEST_PREFIX, CiFixError, _Check, _Step

P = "harness.runners.ci_fix."
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
    pr_state=None,
    backend_raises=None,
):
    prs = [_pr()] if prs is None else prs
    head = {"sha": "sha0"}
    pushes: list[list[str]] = []
    step_results = [_proc(repro_rc, b"boom"), _proc(after_rc)]

    def fake_gh(args, env):
        data = prs if args[1] == "list" else prs[0]
        return _proc(0, json.dumps(data).encode())

    def fake_run_cmd(cmd, cwd, env, timeout, check=True, shell=False):
        if cmd[:2] == ["git", "rev-parse"]:
            return _proc(0, head["sha"].encode())
        if cmd[:2] == ["git", "status"]:
            return _proc(0, b"")
        if cmd[:2] == ["git", "push"]:
            pushes.append(cmd)
            return _proc(push_rc, b"", b"! [rejected] (non-fast-forward)" if push_rc else b"")
        return _proc(0)

    def backend_run(*args, **kwargs):
        if backend_raises:
            raise backend_raises
        head["sha"] = f"sha-{len(pushes)}-{head['sha']}"
        return _proc(0)

    backend = MagicMock()
    backend.run.side_effect = backend_run
    safety = None if green else SafetyResult(SafetyStatus.SKIP, "ci-blocked")
    state = pr_state or {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "headRefOid": "sha0"}
    with (
        patch("harness.state.XDG_DATA", tmp_path / "xdg"),
        patch(P + "acquire_lock", return_value=contextlib.nullcontext()),
        patch(P + "get_gh_token", return_value="tok"),
        patch(P + "build_subprocess_env", return_value={"GITHUB_TOKEN": "tok"}),
        patch(P + "get_current_user", return_value="me"),
        patch(P + "_gh", side_effect=fake_gh) as gh,
        patch(P + "_check_ci", return_value=safety) as check_ci,
        patch(P + "_read_pr_state", return_value=state),
        patch(P + "_read_failing_checks", return_value=[_Check("Run checks", "https://x/actions/runs/9/job/8")]),
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
            backend=backend,
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
    with _mocks(tmp_path, green=True):
        ci_fix.run(_cfg(tmp_path))
    assert ci_fix.get_attempts(SLUG, 1) == 1
    assert ci_fix.get_attempts(SLUG, 99) == 0


@pytest.fixture(autouse=True)
def _state_home(tmp_path):
    with patch("harness.state.XDG_DATA", tmp_path / "xdg"):
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


def test_failed_step_name_from_log():
    assert ci_fix._failed_step_name("quality\tRun checks\t2024Z ##[error]x") == "Run checks"
    assert ci_fix._failed_step_name("no tabs here") is None
