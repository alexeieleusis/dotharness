import logging
import sys
from datetime import date
from pathlib import Path

import click

from harness import state as state_mod
from harness.config import load_config
from harness.runners import address_comments, focused_review, review_prs, review_requested, self_review

TEMPLATE = """\
# dotharness configuration — DO NOT COMMIT this file
# Add .harness.toml to your global gitignore

[harness]
backend = "opencode"               # "opencode" or "claude"
gh_token_cmd = "gh auth token"
backend_timeout_seconds = 900
knowledge_dir = "~/.harness/knowledge"

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
# trusted_commenters = "*"

# [[repo.subdir]]
# path = "."
# pre_commands = []
# coverage = false
# timeout = 300
"""

HARNESS_CONFIG = ".harness.toml"
XDG_LOGS = Path.home() / ".local/share/dotharness/logs"


def _setup_logging(command: str, verbose: bool = False) -> None:
    log_dir = XDG_LOGS / command
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{date.today().isoformat()}.log"
    handlers: list[logging.Handler] = [logging.FileHandler(log_file)]
    if sys.stdout.isatty() or verbose:
        handlers.append(logging.StreamHandler(sys.stdout))
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, handlers=handlers, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@click.group()
def cli():
    pass  # Click group container; subcommands are registered via @cli.command()


@cli.command("init")
@click.argument("directory", default=".", required=False)
def cmd_init(directory):
    """Write a .harness.toml template to DIRECTORY (default: current directory)."""
    p = Path(directory) / HARNESS_CONFIG
    if p.exists():
        raise click.ClickException(f"{HARNESS_CONFIG} already exists in {Path(directory).resolve()}")  # noqa: TRY003
    p.write_text(TEMPLATE)
    click.echo(f"Created {p.resolve()}")


@cli.command("validate")
@click.argument("directory", default=".", required=False)
@click.option("--config", "config_path", default=None, type=click.Path())
def cmd_validate(directory, config_path):
    """Validate .harness.toml in DIRECTORY (default: current directory)."""
    import shutil

    if config_path is None:
        config_path = str(Path(directory) / HARNESS_CONFIG)
    errors = []
    try:
        cfg = load_config(Path(config_path))
    except Exception as e:
        click.echo(str(e), err=True)
        sys.exit(1)

    if not cfg.repo.working_dir.exists():
        errors.append(f"repo.working_dir: path does not exist: {cfg.repo.working_dir}")
    if not cfg.harness.knowledge_dir.exists():
        errors.append(f"harness.knowledge_dir: path does not exist: {cfg.harness.knowledge_dir}")
    backend_bin = "opencode" if cfg.harness.backend == "opencode" else "claude"
    if not shutil.which(backend_bin):
        errors.append(f"harness.backend: binary '{backend_bin}' not found on PATH")
    if not shutil.which("gh"):
        errors.append("gh: binary not found on PATH")
    if cfg.vibe_heal.enabled and cfg.vibe_heal.python and not Path(cfg.vibe_heal.python).expanduser().exists():
        errors.append(f"vibe_heal.python: path does not exist: {cfg.vibe_heal.python}")

    if errors:
        for e in errors:
            click.echo(e, err=True)
        sys.exit(1)
    click.echo(f"Config valid: {cfg.repo.name} backend={cfg.harness.backend}")
    sys.exit(0)


@cli.group("run")
@click.option("--config", "config_path", default=None, type=click.Path(), is_eager=True)
@click.option("--verbose", is_flag=True, default=False, help="Enable DEBUG-level logging.")
@click.pass_context
def cmd_run(ctx, config_path, verbose):
    ctx.ensure_object(dict)
    if config_path is None:
        config_path = str(Path(".") / HARNESS_CONFIG)
    ctx.obj["config_path"] = Path(config_path).resolve()
    ctx.obj["verbose"] = verbose


@cmd_run.command("review-prs")
@click.option(
    "--pr",
    "pr_url",
    default=None,
    help=(
        "Review a single PR URL instead of discovering open PRs automatically. "
        "Bypasses state entirely: the per-PR reviewed-SHA record is neither read nor "
        "updated, so this PR can be re-run any number of times without affecting future "
        "review-prs runs."
    ),
)
@click.pass_context
def run_review_prs(ctx, pr_url):
    """Run vibe_heal static analysis review over open PRs (or a single PR with --pr)."""
    _setup_logging("review-prs", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    review_prs.run(cfg, pr_url=pr_url)


@cmd_run.command("focused-review")
@click.pass_context
def run_focused_review(ctx):
    _setup_logging("focused-review", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    focused_review.run(cfg)


@cmd_run.command("review-requested")
@click.option(
    "--pr",
    "pr_url",
    default=None,
    help="Review a single PR URL instead of discovering PRs where you're a requested reviewer.",
)
@click.pass_context
def run_review_requested(ctx, pr_url):
    """Post an osc-review on PRs where you're a requested reviewer (or a single PR with --pr)."""
    _setup_logging("review-requested", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    review_requested.run(cfg, pr_url=pr_url)


@cmd_run.command("self-review")
@click.pass_context
def run_self_review(ctx):
    _setup_logging("self-review", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    self_review.run(cfg)


@cmd_run.command("address-comments")
@click.pass_context
def run_address_comments(ctx):
    _setup_logging("address-comments", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    address_comments.run(cfg)


@cmd_run.command("all")
@click.pass_context
def run_all(ctx):
    """Run all runners in sequence: review-prs, focused-review, self-review, review-requested, address-comments."""
    _setup_logging("all", ctx.obj.get("verbose", False))
    cfg = load_config(ctx.obj["config_path"])
    log = logging.getLogger(__name__)

    runners = [
        ("review_prs", lambda: review_prs.run(cfg)),
        ("focused_review", lambda: focused_review.run(cfg)),
        ("self_review", lambda: self_review.run(cfg)),
        ("review_requested", lambda: review_requested.run(cfg)),
        ("address_comments", lambda: address_comments.run(cfg)),
    ]

    failed = []
    for name, fn in runners:
        try:
            fn()
        except Exception as exc:
            log.error("Runner %s failed: %s", name, exc, exc_info=True)
            failed.append(name)

    if failed:
        sys.exit(1)


@cli.group("state")
def cmd_state():
    pass  # Intentionally empty; Click group container for subcommands


@cmd_state.command("reset")
@click.argument("command")
@click.option("--config", "config_path", default=HARNESS_CONFIG, type=click.Path())
@click.option("--yes", is_flag=True)
def state_reset(command, config_path, yes):
    cfg = load_config(Path(config_path).resolve())
    if not yes:
        click.confirm(f"Delete state for {command}/{cfg.repo_slug}?", abort=True)
    state_mod.delete_state(cfg.repo_slug, command)
    click.echo(f"State reset for {command}/{cfg.repo_slug}")


@cli.group("schedule")
def cmd_schedule():
    pass  # Click group handler; subcommands are attached via @cmd_schedule.command()


@cmd_schedule.command("install")
@click.argument("command")
@click.option("--config", "config_path", default=HARNESS_CONFIG, type=click.Path())
@click.option("--every", required=True)
@click.option("--scheduler", default="cron", type=click.Choice(["cron", "launchd"]))
def schedule_install(command, config_path, every, scheduler):
    import shutil

    from harness.scheduler import ScheduleEntry, install_cron, install_launchd, parse_duration

    cfg = load_config(Path(config_path).resolve())
    harness_bin = shutil.which("harness") or "harness"
    entry = ScheduleEntry(
        command=command,
        repo_slug=cfg.repo_slug,
        scheduler=scheduler,
        interval_seconds=parse_duration(every),
        config_path=str(Path(config_path).resolve()),
        harness_bin=harness_bin,
    )
    if scheduler == "cron":
        install_cron(entry)
    else:
        install_launchd(entry)
    click.echo(f"Installed {scheduler} schedule for {command}/{cfg.repo_slug}")


@cmd_schedule.command("uninstall")
@click.argument("command")
@click.option("--config", "config_path", default=HARNESS_CONFIG, type=click.Path())
@click.option("--scheduler", default="cron", type=click.Choice(["cron", "launchd"]))
def schedule_uninstall(command, config_path, scheduler):
    from harness.scheduler import uninstall_cron, uninstall_launchd

    cfg = load_config(Path(config_path).resolve())
    if scheduler == "cron":
        uninstall_cron(command, cfg.repo_slug)
    else:
        uninstall_launchd(command, cfg.repo_slug)
    click.echo(f"Uninstalled {scheduler} schedule for {command}/{cfg.repo_slug}")


@cmd_schedule.command("list")
def schedule_list():
    import subprocess

    from harness.scheduler import find_harness_cron_entries

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
        click.echo("No harness schedules found.")
        return
    click.echo(f"{'COMMAND':<22} {'SLUG':<22} {'SCHEDULER':<10} {'EVERY':<8} CONFIG")
    for r in rows:
        click.echo(f"{r['command']:<22} {r['slug']:<22} {r['scheduler']:<10} {r['interval']:<8} {r['config']}")
