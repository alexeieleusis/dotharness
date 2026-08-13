import logging
import shlex
import subprocess

from harness import state
from harness.config import HarnessConfig
from harness.lock import acquire_lock
from harness.runners.common import (
    TIMEOUT_GIT,
    FatalGitError,
    add_reviewer,
    build_subprocess_env,
    get_changed_files,
    get_current_user,
    get_gh_token,
    get_requested_reviewers,
    git_detach_and_record,
    git_fetch_and_checkout,
    git_restore,
    is_pr_open,
    list_open_prs_matching_authors,
    pr_from_url,
    run_cmd,
)

logger = logging.getLogger(__name__)


def run(config: HarnessConfig, pr_url: str | None = None) -> None:
    with acquire_lock(config.repo_slug):
        _run_locked(config, pr_url)


def _discover_prs(config: HarnessConfig, pr_url: str | None, env: dict) -> list[dict] | None:
    if pr_url:
        pr = pr_from_url(pr_url, config.repo.name, env, "number,headRefName,baseRefName,headRefOid")
        return [pr] if pr else []

    return list_open_prs_matching_authors(config.repo.name, config.vibe_heal.authors, str(config.repo.working_dir), env)


def _already_reviewed(reviewed_shas: dict, pr: dict) -> bool:
    reviewed_sha = reviewed_shas.get(str(pr["number"]))
    if reviewed_sha is not None and reviewed_sha == pr.get("headRefOid"):
        logger.info("PR #%d: head unchanged since last review (%.8s), skipping", pr["number"], reviewed_sha)
        return True
    return False


def _run_locked(config: HarnessConfig, pr_url: str | None = None) -> None:
    if not config.vibe_heal.enabled:
        logger.info("vibe_heal disabled, skipping")
        return

    if not config.repo.subdirs:
        logger.error("vibe_heal enabled but repo.subdirs is empty; add a [[repo.subdir]] entry to .harness.toml")
        return

    gh_token = get_gh_token(config.harness.gh_token_cmd)
    env = build_subprocess_env(config.harness.path_prepend, config.harness.env, gh_token)

    if not _run_base_analysis(config, env):
        logger.warning("Base analysis failed; skipping PR review for this run")
        return

    current_user = get_current_user(env)

    candidates = _discover_prs(config, pr_url, env)

    if pr_url is not None:
        to_process = candidates
    else:
        if candidates is None:
            logger.warning("Failed to fetch open PRs; skipping this cycle without touching reviewed_shas")
            return

        # Authoritative "still open" set for this batch — prune closed/merged PRs out of
        # reviewed_shas even on a cycle that finds nothing new to process, so stale entries
        # don't linger just because no fresh PR number showed up.
        state.prune_reviewed_shas(config.repo_slug, {p["number"] for p in candidates})
        reviewed_shas = state.read_vibe_heal_state(config.repo_slug)["reviewed_shas"]
        to_process = [pr for pr in candidates if not _already_reviewed(reviewed_shas, pr)]

    if not to_process:
        return

    wdir = str(config.repo.working_dir)
    original_sha = git_detach_and_record(wdir, env)

    # Each PR's success is recorded the moment it happens (via record_reviewed_sha below),
    # independent of every other PR in this batch — so one perpetually-failing PR (e.g. a
    # vibe_heal review that hangs) never withholds credit from PRs that already succeeded.
    # A FatalGitError still breaks the loop immediately; PRs processed before it keep their
    # credit regardless.
    for pr in to_process:
        success, fatal = _process_pr_safely(pr, config, env, current_user, wdir, original_sha)
        if fatal:
            break

        if pr_url is None and success:
            state.record_reviewed_sha(config.repo_slug, pr["number"], pr["headRefOid"])


def _process_pr_safely(
    pr: dict, config: HarnessConfig, env: dict, current_user: str, wdir: str, original_sha: str
) -> tuple[bool, bool]:
    """Run _process_pr for one PR, always restoring HEAD afterward. Returns (success, fatal)."""
    logger.info("PR #%d: starting", pr["number"])
    success = False
    fatal = False
    try:
        success = _process_pr(pr, config, env, current_user, wdir)
    except FatalGitError:
        logger.exception("PR #%d: fatal git error", pr["number"])
        fatal = True
    except Exception:
        logger.exception("PR #%d: error", pr["number"])
    finally:
        git_restore(original_sha, pr["headRefName"], wdir, env)
    return success, fatal


def _process_pr(pr: dict, config: HarnessConfig, env: dict, current_user: str, wdir: str) -> bool:
    if not is_pr_open(pr["number"], config.repo.name, env):
        logger.info("PR #%d: closed or merged since discovery, skipping", pr["number"])
        return False

    git_fetch_and_checkout(pr["headRefName"], wdir, env)
    was_requested = current_user in get_requested_reviewers(pr["number"], config.repo.name, env)
    results: list[bool] = []
    changed_files = _get_changed_files_for_pr(pr, config, wdir, env)
    for subdir in config.repo.subdirs:
        if not _subdir_has_changes(subdir.path, changed_files):
            logger.info("PR #%d: skipping subdir %s, no changes", pr["number"], subdir.path)
            continue
        results.append(_process_subdir(pr["number"], subdir, config, env))
    if results and was_requested:
        # Submitting a review via the GitHub API clears the submitter from the PR's
        # requested-reviewers list, which would hide this PR from review_requested's
        # "user-review-requested:@me" search for the rest of this run_all cycle.
        # We re-add whenever at least one subdir was processed, since we cannot
        # observe from here whether the subprocess actually posted any comments.
        add_reviewer(pr["number"], config.repo.name, current_user, env)
    return all(results) if results else True


def _is_root_subdir(subdir_path: str) -> bool:
    return subdir_path in (".", "")


def _get_changed_files_for_pr(pr: dict, config: HarnessConfig, wdir: str, env: dict) -> list[str]:
    if all(_is_root_subdir(s.path) for s in config.repo.subdirs):
        return []
    return get_changed_files(pr["baseRefName"], wdir, env)


def _log_called_process_output(e: subprocess.CalledProcessError, level: str) -> None:
    log_func = logger.exception if level == "exception" else logger.warning
    if e.stdout:
        log_func("%s stdout: %s", level, e.stdout.decode("utf-8", errors="replace"))
    if e.stderr:
        log_func("%s stderr: %s", level, e.stderr.decode("utf-8", errors="replace"))


def _subdir_has_changes(subdir_path: str, changed_files: list[str]) -> bool:
    if _is_root_subdir(subdir_path):
        return True
    prefix = subdir_path.rstrip("/") + "/"
    return any(f.startswith(prefix) for f in changed_files)


def _run_pre_commands(subdir, config: HarnessConfig, env: dict) -> bool:
    for pre_command in subdir.pre_commands:
        cmd_str = pre_command.cmd
        logger.info("Running pre-command: %s", cmd_str)
        try:
            has_shell_ops = any(op in cmd_str for op in (">", "|", "&&", ";", "<", "`", "$("))
            cmd = cmd_str if has_shell_ops else shlex.split(cmd_str)
            run_cmd(cmd, cwd=str(config.repo.working_dir), env=env, timeout=subdir.timeout, shell=has_shell_ops)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning("Pre-command failed '%s': %s", cmd_str, e)
            if isinstance(e, subprocess.CalledProcessError):
                _log_called_process_output(e, "Pre-command")
            if pre_command.critical:
                return False
    return True


def _run_vh_command(cmd: list, label: str, cwd: str, timeout: int, env: dict) -> bool:
    try:
        run_cmd(cmd, cwd=cwd, env=env, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.exception("%s failed", label)
        if isinstance(e, subprocess.CalledProcessError):
            _log_called_process_output(e, label)
        return False
    return True


def _process_subdir(pr_number: int, subdir, config: HarnessConfig, env: dict) -> bool:
    subdir_path = str(config.repo.working_dir / subdir.path)
    if not _run_pre_commands(subdir, config, env):
        return False

    vh_cmd = [config.vibe_heal.python, "-m", "vibe_heal", "review", "--pr", str(pr_number)]
    if subdir.coverage:
        vh_cmd.append("--coverage")
    logger.info("Running vibe_heal review in %s for PR #%d", subdir_path, pr_number)
    if not _run_vh_command(vh_cmd, "vibe_heal review", subdir_path, config.vibe_heal.vibe_heal_timeout, env):
        return False

    if not is_pr_open(pr_number, config.repo.name, env):
        logger.info("PR #%d: closed or merged during review, skipping post", pr_number)
        return False

    vh_post = [config.vibe_heal.python, "-m", "vibe_heal", "review", "--post", "--pr", str(pr_number)]
    logger.info("Running vibe_heal review --post in %s for PR #%d", subdir_path, pr_number)
    return _run_vh_command(
        vh_post, "vibe_heal review --post", subdir_path, config.vibe_heal.vibe_heal_post_timeout, env
    )


def _run_base_subdir(subdir, config: HarnessConfig, env: dict) -> bool:
    if not _run_pre_commands(subdir, config, env):
        return False

    subdir_path = str(config.repo.working_dir / subdir.path)
    vh_cmd = [config.vibe_heal.python, "-m", "vibe_heal", "review", "--baseline"]
    logger.info("Running vibe_heal base analysis in %s", subdir_path)
    return _run_vh_command(vh_cmd, "vibe_heal base analysis", subdir_path, config.vibe_heal.vibe_heal_timeout, env)


def _run_base_analysis(config: HarnessConfig, env: dict) -> bool:
    """Analyze origin/main once per new SHA (gated on state.last_main_sha) so vibe_heal has
    a baseline to diff PRs against. Always restores the working dir to where it started,
    regardless of outcome."""
    wdir = str(config.repo.working_dir)

    result = run_cmd(
        ["git", "fetch", "--recurse-submodules", "origin", "main"], cwd=wdir, env=env, timeout=TIMEOUT_GIT, check=False
    )
    if result.returncode != 0:
        logger.warning("Base analysis: git fetch failed: %s", result.stderr.decode("utf-8", errors="replace"))
        return False

    result = run_cmd(["git", "rev-parse", "origin/main"], cwd=wdir, env=env, timeout=TIMEOUT_GIT, check=False)
    if result.returncode != 0:
        logger.warning("Base analysis: git rev-parse failed: %s", result.stderr.decode("utf-8", errors="replace"))
        return False
    main_sha = result.stdout.decode("utf-8").strip()

    vh_state = state.read_vibe_heal_state(config.repo_slug)
    if vh_state["last_main_sha"] == main_sha:
        logger.info("Base analysis: already current at %s", main_sha[:8])
        return True

    original_sha: str | None = None
    try:
        original_sha = git_detach_and_record(wdir, env)

        result = run_cmd(
            ["git", "checkout", "--recurse-submodules", "--detach", main_sha],
            cwd=wdir,
            env=env,
            timeout=TIMEOUT_GIT,
            check=False,
        )
        if result.returncode != 0:
            logger.warning("Base analysis: checkout failed: %s", result.stderr.decode("utf-8", errors="replace"))
            return False

        success = all(_run_base_subdir(s, config, env) for s in config.repo.subdirs)
        if success:
            state.write_vibe_heal_state(config.repo_slug, last_main_sha=main_sha)
    except FatalGitError:
        logger.exception("Base analysis: fatal git error")
        return False
    else:
        return success
    finally:
        if original_sha is not None:
            git_restore(original_sha, "", wdir, env)
