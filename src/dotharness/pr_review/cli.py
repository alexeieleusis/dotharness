import asyncio
import enum
import logging
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from dotharness.cli_common import fail
from dotharness.code_host.errors import ConfigurationError
from dotharness.code_host.factory import HostSelection, create_code_host
from dotharness.code_host.interfaces import CodeHost
from dotharness.paths import data_dir
from dotharness.pr_review import state as state_mod
from dotharness.pr_review.config import HarnessConfig, load_config
from dotharness.pr_review.runners import (
    address_comments,
    auto_approve,
    ci_fix,
    focused_review,
    local_review,
    merge,
    review_prs,
    review_requested,
    self_review,
)

TEMPLATE = """\
# dotharness configuration — DO NOT COMMIT this file
# Add .harness.toml to your global gitignore

[harness]
backend = "opencode"               # "opencode" or "claude"
gh_token_cmd = "gh auth token"
# host_token_cmd = "gh auth token"  # host-neutral name; wins over gh_token_cmd when both are set
# host = "reference"                # code host; absent means "reference"
backend_timeout_seconds = 900

# [harness.host_options]
# (options passed to the selected host)

# [harness.path_prepend]
# java = "/Users/you/.sdkman/candidates/java/current/bin"
# node = "/Users/you/.nvm/versions/node/v22.20.0/bin"

# [harness.env]
# JAVA_HOME = "/Users/you/.sdkman/candidates/java/current"

# review_knowledge_file = "/path/to/review-guide.md"

[repo]
name = "org/repo"
working_dir = "/path/to/repo"

[vibe_heal]
enabled = false
# python = "/path/to/vibe-heal/.venv/bin/python3"
# authors = "*"
# vibe_heal_timeout = 600
# vibe_heal_post_timeout = 120

[focused_review]
enabled = false
# vibe_types_repo = "~/.harness/vendor/vibe-types"

[address_comments]
# enabled = true
# trusted_commenters = "*"

# [auto_approve]
# enabled = false
# viewed_max_lines = 75
# approve_max_lines = 500

# [[repo.subdir]]
# path = "."
# pre_commands = []
# coverage = false
# timeout = 300
# env_file = "~/secrets/api.env.vibeheal"  # optional vibe-heal env file; may live outside the repo
"""

HARNESS_CONFIG = ".harness.toml"
XDG_LOGS = data_dir() / "logs"
RUNNER_FAILED_MSG = "Runner %s failed: %s"


def _setup_logging(command: str, verbose: bool = False) -> None:
    log_dir = XDG_LOGS / command
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{date.today().isoformat()}.log"
    handlers: list[logging.Handler] = [logging.FileHandler(log_file)]
    if sys.stdout.isatty() or verbose:
        handlers.append(logging.StreamHandler(sys.stdout))
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, handlers=handlers, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


app = typer.Typer(
    name="pr_review", add_completion=False, no_args_is_help=True, rich_markup_mode=None, pretty_exceptions_enable=False
)
cli = app  # alias kept for dotharness.cli (``from dotharness.pr_review.cli import cli as pr_review``)

run_app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
state_app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
schedule_app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)
app.add_typer(run_app, name="run")
app.add_typer(state_app, name="state")
app.add_typer(schedule_app, name="schedule")


class Scheduler(enum.StrEnum):
    cron = "cron"
    launchd = "launchd"


@app.command("init")
def cmd_init(directory: Annotated[str, typer.Argument(metavar="DIRECTORY")] = ".") -> None:
    """Write a .harness.toml template to DIRECTORY (default: current directory)."""
    p = Path(directory) / HARNESS_CONFIG
    if p.exists():
        fail(f"{HARNESS_CONFIG} already exists in {Path(directory).resolve()}")
    p.write_text(TEMPLATE)
    typer.echo(f"Created {p.resolve()}")


def _build_host(cfg: HarnessConfig) -> CodeHost:
    """Build the configured code host; raises ConfigurationError on bad host config."""
    selection = HostSelection(
        setting="host",
        name=cfg.harness.host,
        options=cfg.harness.host_options,
        token_cmd=cfg.harness.effective_token_cmd,
        working_directory=cfg.repo.working_dir if cfg.repo.working_dir.is_dir() else None,
    )
    return create_code_host(selection)


def _create_host(cfg: HarnessConfig) -> CodeHost:
    """Build the configured code host or stop with the factory's message (exit code 1, like other config errors)."""
    try:
        return _build_host(cfg)
    except ConfigurationError as e:
        typer.echo(str(e), err=True)
        sys.exit(1)


def _check_host(cfg: HarnessConfig) -> str | None:
    """Return a validation error for the configured host, or None when it is usable."""
    host_name = cfg.harness.host or "reference"
    try:
        host = _build_host(cfg)
    except ConfigurationError as e:
        return str(e)
    try:
        available = asyncio.run(host.session.available())
    except Exception as e:
        return f"harness.host: availability check for host '{host_name}' failed: {e}"
    return None if available else f"harness.host: host '{host_name}' is not available"


@app.command("validate")
def cmd_validate(
    directory: Annotated[str, typer.Argument(metavar="DIRECTORY")] = ".",
    config_path: Annotated[Path | None, typer.Option("--config", metavar="PATH")] = None,
) -> None:
    """Validate .harness.toml in DIRECTORY (default: current directory)."""
    if config_path is None:
        config_path = Path(directory) / HARNESS_CONFIG
    errors = []
    try:
        cfg = load_config(Path(config_path))
    except Exception as e:
        typer.echo(str(e), err=True)
        sys.exit(1)

    if not cfg.repo.working_dir.exists():
        errors.append(f"repo.working_dir: path does not exist: {cfg.repo.working_dir}")
    if not cfg.harness.knowledge_dir.exists():
        errors.append(f"harness.knowledge_dir: path does not exist: {cfg.harness.knowledge_dir}")
    backend_bin = "opencode" if cfg.harness.backend == "opencode" else "claude"
    if not shutil.which(backend_bin):
        errors.append(f"harness.backend: binary '{backend_bin}' not found on PATH")
    host_error = _check_host(cfg)
    if host_error:
        errors.append(host_error)
    if cfg.vibe_heal.enabled and cfg.vibe_heal.python and not Path(cfg.vibe_heal.python).expanduser().exists():
        errors.append(f"vibe_heal.python: path does not exist: {cfg.vibe_heal.python}")

    if errors:
        for e in errors:
            typer.echo(e, err=True)
        sys.exit(1)
    typer.echo(f"Config valid: {cfg.repo.name} backend={cfg.harness.backend}")
    sys.exit(0)


@run_app.callback()
def cmd_run(
    ctx: typer.Context,
    config_path: Annotated[Path | None, typer.Option("--config", metavar="PATH", is_eager=True)] = None,
    verbose: Annotated[bool, typer.Option("--verbose", help="Enable DEBUG-level logging.")] = False,
) -> None:
    ctx.ensure_object(dict)
    if config_path is None:
        config_path = Path(".") / HARNESS_CONFIG
    ctx.obj["config_path"] = Path(config_path).resolve()
    ctx.obj["verbose"] = verbose


@run_app.command("review-prs")
def run_review_prs(
    ctx: typer.Context,
    pr_url: Annotated[
        str | None,
        typer.Option(
            "--pr",
            metavar="TEXT",
            help=(
                "Review a single PR URL instead of discovering open PRs automatically. "
                "Bypasses state entirely: the per-PR reviewed-SHA record is neither read nor "
                "updated, so this PR can be re-run any number of times without affecting future "
                "review-prs runs."
            ),
        ),
    ] = None,
) -> None:
    """Run vibe_heal static analysis review over open PRs (or a single PR with --pr)."""
    _setup_logging("review-prs", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    try:
        asyncio.run(review_prs.run(cfg, pr_url=pr_url))
    except review_prs.BaseAnalysisError as exc:
        logging.getLogger(__name__).error(RUNNER_FAILED_MSG, "review_prs", exc)  # noqa: TRY400
        sys.exit(1)


@run_app.command("focused-review")
def run_focused_review(ctx: typer.Context) -> None:
    _setup_logging("focused-review", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    asyncio.run(focused_review.run(cfg))


@run_app.command("review-requested")
def run_review_requested(
    ctx: typer.Context,
    pr_url: Annotated[
        str | None,
        typer.Option(
            "--pr",
            metavar="TEXT",
            help="Review a single PR URL instead of discovering PRs where you're a requested reviewer.",
        ),
    ] = None,
) -> None:
    """Post a dotharness-review on PRs where you're a requested reviewer (or a single PR with --pr)."""
    _setup_logging("review-requested", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    asyncio.run(review_requested.run(cfg, pr_url=pr_url))


@run_app.command("self-review")
def run_self_review(ctx: typer.Context) -> None:
    _setup_logging("self-review", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    asyncio.run(self_review.run(cfg))


@run_app.command("local-review")
def run_local_review(
    ctx: typer.Context,
    base: Annotated[
        str | None,
        typer.Option(
            "--base", metavar="TEXT", help="Base ref to compare against (default: config, origin/HEAD, main)."
        ),
    ] = None,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", metavar="PATH", help="Output root override.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Ignore the manifest and re-run every pass for this head.")
    ] = False,
    address: Annotated[bool, typer.Option("--address", help="After the review phase, address open findings.")] = False,
    skip_review: Annotated[
        bool, typer.Option("--skip-review", help="With --address: skip the review, address an earlier one.")
    ] = False,
    review_dir: Annotated[
        Path | None,
        typer.Option("--review-dir", metavar="PATH", help="With --address --skip-review: review directory."),
    ] = None,
) -> None:
    """Review the current checkout against its base, offline; write findings under the output root."""
    # local_review is typed on str paths (click.Path() yielded str); keep that contract.
    output_dir_arg = None if output_dir is None else str(output_dir)
    review_dir_arg = None if review_dir is None else str(review_dir)
    usage_error = local_review.address_usage_error(address, skip_review, review_dir_arg)
    if usage_error:
        raise typer.BadParameter(usage_error)
    _setup_logging("local-review", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"], require_repo_name=False)
    code = asyncio.run(
        local_review.run_local_review(
            cfg,
            base=base,
            output_dir=output_dir_arg,
            force=force,
            address=address,
            skip_review=skip_review,
            review_dir=review_dir_arg,
        )
    )
    sys.exit(code)


@run_app.command("address-comments")
def run_address_comments(ctx: typer.Context) -> None:
    _setup_logging("address-comments", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    asyncio.run(address_comments.run(cfg))


@run_app.command("auto-approve")
def run_auto_approve(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Evaluate and log what would be marked viewed or approved, without any GitHub write.",
        ),
    ] = False,
) -> None:
    """Mark small files viewed and approve small PRs once the author has followed up on review comments."""
    _setup_logging("auto-approve", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    asyncio.run(auto_approve.run(cfg, dry_run=dry_run))


PrOption = Annotated[
    int | None,
    typer.Option(
        "--pr",
        metavar="N",
        help="Only process the PR with this number (default: all of your open PRs).",
    ),
]


@run_app.command("ci-fix")
def run_ci_fix(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Reproduce and log what would be fixed, without any push or GitHub write."),
    ] = False,
    pr: PrOption = None,
) -> None:
    """Fix failing required CI checks on your own PRs and push the fix to the PR branch. Off unless [ci_fix].enabled."""
    _setup_logging("ci-fix", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    asyncio.run(ci_fix.run(cfg, dry_run=dry_run, pr=pr))


@run_app.command("merge")
def run_merge(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Evaluate and log whether each PR would be merged, without any merge or GitHub write."
        ),
    ] = False,
    pr: PrOption = None,
) -> None:
    """Squash-merge your own PRs once every merge condition holds. Never approves. Off unless [merge].enabled."""
    _setup_logging("merge", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    asyncio.run(merge.run(cfg, dry_run=dry_run, pr=pr))


@run_app.command("all")
def run_all(ctx: typer.Context) -> None:
    """Run all runners in sequence: review-prs, focused-review, self-review, review-requested, address-comments, auto-approve, ci-fix, merge.

    ci-fix and merge are no-ops unless [ci_fix].enabled / [merge].enabled is set.
    """
    _setup_logging("all", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    log = logging.getLogger(__name__)

    runners = [
        ("review_prs", lambda: review_prs.run(cfg)),
        ("focused_review", lambda: focused_review.run(cfg)),
        ("self_review", lambda: self_review.run(cfg)),
        ("review_requested", lambda: review_requested.run(cfg)),
        ("address_comments", lambda: address_comments.run(cfg)),
        ("auto_approve", lambda: auto_approve.run(cfg)),
        ("ci_fix", lambda: ci_fix.run(cfg)),
        ("merge", lambda: merge.run(cfg)),
    ]

    async def _run_each() -> list[str]:
        failed = []
        for name, fn in runners:
            try:
                await fn()
            except review_prs.BaseAnalysisError as exc:
                log.error(RUNNER_FAILED_MSG, name, exc)  # noqa: TRY400
                failed.append(name)
            except Exception as exc:
                log.error(RUNNER_FAILED_MSG, name, exc, exc_info=True)
                failed.append(name)
        return failed

    failed = asyncio.run(_run_each())

    if failed:
        sys.exit(1)


def _reset_local_review(config_path: Path, yes: bool) -> None:
    cfg = load_config(config_path, require_repo_name=False)
    try:
        review_root = local_review.review_root_for_reset(cfg)
    except ValueError as exc:
        fail(str(exc))
    if not review_root.exists():
        typer.echo(f"Nothing to reset: {review_root} does not exist")
        return
    if not yes:
        typer.confirm(f"Delete review directory {review_root} (all reviews for {cfg.repo_slug})?", abort=True)
    local_review.reset(review_root)
    typer.echo(f"State reset for local-review/{cfg.repo_slug}: deleted {review_root}")


# Commands whose state is not a JSON file under state/ and so can't go through state.delete_state.
_CUSTOM_RESETS = {"local-review": _reset_local_review}

ConfigOption = Annotated[Path, typer.Option("--config", metavar="PATH")]


@state_app.command("reset")
def state_reset(
    command: Annotated[str, typer.Argument(metavar="COMMAND")],
    config_path: ConfigOption = Path(HARNESS_CONFIG),
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    if command in _CUSTOM_RESETS:
        _CUSTOM_RESETS[command](Path(config_path).resolve(), yes)
        return
    cfg = load_config(Path(config_path).resolve())
    if not yes:
        typer.confirm(f"Delete state for {command}/{cfg.repo_slug}?", abort=True)
    state_mod.delete_state(cfg.repo_slug, command)
    typer.echo(f"State reset for {command}/{cfg.repo_slug}")


@schedule_app.command("install")
def schedule_install(
    command: Annotated[str, typer.Argument(metavar="COMMAND")],
    every: Annotated[str, typer.Option("--every", metavar="TEXT")],
    config_path: ConfigOption = Path(HARNESS_CONFIG),
    scheduler: Annotated[Scheduler, typer.Option("--scheduler", metavar="[cron|launchd]")] = Scheduler.cron,
) -> None:
    from dotharness.pr_review.scheduler import ScheduleEntry, install_cron, install_launchd, parse_duration

    cfg = load_config(Path(config_path).resolve())
    harness_bin = shutil.which("pr_review") or "pr_review"
    entry = ScheduleEntry(
        command=command,
        repo_slug=cfg.repo_slug,
        scheduler=scheduler.value,
        interval_seconds=parse_duration(every),
        config_path=str(Path(config_path).resolve()),
        harness_bin=harness_bin,
    )
    if scheduler is Scheduler.cron:
        install_cron(entry)
    else:
        install_launchd(entry)
    typer.echo(f"Installed {scheduler.value} schedule for {command}/{cfg.repo_slug}")


@schedule_app.command("uninstall")
def schedule_uninstall(
    command: Annotated[str, typer.Argument(metavar="COMMAND")],
    config_path: ConfigOption = Path(HARNESS_CONFIG),
    scheduler: Annotated[Scheduler, typer.Option("--scheduler", metavar="[cron|launchd]")] = Scheduler.cron,
) -> None:
    from dotharness.pr_review.scheduler import uninstall_cron, uninstall_launchd

    cfg = load_config(Path(config_path).resolve())
    if scheduler is Scheduler.cron:
        uninstall_cron(command, cfg.repo_slug)
    else:
        uninstall_launchd(command, cfg.repo_slug)
    typer.echo(f"Uninstalled {scheduler.value} schedule for {command}/{cfg.repo_slug}")


@schedule_app.command("list")
def schedule_list() -> None:
    import subprocess

    from dotharness.pr_review.scheduler import find_harness_cron_entries

    rows = []
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)  # noqa: S607
    if result.returncode == 0:
        for entry in find_harness_cron_entries(result.stdout):
            rows.append({
                "command": entry["command"],
                "slug": entry["slug"],
                "scheduler": "cron",
                "interval": f"{entry['interval_seconds'] // 3600}h"
                if entry["interval_seconds"] % 3600 == 0
                else f"{entry['interval_seconds'] // 60}m",
                "config": entry["config_path"],
            })
    launchd_dir = Path.home() / "Library/LaunchAgents"
    for plist in launchd_dir.glob("com.harness.*.plist"):
        label = plist.stem.replace("com.harness.", "")
        rows.append({"command": label, "slug": "", "scheduler": "launchd", "interval": "?", "config": str(plist)})
    if not rows:
        typer.echo("No harness schedules found.")
        return
    typer.echo(f"{'COMMAND':<22} {'SLUG':<22} {'SCHEDULER':<10} {'EVERY':<8} CONFIG")
    for r in rows:
        typer.echo(f"{r['command']:<22} {r['slug']:<22} {r['scheduler']:<10} {r['interval']:<8} {r['config']}")
