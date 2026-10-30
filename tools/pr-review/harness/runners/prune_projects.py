import logging
import subprocess

from harness.config import HarnessConfig, SubDir
from harness.runners.common import env_file_args, log_called_process_output, resolve_env_file, run_cmd

logger = logging.getLogger(__name__)


def run(config: HarnessConfig, env: dict) -> None:
    """Delete stale vibe_heal temp SonarQube projects in every configured subdir.

    A subdir's SonarQube project key comes from its own .env.vibeheal file, so this
    runs `vibe_heal prune-projects` once per subdir rather than once per repo. Disabled
    by default (`vibe_heal.prune_projects_enabled`) since it deletes projects with
    `--yes`, skipping the CLI's own confirmation prompt. A failure in one subdir is
    logged and never raised, so it can't block the base analysis or PR review that
    follow it in review-prs.
    """
    if not config.vibe_heal.prune_projects_enabled:
        return

    for subdir in config.repo.subdirs:
        _prune_subdir(subdir, config, env)


def env_file_error(config: HarnessConfig, subdir: SubDir) -> str | None:
    """Return an error message when the subdir's configured env_file is not an existing regular file.

    None when env_file is unset or the file exists. Only the path is checked; the file is never opened.
    """
    resolved = resolve_env_file(config.repo, subdir)
    if resolved is None or resolved.is_file():
        return None
    return f"env_file for subdir {subdir.path} is not an existing regular file: {resolved}"


def _prune_subdir(subdir: SubDir, config: HarnessConfig, env: dict) -> None:
    subdir_path = str(config.repo.working_dir / subdir.path)
    env_error = env_file_error(config, subdir)
    if env_error:
        logger.warning("vibe_heal prune-projects skipped: %s", env_error)
        return
    cmd = [
        config.vibe_heal.python,
        "-m",
        "vibe_heal",
        "prune-projects",
        "--yes",
        "--older-than",
        str(config.vibe_heal.prune_older_than_minutes),
    ]
    cmd += env_file_args(config.repo, subdir)
    logger.info("Running vibe_heal prune-projects in %s", subdir_path)
    try:
        run_cmd(cmd, cwd=subdir_path, env=env, timeout=config.vibe_heal.prune_projects_timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        logger.warning("vibe_heal prune-projects failed in %s: %s", subdir_path, e)
        if isinstance(e, subprocess.CalledProcessError):
            log_called_process_output(e, logger.warning, "vibe_heal prune-projects")
