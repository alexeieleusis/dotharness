import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest

from dotharness.code_host import (
    Capability,
    ChangeRequestState,
    CodeHostError,
    CodeHostTimeoutError,
    UnsupportedCapabilityError,
    User,
)
from dotharness.code_host.testing import InMemoryCodeHost
from dotharness.pr_review import state
from dotharness.pr_review.config import PreCommand, SubDir
from dotharness.pr_review.runners import review_prs
from tests.pr_review.runners.runner_support import REPO, make_change_request, make_scaffold


@pytest.fixture(autouse=True)
def clean_mock():
    """The failure-path clean runs real git otherwise; patch it by name in the runner's namespace."""
    with (
        patch("dotharness.pr_review.runners.review_prs.git_clean_working_dir") as m,
        # The shared failure helper calls the common-module binding; point it at the same mock.
        patch("dotharness.pr_review.runners.common.git_clean_working_dir", m),
    ):
        yield m


@pytest.fixture(autouse=True)
def dirty_mock(clean_mock):
    """The failure-path probe runs real git otherwise; default to a successful clean and a clean tree."""
    clean_mock.return_value = True
    with patch("dotharness.pr_review.runners.common.git_is_dirty", return_value=False) as m:
        yield m


@pytest.fixture(autouse=True)
def pre_checkout_clean_mock():
    """The pre-checkout probe runs real git otherwise; patch it by name in the runner's namespace."""
    with patch("dotharness.pr_review.runners.review_prs.git_clean_if_dirty") as m:
        yield m


def _seed(prs=()) -> InMemoryCodeHost:
    """An in-memory host holding ``prs`` (ChangeRequests) as open change requests authored by alice."""
    host = InMemoryCodeHost(repository="acme/frontend", user="alice")
    for pr in prs:
        host.add_change_request(
            REPO,
            number=pr.number,
            head_reference=pr.head_reference,
            head_revision=pr.head_revision,
            base_reference=pr.base_reference,
            author=pr.author.login if pr.author else "alice",
            state=pr.state,
            is_draft=pr.is_draft,
            requested_reviewers=[u.login for u in pr.requested_reviewers],
        )
    return host


def _pr(number, ref="feat", sha=None, **fields):
    return make_change_request(number, head_reference=ref, head_revision=sha or f"sha{number}", **fields)


def _make_config(tmp_path, authors="*", subdirs=None):
    from dotharness.pr_review.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    return HarnessConfig(
        harness=HarnessSection(
            backend="opencode",
            host_token_cmd="echo tok",  # noqa: S106
            knowledge_dir=tmp_path / "knowledge",
            path_prepend=[],
            env={},
            backend_timeout_seconds=900,
        ),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path, subdirs=subdirs or []),
        vibe_heal=VibehealConfig(
            enabled=True,
            python="/venv/bin/python3",
            authors=authors,
            vibe_heal_timeout=10,
            vibe_heal_post_timeout=5,
        ),
    )


async def test_skips_prs_with_unchanged_head_sha(tmp_xdg, tmp_path):
    from dotharness.pr_review.config import SubDir

    state.record_reviewed_sha("acme-frontend", 8, "sha8", 1000.0)
    state.record_reviewed_sha("acme-frontend", 10, "sha10", 1000.0)
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    prs = [
        _pr(8, "b", "sha8"),
        _pr(10, "b", "sha10"),
    ]
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record") as mock_detach,
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg)
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert vibe_calls == []
    # Both PRs were skipped before any checkout was needed.
    mock_detach.assert_not_called()


def test_already_reviewed_false_for_never_reviewed_pr():
    pr = _pr(1)
    assert review_prs._already_reviewed({}, pr, min_interval_seconds=86400, now=2_000_000.0) is False


def test_already_reviewed_true_when_sha_unchanged_regardless_of_interval():
    reviewed_shas = {"8": {"sha": "sha8", "reviewed_at": 1000.0}}
    pr = _pr(8)
    # Interval is 0 (already elapsed) but the SHA still matches, so it's skipped anyway.
    assert review_prs._already_reviewed(reviewed_shas, pr, min_interval_seconds=0, now=2_000_000.0) is True


def test_already_reviewed_true_when_sha_changed_but_within_interval():
    reviewed_shas = {"7": {"sha": "old-sha", "reviewed_at": 1000.0}}
    pr = _pr(7, sha="new-sha")
    # Only 100s have passed since the last review; the 24h (86400s) interval hasn't elapsed.
    assert review_prs._already_reviewed(reviewed_shas, pr, min_interval_seconds=86400, now=1100.0) is True


def test_already_reviewed_false_when_sha_changed_and_interval_elapsed():
    reviewed_shas = {"7": {"sha": "old-sha", "reviewed_at": 1000.0}}
    pr = _pr(7, sha="new-sha")
    # 25 hours have passed, past the 24h (86400s) interval.
    assert review_prs._already_reviewed(reviewed_shas, pr, min_interval_seconds=86400, now=1000.0 + 25 * 3600) is False


async def test_run_locked_skips_pr_with_new_sha_within_min_reanalysis_interval(tmp_xdg, tmp_path):
    from dotharness.pr_review.config import SubDir

    state.record_reviewed_sha("acme-frontend", 7, "old-sha", 1_000_000.0)
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    prs = [_pr(7, "feat", "new-sha")]
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record") as mock_detach,
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
        patch(
            "dotharness.pr_review.runners.review_prs.time.time", return_value=1_000_000.0 + 3600
        ),  # 1h later, within 24h default
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg)
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert vibe_calls == []
    mock_detach.assert_not_called()


async def test_run_locked_reprocesses_pr_with_new_sha_after_min_reanalysis_interval_elapses(tmp_xdg, tmp_path):
    from dotharness.pr_review.config import SubDir

    state.record_reviewed_sha("acme-frontend", 7, "old-sha", 1_000_000.0)
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    prs = [_pr(7, "feat", "new-sha")]
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
        patch(
            "dotharness.pr_review.runners.review_prs.time.time", return_value=1_000_000.0 + 25 * 3600
        ),  # 25h later, past 24h default
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg)
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert len(vibe_calls) >= 1
    assert state.get_reviewed_sha("acme-frontend", 7) == "new-sha"


async def test_run_locked_respects_configured_min_reanalysis_interval_hours(tmp_xdg, tmp_path):
    from dotharness.pr_review.config import SubDir

    state.record_reviewed_sha("acme-frontend", 7, "old-sha", 1_000_000.0)
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    cfg.vibe_heal.min_reanalysis_interval_hours = 1
    prs = [_pr(7, "feat", "new-sha")]
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
        # 2h later: past the configured 1h interval, even though it's within the 24h default.
        patch("dotharness.pr_review.runners.review_prs.time.time", return_value=1_000_000.0 + 2 * 3600),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg)
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert len(vibe_calls) >= 1


async def test_reprocesses_pr_when_head_sha_changes(tmp_xdg, tmp_path):
    from dotharness.pr_review.config import SubDir

    state.record_reviewed_sha("acme-frontend", 7, "old-sha", 1000.0)
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    prs = [_pr(7, "feat", "new-sha")]
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg)
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert len(vibe_calls) >= 1
    assert state.get_reviewed_sha("acme-frontend", 7) == "new-sha"


async def test_processes_new_prs(tmp_xdg, tmp_path):
    from dotharness.pr_review.config import SubDir

    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    prs = [_pr(5, "feat", "sha5")]
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg)
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert len(vibe_calls) >= 1


async def test_failed_pr_fetch_does_not_prune_reviewed_shas(tmp_xdg, tmp_path):
    from dotharness.pr_review.config import SubDir

    state.record_reviewed_sha("acme-frontend", 5, "sha5", 1000.0)
    state.record_reviewed_sha("acme-frontend", 6, "sha6", 1000.0)
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    host = _seed()
    host.fail_next("ChangeRequests.list", CodeHostError("boom"))
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(host)),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record") as mock_detach,
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg)
    assert state.get_reviewed_sha("acme-frontend", 5) == "sha5"
    assert state.get_reviewed_sha("acme-frontend", 6) == "sha6"
    mock_detach.assert_not_called()


async def test_updates_state_after_success(tmp_xdg, tmp_path):
    from dotharness.pr_review.config import SubDir

    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    prs = [_pr(7, "feat", "sha7")]
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg)
    assert state.get_reviewed_sha("acme-frontend", 7) == "sha7"


async def test_coverage_flag_passed_when_true(tmp_xdg, tmp_path):
    from dotharness.pr_review.config import SubDir

    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], coverage=True, timeout=30)])
    prs = [_pr(3, "feat", "sha3")]
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg)
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c) and "--coverage" in str(c)]
    assert len(vibe_calls) >= 1


async def test_pr_url_graceful_on_pr_from_url_returns_empty(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold()),
        patch("dotharness.pr_review.runners.review_prs.pr_from_url", return_value=None),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg, pr_url="https://github.com/acme/frontend/pull/3")
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert vibe_calls == []


async def test_pr_url_graceful_on_pr_from_url_raises(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold()),
        patch("dotharness.pr_review.runners.review_prs.pr_from_url", side_effect=RuntimeError("network error")),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        with pytest.raises(RuntimeError):
            await review_prs._run_locked(cfg, pr_url="https://github.com/acme/frontend/pull/3")
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert vibe_calls == []


async def test_pr_url_bypasses_reviewed_sha_filtering(tmp_xdg, tmp_path):
    # Even though this PR's current head SHA is already recorded as reviewed, --pr forces
    # it to run anyway — the whole point of the manual override.
    state.record_reviewed_sha("acme-frontend", 3, "sha3", 1000.0)
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    pr = make_change_request(3, head_reference="feat", head_revision="sha3")
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold()),
        patch("dotharness.pr_review.runners.review_prs.pr_from_url", return_value=pr),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg, pr_url="https://github.com/acme/frontend/pull/3")
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert len(vibe_calls) >= 1


async def test_pr_url_does_not_update_reviewed_shas(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    pr = make_change_request(3, head_reference="feat", head_revision="sha3")
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold()),
        patch("dotharness.pr_review.runners.review_prs.pr_from_url", return_value=pr),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg, pr_url="https://github.com/acme/frontend/pull/3")
    assert state.read_vibe_heal_state("acme-frontend")["reviewed_shas"] == {}


async def test_run_pre_commands_continues_after_non_critical_failure(tmp_path):
    from dotharness.pr_review.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(),
    )
    subdir = SubDir(path=".", pre_commands=[PreCommand(cmd="pnpm ci"), PreCommand(cmd="echo hi")])
    with patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run:
        mock_run.side_effect = [subprocess.CalledProcessError(1, "pnpm ci"), MagicMock(returncode=0)]
        result = await review_prs._run_pre_commands(subdir, cfg, {})
    assert result is True
    assert mock_run.call_count == 2


async def test_run_pre_commands_aborts_after_critical_failure(tmp_path):
    from dotharness.pr_review.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(),
    )
    subdir = SubDir(
        path=".",
        pre_commands=[PreCommand(cmd="poetry install", critical=True), PreCommand(cmd="echo hi")],
    )
    with patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "poetry install")
        result = await review_prs._run_pre_commands(subdir, cfg, {})
    assert result is False
    assert mock_run.call_count == 1


async def test_run_pre_commands_continues_after_non_critical_timeout(tmp_path):
    from dotharness.pr_review.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(),
    )
    subdir = SubDir(path=".", pre_commands=[PreCommand(cmd="pnpm ci"), PreCommand(cmd="echo hi")])
    with patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run:
        mock_run.side_effect = [subprocess.TimeoutExpired("pnpm ci", 30), MagicMock(returncode=0)]
        result = await review_prs._run_pre_commands(subdir, cfg, {})
    assert result is True
    assert mock_run.call_count == 2


async def test_run_pre_commands_aborts_after_critical_timeout(tmp_path):
    from dotharness.pr_review.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(),
    )
    subdir = SubDir(
        path=".",
        pre_commands=[PreCommand(cmd="poetry install", critical=True), PreCommand(cmd="echo hi")],
    )
    with patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired("poetry install", 30)
        result = await review_prs._run_pre_commands(subdir, cfg, {})
    assert result is False
    assert mock_run.call_count == 1


async def test_run_locked_re_requests_review_once_per_pr_across_subdirs(tmp_xdg, tmp_path):
    cfg = _make_config(
        tmp_path,
        subdirs=[SubDir(path="a", pre_commands=[], coverage=False, timeout=30), SubDir(path="b")],
    )
    host = _seed([_pr(9, "feat", "sha9", requested_reviewers=(User("alice"),))])
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(host)),
        patch("dotharness.pr_review.runners.review_prs.get_changed_files", return_value=["a/foo.py", "b/bar.py"]),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg)
    requests = [c for c in host.calls if c.operation == "ChangeRequests.request_reviewer"]
    assert [c.args for c in requests] == [(REPO, 9, "alice")]
    assert [c.operation for c in host.calls].count("ChangeRequests.get") == 1


async def test_run_locked_does_not_re_request_review_if_not_previously_requested(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    host = _seed([_pr(9, "feat", "sha9")])
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(host)),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg)
    assert not [c for c in host.calls if c.operation == "ChangeRequests.request_reviewer"]


async def test_process_subdir_skips_vibe_heal_after_critical_pre_command_failure(tmp_path):
    from dotharness.pr_review.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(enabled=True, python="/venv/bin/python3"),
    )
    subdir = SubDir(path=".", pre_commands=[PreCommand(cmd="poetry install", critical=True)])
    host = _seed([_pr(1)]).code_host()
    with patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "poetry install")
        result = await review_prs._process_subdir(host, REPO, 1, subdir, cfg, {})
    assert result is False
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert vibe_calls == []


async def test_process_pr_skips_when_closed_since_discovery(tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    pr = _pr(42, "feat", "sha42", state=ChangeRequestState.MERGED)
    host = _seed([pr]).code_host()
    with patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout") as mock_fetch:
        result = await review_prs._process_pr(pr, cfg, host, REPO, {}, "alice", str(tmp_path))
    assert result is False
    mock_fetch.assert_not_called()


async def test_process_subdir_skips_post_when_pr_closed_during_review(tmp_path):
    from dotharness.pr_review.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(enabled=True, python="/venv/bin/python3"),
    )
    subdir = SubDir(path=".", pre_commands=[])
    host = _seed([_pr(1, state=ChangeRequestState.MERGED)]).code_host()
    with patch(
        "dotharness.pr_review.runners.review_prs.run_cmd", return_value=MagicMock(returncode=0, stdout=b"[]")
    ) as mock_run:
        result = await review_prs._process_subdir(host, REPO, 1, subdir, cfg, {})
    assert result is False
    post_calls = [c for c in mock_run.call_args_list if "--post" in str(c)]
    assert post_calls == []


def test_subdir_has_changes_root_always_matches():
    assert review_prs._subdir_has_changes(".", []) is True
    assert review_prs._subdir_has_changes(".", ["anything.py"]) is True


def test_subdir_has_changes_bare_name_is_not_a_match():
    assert review_prs._subdir_has_changes("frontend", ["frontend"]) is False


def test_subdir_has_changes_prefix_match():
    assert review_prs._subdir_has_changes("frontend", ["frontend/src/app.tsx"]) is True


def test_subdir_has_changes_no_match():
    assert review_prs._subdir_has_changes("backend", ["frontend/src/app.tsx"]) is False


def test_subdir_has_changes_does_not_false_positive_on_sibling_name():
    assert review_prs._subdir_has_changes("frontend", ["frontend-legacy/src/app.tsx"]) is False


def test_subdir_has_changes_handles_trailing_slash_in_config():
    assert review_prs._subdir_has_changes("frontend/", ["frontend/src/app.tsx"]) is True


async def test_run_locked_skips_subdir_with_no_changes(tmp_xdg, tmp_path):
    cfg = _make_config(
        tmp_path,
        subdirs=[SubDir(path="a", pre_commands=[], coverage=False, timeout=30), SubDir(path="b")],
    )
    prs = [_pr(11, "feat", "sha11")]
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs.get_changed_files", return_value=["a/src/foo.py"]),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]")
        await review_prs._run_locked(cfg)
    vibe_calls = [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert len(vibe_calls) >= 1
    subdir_b_path = str(tmp_path / "b")
    assert all(c.kwargs.get("cwd") != subdir_b_path for c in vibe_calls)


async def test_run_base_analysis_skips_when_sha_unchanged(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="main@deadbeef")
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    with patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"deadbeef\n", stderr=b"")
        result = await review_prs._run_base_analysis(cfg, {}, "main", "host default")
    assert result is True
    # Only fetch + rev-parse should run; no checkout, since the SHA already matches.
    assert not any("checkout" in str(c) for c in mock_run.call_args_list)


async def test_run_base_analysis_runs_subdirs_and_persists_sha_on_success(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="old-sha")
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    with (
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="original-sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore") as mock_restore,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")
        result = await review_prs._run_base_analysis(cfg, {}, "main", "host default")
    assert result is True
    assert state.read_vibe_heal_state("acme-frontend")["last_main_sha"] == "main@new-sha"
    vibe_calls = [c for c in mock_run.call_args_list if "--baseline" in str(c)]
    assert len(vibe_calls) == 1
    mock_restore.assert_called_once_with("original-sha", "", str(tmp_path), {})


async def test_run_base_analysis_does_not_persist_sha_when_subdir_fails(tmp_xdg, tmp_path):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="old-sha")
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])

    def fake_run_cmd(cmd, **kwargs):
        if "--baseline" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")

    with (
        patch("dotharness.pr_review.runners.review_prs.run_cmd", side_effect=fake_run_cmd),
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="original-sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore") as mock_restore,
    ):
        result = await review_prs._run_base_analysis(cfg, {}, "main", "host default")
    assert result is False
    assert state.read_vibe_heal_state("acme-frontend")["last_main_sha"] == "old-sha"
    mock_restore.assert_called_once_with("original-sha", "", str(tmp_path), {})


async def test_run_base_analysis_raises_on_git_fetch_failure(tmp_xdg, tmp_path, caplog, clean_mock):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    with patch.object(type(cfg), "repo_slug", "acme-frontend"):
        state.write_vibe_heal_state("acme-frontend", last_main_sha="old-sha")

    def fake_run_cmd(cmd, **kwargs):
        if "git" in cmd and "fetch" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"fetch error")
        return MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")

    with (
        patch.object(type(cfg), "repo_slug", "acme-frontend"),
        patch("dotharness.pr_review.runners.review_prs.run_cmd", side_effect=fake_run_cmd) as mock_run,
        caplog.at_level("WARNING"),
        pytest.raises(review_prs.BaseAnalysisError),
    ):
        await review_prs._run_base_analysis(cfg, {}, "main", "host default")
    assert "Base analysis: git fetch failed: fetch error" in caplog.text
    assert mock_run.call_count == 1
    assert "fetch" in str(mock_run.call_args_list[0])
    clean_mock.assert_called_once_with(str(tmp_path), {})


async def test_run_base_analysis_raises_on_rev_parse_failure(tmp_xdg, tmp_path, caplog, clean_mock):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    with patch.object(type(cfg), "repo_slug", "acme-frontend"):
        state.write_vibe_heal_state("acme-frontend", last_main_sha="old-sha")

    def fake_run_cmd(cmd, **kwargs):
        if "git" in cmd and "fetch" in cmd:
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if "rev-parse" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"rev-parse error")
        return MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")

    with (
        patch.object(type(cfg), "repo_slug", "acme-frontend"),
        patch("dotharness.pr_review.runners.review_prs.run_cmd", side_effect=fake_run_cmd) as mock_run,
        caplog.at_level("WARNING"),
        pytest.raises(review_prs.BaseAnalysisError),
    ):
        await review_prs._run_base_analysis(cfg, {}, "main", "host default")
    assert "Base analysis: git rev-parse failed: rev-parse error" in caplog.text
    assert mock_run.call_count == 2
    clean_mock.assert_called_once_with(str(tmp_path), {})


async def test_run_base_analysis_raises_on_checkout_failure(tmp_xdg, tmp_path, caplog, clean_mock):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    with patch.object(type(cfg), "repo_slug", "acme-frontend"):
        state.write_vibe_heal_state("acme-frontend", last_main_sha="old-sha")

    def fake_run_cmd(cmd, **kwargs):
        if "git" in cmd and "fetch" in cmd:
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if "rev-parse" in cmd:
            return MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")
        if "checkout" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"checkout error")
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with (
        patch.object(type(cfg), "repo_slug", "acme-frontend"),
        patch("dotharness.pr_review.runners.review_prs.run_cmd", side_effect=fake_run_cmd),
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="original-sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore") as mock_restore,
        caplog.at_level("WARNING"),
        pytest.raises(review_prs.BaseAnalysisError),
    ):
        await review_prs._run_base_analysis(cfg, {}, "main", "host default")
    assert "Base analysis: git checkout failed: checkout error" in caplog.text
    mock_restore.assert_called_once_with("original-sha", "", str(tmp_path), {})
    clean_mock.assert_called_once_with(str(tmp_path), {}, "original-sha")


@pytest.mark.parametrize("step", ["fetch", "rev-parse", "checkout"])
async def test_run_base_analysis_raises_on_git_timeout(tmp_xdg, tmp_path, caplog, step, clean_mock):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="old-sha")
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    seen = []
    raised = []

    def fake_run_cmd(cmd, **kwargs):
        seen.append(cmd[1])
        if cmd[1] == step:
            raised.append(subprocess.TimeoutExpired(cmd, 60))
            raise raised[0]
        return MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")

    with (
        patch("dotharness.pr_review.runners.review_prs.run_cmd", side_effect=fake_run_cmd),
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="original-sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore") as mock_restore,
        caplog.at_level("WARNING"),
        pytest.raises(review_prs.BaseAnalysisError) as info,
    ):
        await review_prs._run_base_analysis(cfg, {}, "main", "host default")
    assert info.value.__cause__ is raised[0]
    assert not isinstance(info.value, review_prs.FatalGitError)
    order = ["fetch", "rev-parse", "checkout"]
    assert seen == order[: order.index(step) + 1]
    records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(records) == 1
    assert records[0].getMessage() == f"Base analysis: git {step} timed out after 60s"
    assert not records[0].exc_info
    assert state.read_vibe_heal_state("acme-frontend")["last_main_sha"] == "old-sha"
    if step == "checkout":
        mock_restore.assert_called_once_with("original-sha", "", str(tmp_path), {})
        clean_mock.assert_called_once_with(str(tmp_path), {}, "original-sha")
    else:
        mock_restore.assert_not_called()
        clean_mock.assert_called_once_with(str(tmp_path), {})


async def test_run_locked_propagates_base_analysis_timeout_without_processing_prs(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])

    def fake_run_cmd(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60)

    host = _seed([_pr(1)])
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(host)),
        patch("dotharness.pr_review.runners.review_prs.prune_projects.run"),
        patch("dotharness.pr_review.runners.review_prs.run_cmd", side_effect=fake_run_cmd),
        pytest.raises(review_prs.BaseAnalysisError),
    ):
        await review_prs._run_locked(cfg)
    assert [c.operation for c in host.calls if "default_branch" not in c.operation] == []  # base resolution reads it


async def test_run_locked_calls_prune_projects_before_base_analysis(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    calls = []
    host = _seed([_pr(1)])
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(host)),
        patch(
            "dotharness.pr_review.runners.review_prs.prune_projects.run", side_effect=lambda *a: calls.append("prune")
        ),
        patch(
            "dotharness.pr_review.runners.review_prs._run_base_analysis",
            side_effect=lambda *a: calls.append("base_analysis") or False,
        ),
    ):
        await review_prs._run_locked(cfg)
    assert calls == ["prune", "base_analysis"]
    assert [c.operation for c in host.calls if "default_branch" not in c.operation] == []  # base resolution reads it


async def test_run_locked_does_not_prune_when_disabled(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    assert cfg.vibe_heal.prune_projects_enabled is False
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold()),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
    ):
        await review_prs._run_locked(cfg)
    prune_calls = [c for c in mock_run.call_args_list if "prune-projects" in str(c)]
    assert prune_calls == []


async def test_run_locked_skips_when_subdirs_empty(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path)
    with patch("dotharness.pr_review.runners.review_prs.open_host") as mock_token:
        await review_prs._run_locked(cfg)
    mock_token.assert_not_called()


async def test_run_locked_skips_pr_processing_when_base_analysis_fails(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    host = _seed([_pr(1)])
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(host)),
        patch("dotharness.pr_review.runners.review_prs.prune_projects.run"),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=False),
    ):
        await review_prs._run_locked(cfg)
    assert [c.operation for c in host.calls if "default_branch" not in c.operation] == []  # base resolution reads it


async def test_run_locked_does_not_record_reviewed_sha_on_subdir_failure(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    prs = [_pr(7, "feat", "sha7")]

    def fake_run_cmd(cmd, **kwargs):
        if "vibe_heal" in str(cmd) and "--pr" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return MagicMock(returncode=0, stdout=b"[]", stderr=b"")

    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd", side_effect=fake_run_cmd),
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        await review_prs._run_locked(cfg)
    assert state.get_reviewed_sha("acme-frontend", 7) is None


async def test_run_locked_does_not_record_reviewed_sha_on_generic_exception(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    prs = [_pr(7, "feat", "sha7")]

    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs._process_pr", side_effect=ValueError("boom")),
        patch(
            "dotharness.pr_review.runners.review_prs.run_cmd",
            return_value=MagicMock(returncode=0, stdout=b"[]", stderr=b""),
        ),
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        await review_prs._run_locked(cfg)
    assert state.get_reviewed_sha("acme-frontend", 7) is None


async def test_run_locked_returns_early_when_vibe_heal_disabled(tmp_path):
    from dotharness.pr_review.config import HarnessConfig, HarnessSection, RepoConfig, VibehealConfig

    cfg = HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/frontend", working_dir=tmp_path),
        vibe_heal=VibehealConfig(enabled=False),
    )
    with patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run:
        await review_prs._run_locked(cfg)
    mock_run.assert_not_called()


def _loop_patches(prs, process_side_effect):
    return (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs._process_pr", side_effect=process_side_effect),
        patch("dotharness.pr_review.runners.review_prs.run_cmd", return_value=MagicMock(returncode=0, stdout=b"[]")),
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
    )


async def test_identified_git_failure_skips_pr_and_continues(tmp_xdg, tmp_path, clean_mock, dirty_mock, caplog):
    from dotharness.pr_review.runners.common import FatalGitError

    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    prs = [_pr(1, "pr1", "sha1"), _pr(2, "pr2", "sha2"), _pr(3, "pr3", "sha3")]
    cause = subprocess.CalledProcessError(128, ["git", "checkout"], stderr=b"fatal: bad ref\nsecond line")
    exc = FatalGitError("git checkout pr2 failed")
    exc.__cause__ = cause
    p = _loop_patches(prs, [True, exc, True])
    with p[0], p[1], p[2] as mock_process, p[3], p[4], p[5] as mock_restore, caplog.at_level(logging.ERROR):
        failed = await review_prs._run_locked(cfg)
    assert failed == [2]
    assert mock_process.call_count == 3
    assert mock_restore.call_count == 3
    clean_mock.assert_called_once_with(str(tmp_path), ANY, "sha")
    dirty_mock.assert_called_once_with(str(tmp_path), ANY)
    assert "PR #2 skipped: git failure: fatal: bad ref" in caplog.text
    assert "second line" not in caplog.text.split("PR #2 skipped: git failure:")[1].splitlines()[0]
    assert state.get_reviewed_sha("acme-frontend", 1) == "sha1"
    assert state.get_reviewed_sha("acme-frontend", 2) is None
    assert state.get_reviewed_sha("acme-frontend", 3) == "sha3"


async def test_git_failure_log_uses_exception_text_when_stderr_empty(tmp_xdg, tmp_path, caplog):
    from dotharness.pr_review.runners.common import FatalGitError

    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    p = _loop_patches([_pr(1, "pr1", "sha1")], [FatalGitError("git checkout timed out")])
    with p[0], p[1], p[2], p[3], p[4], p[5], caplog.at_level(logging.ERROR):
        await review_prs._run_locked(cfg)
    assert "PR #1 skipped: git failure: git checkout timed out" in caplog.text


@pytest.mark.parametrize(
    ("cleaned", "dirty"),
    [(False, False), (True, True), (True, None)],
    ids=["helper-false", "probe-dirty", "probe-failed"],
)
async def test_unidentified_git_failure_stops_cycle(tmp_xdg, tmp_path, clean_mock, dirty_mock, cleaned, dirty, caplog):
    from dotharness.pr_review.runners.common import FatalGitError

    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    clean_mock.return_value = cleaned
    dirty_mock.return_value = dirty
    p = _loop_patches([_pr(5, "pr5", "sha5"), _pr(6, "pr6", "sha6")], [FatalGitError("git broke"), True])
    with p[0], p[1], p[2] as mock_process, p[3], p[4], p[5], caplog.at_level(logging.ERROR):
        failed = await review_prs._run_locked(cfg)
    assert mock_process.call_count == 1
    assert failed == [5]
    assert "PR #5 skipped: git failure: git broke" in caplog.text


async def test_run_raises_after_skipped_pr_with_later_success(tmp_xdg, tmp_path):
    from dotharness.pr_review.runners.common import FatalGitError

    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    p = _loop_patches([_pr(1, "pr1", "sha1"), _pr(2, "pr2", "sha2")], [FatalGitError("x"), True])
    with (
        p[0],
        p[1],
        p[2],
        p[3],
        p[4],
        p[5],
        patch("dotharness.pr_review.runners.review_prs.acquire_lock"),
        pytest.raises(review_prs.PrGitFailureError, match="#1"),
    ):
        await review_prs.run(cfg)
    assert state.get_reviewed_sha("acme-frontend", 2) == "sha2"


async def test_run_raises_in_single_pr_mode_on_skipped_pr(tmp_xdg, tmp_path):
    from dotharness.pr_review.runners.common import FatalGitError

    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    p = _loop_patches([_pr(1, "pr1", "sha1")], [FatalGitError("x")])
    with (
        p[0],
        p[1],
        p[2],
        p[3],
        p[4],
        p[5],
        patch("dotharness.pr_review.runners.review_prs.acquire_lock"),
        pytest.raises(review_prs.PrGitFailureError),
    ):
        await review_prs.run(cfg, pr_url="https://github.com/acme/acme-frontend/pull/1")


async def test_run_returns_normally_when_no_pr_skipped(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    p = _loop_patches([_pr(1, "pr1", "sha1")], [True])
    with p[0], p[1], p[2], p[3], p[4], p[5], patch("dotharness.pr_review.runners.review_prs.acquire_lock"):
        await review_prs.run(cfg)


async def test_base_analysis_failure_skips_no_prs_and_is_not_per_pr(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    host = _seed([_pr(1)])
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(host)),
        patch("dotharness.pr_review.runners.review_prs.prune_projects.run"),
        patch(
            "dotharness.pr_review.runners.review_prs._run_base_analysis",
            side_effect=review_prs.BaseAnalysisError("fetch", "failed"),
        ),
        patch("dotharness.pr_review.runners.review_prs._process_pr") as mock_process,
        pytest.raises(review_prs.BaseAnalysisError, match="git fetch failed"),
    ):
        await review_prs._run_locked(cfg)
    mock_process.assert_not_called()


async def test_fatal_git_error_from_fetch_runs_clean_then_probe_and_continues(tmp_xdg, tmp_path, clean_mock):
    from dotharness.pr_review.runners.common import FatalGitError

    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    prs = [_pr(3, "pr3", "sha3"), _pr(4, "pr4", "sha4"), _pr(7, "pr7", "sha7")]
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd", return_value=MagicMock(returncode=0, stdout=b"[]")),
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore") as mock_restore,
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout") as mock_fetch,
    ):
        mock_fetch.side_effect = [None, FatalGitError("corrupted ref"), None]
        failed = await review_prs._run_locked(cfg)
    assert failed == [4]
    assert mock_fetch.call_count == 3
    assert state.get_reviewed_sha("acme-frontend", 3) == "sha3"
    assert state.get_reviewed_sha("acme-frontend", 4) is None
    assert state.get_reviewed_sha("acme-frontend", 7) == "sha7"
    assert mock_restore.call_count == 3
    clean_mock.assert_called_once_with(str(tmp_path), ANY, "sha")


async def test_generic_exception_does_not_block_credit_for_other_prs(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    prs = [
        _pr(2, "pr2", "sha2"),
        _pr(4, "pr4", "sha4"),
    ]
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd", return_value=MagicMock(returncode=0, stdout=b"[]")),
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore") as mock_restore,
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout") as mock_fetch,
    ):
        mock_fetch.side_effect = [None, RuntimeError("unexpected failure")]
        await review_prs._run_locked(cfg)
    # Both PRs were processed; generic Exception on PR 4 did not break the loop.
    assert mock_fetch.call_count == 2
    assert mock_restore.call_count == 2
    # PR 2 succeeded and is credited immediately — PR 4's later failure doesn't retroactively
    # withhold that credit (this is the fix: no more batch-wide all-or-nothing gating).
    assert state.get_reviewed_sha("acme-frontend", 2) == "sha2"
    assert state.get_reviewed_sha("acme-frontend", 4) is None


async def test_run_locked_calls_git_restore_when_process_pr_raises(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    prs = [_pr(5, "feat", "sha5")]

    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs._process_pr", side_effect=RuntimeError("boom")),
        patch("dotharness.pr_review.runners.review_prs.run_cmd", return_value=MagicMock(returncode=0, stdout=b"[]")),
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="original-sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore") as mock_restore,
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        await review_prs._run_locked(cfg)
    mock_restore.assert_called_once()
    call_args = mock_restore.call_args
    assert call_args[0][0] == "original-sha"
    assert call_args[0][1] == "feat"
    assert call_args[0][2] == str(tmp_path)


# --- per-subdir env_file (A.2) ---


def _vibe_cmds(mock_run):
    return [c.args[0] for c in mock_run.call_args_list if "dotharness.vibe_heal" in c.args[0]]


def test_resolve_env_file_none_when_unset(tmp_path):
    from dotharness.pr_review.runners.common import resolve_env_file

    cfg = _make_config(tmp_path)
    assert resolve_env_file(cfg.repo, SubDir(path=".")) is None


def test_resolve_env_file_absolute_returned_unchanged(tmp_path):
    from dotharness.pr_review.runners.common import resolve_env_file

    cfg = _make_config(tmp_path)
    outside = tmp_path.parent / "elsewhere" / ".env"
    assert resolve_env_file(cfg.repo, SubDir(path="a", env_file=str(outside))) == outside


def test_resolve_env_file_relative_resolves_against_working_dir_not_cwd(tmp_path, monkeypatch):
    from dotharness.pr_review.runners.common import resolve_env_file

    cfg = _make_config(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    resolved = resolve_env_file(cfg.repo, SubDir(path="a", env_file="envs/.env"))
    assert resolved == tmp_path / "envs" / ".env"
    assert resolved is not None
    assert resolved.is_absolute()


def test_resolve_env_file_relative_working_dir_yields_absolute(tmp_path, monkeypatch):
    from pathlib import Path

    from dotharness.pr_review.config import RepoConfig
    from dotharness.pr_review.runners.common import resolve_env_file

    monkeypatch.chdir(tmp_path)
    repo = RepoConfig(name="acme/frontend", working_dir=Path("rel/repo"))
    resolved = resolve_env_file(repo, SubDir(path=".", env_file="x.env"))
    assert resolved is not None
    assert resolved.is_absolute()
    assert resolved == tmp_path / "rel" / "repo" / "x.env"


def test_resolve_env_file_tilde_expanded_outside_working_dir_no_containment_error(tmp_path):
    from pathlib import Path

    from dotharness.pr_review.runners.common import resolve_env_file

    cfg = _make_config(tmp_path)
    stored = str(Path("~/secrets/.env").expanduser())
    assert resolve_env_file(cfg.repo, SubDir(path="a", env_file=stored)) == Path(stored)


def test_env_file_args(tmp_path):
    from dotharness.pr_review.runners.common import env_file_args

    cfg = _make_config(tmp_path)
    assert env_file_args(cfg.repo, SubDir(path=".")) == []
    p = tmp_path / ".env"
    assert env_file_args(cfg.repo, SubDir(path=".", env_file=str(p))) == ["--env-file", str(p)]


async def test_base_subdir_argv_with_and_without_env_file(tmp_path):
    cfg = _make_config(tmp_path)
    (tmp_path / ".env").write_text("X=1\n")
    p = str(tmp_path / ".env")
    with patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        await review_prs._run_base_subdir(SubDir(path="a", env_file=p), cfg, {})
        await review_prs._run_base_subdir(SubDir(path="b"), cfg, {})
    with_env, without = _vibe_cmds(mock_run)
    base = ["/venv/bin/python3", "-m", "dotharness.vibe_heal", "review", "--baseline"]
    assert with_env == [*base, "--env-file", p]
    assert without == base


@pytest.mark.parametrize("coverage", [True, False])
async def test_process_subdir_argv_with_and_without_env_file(tmp_path, coverage):
    cfg = _make_config(tmp_path)
    (tmp_path / ".env").write_text("X=1\n")
    p = str(tmp_path / ".env")
    host = _seed([_pr(7)]).code_host()
    with patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        await review_prs._process_subdir(host, REPO, 7, SubDir(path="a", coverage=coverage, env_file=p), cfg, {})
        await review_prs._process_subdir(host, REPO, 7, SubDir(path="b", coverage=coverage), cfg, {})
    review_a, post_a, review_b, post_b = _vibe_cmds(mock_run)
    py = ["/venv/bin/python3", "-m", "dotharness.vibe_heal"]
    cov = ["--coverage"] if coverage else []
    assert review_a == [*py, "review", "--pr", "7", *cov, "--env-file", p]
    assert post_a == [*py, "review", "--post", "--pr", "7", "--env-file", p]
    assert review_b == [*py, "review", "--pr", "7", *cov]
    assert post_b == [*py, "review", "--post", "--pr", "7"]


# --- env_file existence check (A.3) ---


def _bad_env_file(tmp_path, kind):
    """Return a path that is not a regular file: missing, or a directory."""
    p = tmp_path / "envs" / ".env"
    if kind == "directory":
        p.mkdir(parents=True)
    return str(p)


@pytest.mark.parametrize("kind", ["missing", "directory"])
async def test_base_subdir_fails_fast_on_bad_env_file(tmp_path, caplog, kind):
    cfg = _make_config(tmp_path)
    bad = _bad_env_file(tmp_path, kind)
    subdir = SubDir(path="a", env_file=bad, pre_commands=[PreCommand(cmd="echo hi")])
    with patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run, caplog.at_level("DEBUG"):
        assert (await review_prs._run_base_subdir(subdir, cfg, {})) is False
    mock_run.assert_not_called()
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "a" in errors[0].getMessage() and bad in errors[0].getMessage()


@pytest.mark.parametrize("kind", ["missing", "directory"])
async def test_process_subdir_fails_fast_on_bad_env_file(tmp_path, caplog, kind):
    cfg = _make_config(tmp_path)
    bad = _bad_env_file(tmp_path, kind)
    subdir = SubDir(path="a", env_file=bad, pre_commands=[PreCommand(cmd="echo hi")])
    host = _seed([_pr(7)]).code_host()
    with patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run, caplog.at_level("DEBUG"):
        assert (await review_prs._process_subdir(host, REPO, 7, subdir, cfg, {})) is False
    mock_run.assert_not_called()
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "a" in errors[0].getMessage() and bad in errors[0].getMessage()


@pytest.mark.parametrize("kind", ["missing", "directory"])
async def test_run_base_analysis_does_not_persist_sha_on_bad_env_file(tmp_xdg, tmp_path, kind):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="old-sha")
    bad = _bad_env_file(tmp_path, kind)
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30, env_file=bad)])
    with (
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="original-sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")
        assert (await review_prs._run_base_analysis(cfg, {}, "main", "host default")) is False
    assert not [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert state.read_vibe_heal_state("acme-frontend")["last_main_sha"] == "old-sha"


@pytest.mark.parametrize("kind", ["missing", "directory"])
async def test_process_pr_does_not_record_reviewed_sha_on_bad_env_file(tmp_xdg, tmp_path, kind):
    bad = _bad_env_file(tmp_path, kind)
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30, env_file=bad)])
    prs = [_pr(7, "feat", "sha7")]
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed(prs))),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=True),
        patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run,
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"[]", stderr=b"")
        await review_prs._run_locked(cfg)
    assert not [c for c in mock_run.call_args_list if "vibe_heal" in str(c)]
    assert state.get_reviewed_sha("acme-frontend", 7) is None


async def test_unset_env_file_logs_no_env_file_message(tmp_path, caplog):
    cfg = _make_config(tmp_path)
    with patch("dotharness.pr_review.runners.review_prs.run_cmd") as mock_run, caplog.at_level("DEBUG"):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        assert (await review_prs._run_base_subdir(SubDir(path="a"), cfg, {})) is True
    assert not [r for r in caplog.records if "regular file" in r.getMessage()]


async def test_discover_prs_lists_open_matching_authors_sorted_and_own_drafts(tmp_path):
    cfg = _make_config(tmp_path, authors=["alice"])
    host = _seed([
        _pr(9, "b", author=User("alice")),
        _pr(2, "a", author=User("alice")),
        _pr(4, "d", author=User("alice"), is_draft=True),
        _pr(5, "e", author=User("eve")),
        _pr(6, "f", author=User("alice"), state=ChangeRequestState.CLOSED),
    ])
    found = await review_prs._discover_prs(cfg, None, host.code_host(), REPO, "alice")
    assert found is not None
    assert [pr.number for pr in found] == [2, 4, 9]
    assert [c.operation for c in host.calls] == ["ChangeRequests.list"]


async def test_discover_prs_returns_none_when_the_list_fails_and_empty_when_there_are_none(tmp_path):
    cfg = _make_config(tmp_path)
    host = _seed()
    assert await review_prs._discover_prs(cfg, None, host.code_host(), REPO, "alice") == []
    host.fail_next("ChangeRequests.list", CodeHostError("boom"))
    assert await review_prs._discover_prs(cfg, None, host.code_host(), REPO, "alice") is None


async def test_discover_prs_timeout_on_list_is_a_failed_read(tmp_path):
    host = _seed([_pr(1)])
    host.fail_next("ChangeRequests.list", CodeHostTimeoutError("slow", write=False))
    assert await review_prs._discover_prs(_make_config(tmp_path), None, host.code_host(), REPO, "alice") is None


async def test_discover_prs_with_url_reads_only_that_change_request(tmp_path):
    host = _seed([_pr(3, "feat", "sha3"), _pr(4)])
    found = await review_prs._discover_prs(
        _make_config(tmp_path), "https://example.test/acme/frontend/pull/3", host.code_host(), REPO, "alice"
    )
    assert found is not None
    assert [(pr.number, pr.head_revision, pr.base_reference) for pr in found] == [(3, "sha3", "main")]
    assert "ChangeRequests.list" not in [c.operation for c in host.calls]


async def test_discover_prs_stops_when_the_host_cannot_list_change_requests(tmp_path):
    host = InMemoryCodeHost(
        repository="acme/frontend", user="alice", capabilities=frozenset(Capability) - {Capability.CHANGE_REQUESTS}
    )
    with pytest.raises(UnsupportedCapabilityError):
        await review_prs._discover_prs(_make_config(tmp_path), None, host.code_host(), REPO, "alice")


async def test_process_pr_skips_when_the_state_read_fails(tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(path=".", pre_commands=[], coverage=False, timeout=30)])
    pr = _pr(42)
    host = _seed([pr])
    host.fail_next("ChangeRequests.state", CodeHostError("boom"))
    with patch("dotharness.pr_review.runners.review_prs.git_fetch_and_checkout") as mock_fetch:
        result = await review_prs._process_pr(pr, cfg, host.code_host(), REPO, {}, "alice", str(tmp_path))
    assert result is False
    mock_fetch.assert_not_called()


async def test_base_analysis_checkout_runs_pre_checkout_clean_before_checkout(
    tmp_xdg, tmp_path, pre_checkout_clean_mock
):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    state.write_vibe_heal_state("acme-frontend", last_main_sha="old-sha")
    seen = []

    def fake_run_cmd(cmd, **kwargs):
        seen.append(cmd[1])
        return MagicMock(returncode=0, stdout=b"new-sha\n", stderr=b"")

    pre_checkout_clean_mock.side_effect = lambda *a: seen.append("pre-clean")
    with (
        patch("dotharness.pr_review.runners.review_prs.run_cmd", side_effect=fake_run_cmd),
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="original-sha"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs._run_base_subdir", return_value=True),
    ):
        await review_prs._run_base_analysis(cfg, {}, "main", "host default")
    pre_checkout_clean_mock.assert_called_once_with(str(tmp_path), {})
    assert seen.index("pre-clean") < seen.index("checkout")


# --- resolve_base_branch (precedence chain) ---


class _StubRepository:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    async def default_branch(self, repo):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def _stub_scaffold(repository):
    return SimpleNamespace(host=SimpleNamespace(repository=repository), repo=REPO)


def _base_config(tmp_path, base_branch=None):
    return SimpleNamespace(repo=SimpleNamespace(base_branch=base_branch, working_dir=tmp_path))


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)  # noqa: S607


@pytest.fixture
def git_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i")
    return tmp_path


async def test_resolve_config_wins_without_host_or_git(tmp_path):
    repository = _StubRepository(error=AssertionError("host must not be called"))
    with patch("dotharness.pr_review.runners.review_prs.run_cmd", side_effect=AssertionError("no git")):
        result = await review_prs.resolve_base_branch(_base_config(tmp_path, "develop"), _stub_scaffold(repository))
    assert result == ("develop", "config")
    assert repository.calls == 0


async def test_resolve_host_default_read_once(tmp_path):
    repository = _StubRepository("trunk")
    result = await review_prs.resolve_base_branch(_base_config(tmp_path), _stub_scaffold(repository))
    assert result == ("trunk", "host default")
    assert repository.calls == 1


async def test_resolve_host_default_main_is_main(tmp_path):
    result = await review_prs.resolve_base_branch(_base_config(tmp_path), _stub_scaffold(_StubRepository("main")))
    assert result == ("main", "host default")


@pytest.mark.parametrize(
    "repository",
    [
        _StubRepository(error=CodeHostError("boom")),
        _StubRepository(error=UnsupportedCapabilityError("no", capability="Repository")),
        _StubRepository(""),
        _StubRepository("   "),
    ],
)
async def test_resolve_host_failure_falls_to_final_fallback(tmp_path, repository):
    result = await review_prs.resolve_base_branch(_base_config(tmp_path), _stub_scaffold(repository))
    assert result == ("main", "final fallback")
    assert repository.calls == 1


async def test_resolve_final_fallback(git_repo):
    _git(git_repo, "branch", "-M", "other")
    repository = _StubRepository(error=CodeHostError("down"))
    result = await review_prs.resolve_base_branch(_base_config(git_repo), _stub_scaffold(repository))
    assert result == ("main", "final fallback")


# --- base analysis on the resolved ref (composite gate, logging, errors) ---

SHA1, SHA2 = "1" * 40, "2" * 40


def _base_cmds(shas=SHA1, fail=None):
    """A fake run_cmd recording git commands; ``fail`` names a git step that exits 1."""
    seen = []

    def fake(cmd, **kwargs):
        seen.append(list(cmd))
        if fail and cmd[1] == fail:
            return MagicMock(returncode=1, stdout=b"", stderr=b"fatal: couldn't find remote ref")
        return MagicMock(returncode=0, stdout=f"{shas}\n".encode(), stderr=b"")

    return seen, fake


def _patches(fake):
    return (
        patch("dotharness.pr_review.runners.review_prs.run_cmd", side_effect=fake),
        patch("dotharness.pr_review.runners.review_prs.git_detach_and_record", return_value="orig"),
        patch("dotharness.pr_review.runners.review_prs.git_restore"),
        patch("dotharness.pr_review.runners.review_prs._run_base_subdir", return_value=True),
    )


async def _run_base(cfg, base, source, fake):
    p1, p2, p3, p4 = _patches(fake)
    with p1, p2, p3, p4 as subdir:
        result = await review_prs._run_base_analysis(cfg, {}, base, source)
    return result, subdir


async def test_base_analysis_uses_resolved_ref_in_git_calls(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    seen, fake = _base_cmds(SHA1)
    result, _ = await _run_base(cfg, "develop", "config", fake)
    assert result is True
    assert seen[0] == ["git", "fetch", "--recurse-submodules", "--", "origin", "develop"]
    assert seen[1] == ["git", "rev-parse", "origin/develop"]
    assert seen[2] == ["git", "checkout", "--recurse-submodules", "--detach", SHA1]
    assert state.read_vibe_heal_state("acme-frontend")["last_main_sha"] == f"develop@{SHA1}"


async def test_base_analysis_default_main_commands_unchanged(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    seen, fake = _base_cmds(SHA1)
    await _run_base(cfg, "main", "host default", fake)
    assert seen[0] == ["git", "fetch", "--recurse-submodules", "--", "origin", "main"]
    assert seen[1] == ["git", "rev-parse", "origin/main"]


async def test_base_analysis_absent_ref_names_ref_and_source(tmp_xdg, tmp_path, clean_mock):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    state.write_vibe_heal_state("acme-frontend", last_main_sha=f"develop@{SHA2}")
    _, fake = _base_cmds(fail="fetch")
    with _patches(fake)[0], pytest.raises(review_prs.BaseAnalysisError) as info:
        await review_prs._run_base_analysis(cfg, {}, "develop", "config")
    assert "origin/develop" in str(info.value)
    assert "config" in str(info.value)
    clean_mock.assert_called_once_with(str(tmp_path), {})
    assert state.read_vibe_heal_state("acme-frontend")["last_main_sha"] == f"develop@{SHA2}"


async def test_base_analysis_same_composite_is_a_hit(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    state.write_vibe_heal_state("acme-frontend", last_main_sha=f"develop@{SHA1}")
    seen, fake = _base_cmds(SHA1)
    result, subdir = await _run_base(cfg, "develop", "config", fake)
    assert result is True
    subdir.assert_not_called()
    assert not any(c[1] == "checkout" for c in seen)


async def test_base_analysis_new_sha_rebaselines(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    state.write_vibe_heal_state("acme-frontend", last_main_sha=f"develop@{SHA1}")
    _, fake = _base_cmds(SHA2)
    result, subdir = await _run_base(cfg, "develop", "config", fake)
    assert result is True
    subdir.assert_called_once()
    assert state.read_vibe_heal_state("acme-frontend")["last_main_sha"] == f"develop@{SHA2}"


async def test_base_analysis_legacy_bare_sha_misses_once_then_hits(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    state.write_vibe_heal_state("acme-frontend", last_main_sha=SHA1)
    _, fake = _base_cmds(SHA1)
    _, subdir = await _run_base(cfg, "main", "host default", fake)
    subdir.assert_called_once()
    _, subdir = await _run_base(cfg, "main", "host default", fake)
    subdir.assert_not_called()


async def test_base_analysis_other_ref_name_misses_at_same_sha(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    state.write_vibe_heal_state("acme-frontend", last_main_sha=f"main@{SHA1}")
    _, fake = _base_cmds(SHA1)
    _, subdir = await _run_base(cfg, "develop", "config", fake)
    subdir.assert_called_once()
    assert state.read_vibe_heal_state("acme-frontend")["last_main_sha"] == f"develop@{SHA1}"


@pytest.mark.parametrize("hit", [False, True])
async def test_base_analysis_logs_ref_and_source_before_git(tmp_xdg, tmp_path, caplog, hit):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    if hit:
        state.write_vibe_heal_state("acme-frontend", last_main_sha=f"develop@{SHA1}")
    _, base_fake = _base_cmds(SHA1)

    def fake(cmd, **kwargs):
        assert "origin/develop" in caplog.text
        assert "host default" in caplog.text
        return base_fake(cmd, **kwargs)

    with caplog.at_level("INFO"):
        await _run_base(cfg, "develop", "host default", fake)
    infos = [r.getMessage() for r in caplog.records if r.levelname == "INFO" and "origin/develop" in r.getMessage()]
    assert any("host default" in m for m in infos)


async def test_run_locked_passes_resolved_base_to_base_analysis(tmp_xdg, tmp_path):
    cfg = _make_config(tmp_path, subdirs=[SubDir(".", [], False, 30)])
    cfg.repo.base_branch = "develop"
    with (
        patch("dotharness.pr_review.runners.review_prs.open_host", return_value=make_scaffold(_seed())),
        patch("dotharness.pr_review.runners.review_prs.prune_projects.run"),
        patch("dotharness.pr_review.runners.review_prs._run_base_analysis", return_value=False) as mock_base,
    ):
        await review_prs._run_locked(cfg)
    mock_base.assert_called_once_with(cfg, ANY, "develop", "config")
