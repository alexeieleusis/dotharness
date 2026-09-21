from pathlib import Path

import click

from spec_prism_flow import workspace
from spec_prism_flow.config import load_config

CONFIG_FILE = ".spec-prism-flow.toml"


@click.group()
def cli():
    pass  # Click group container; subcommands are registered via @cli.command()


@cli.group("plan")
def cmd_plan():
    """Requirements refinement: brief -> phase corpus (Feature A)."""


@cli.group("build")
def cmd_build():
    """Phase execution: phase corpus -> merged code (Feature B)."""


def _require_readable_file(path_str: str, label: str) -> Path:
    path = Path(path_str)
    if not path.exists():
        raise click.ClickException(f"{label} does not exist: {path}")  # noqa: TRY003
    try:
        with open(path, "rb") as f:
            f.read(1)
    except OSError as e:
        raise click.ClickException(f"{label} is not readable: {path} ({e})") from e  # noqa: TRY003
    return path


def _require_existing_path(path_str: str, label: str) -> Path:
    path = Path(path_str)
    if not path.exists():
        raise click.ClickException(f"{label} does not exist: {path}")  # noqa: TRY003
    return path


@cmd_plan.command("init")
@click.argument("brief_path", type=click.Path())
@click.option("--code", "code_path_str", default=None, type=click.Path(), help="Existing path to prior code.")
@click.option(
    "--conventions", "conventions_path_str", default=None, type=click.Path(), help="Existing path to conventions."
)
@click.option("--links", "links_text", default=None, help="Comma-separated URLs, stored verbatim.")
@click.option("--config", "config_path_str", default=None, type=click.Path(), help="Config file to use.")
@click.option("--yes", is_flag=True, default=False, help="Skip the overwrite-confirmation prompt.")
def plan_init(brief_path, code_path_str, conventions_path_str, links_text, config_path_str, yes):
    """Create a plan workspace from BRIEF_PATH and write init_manifest.json."""
    brief = _require_readable_file(brief_path, "BRIEF_PATH")
    code = _require_existing_path(code_path_str, "--code") if code_path_str else None
    conventions = _require_existing_path(conventions_path_str, "--conventions") if conventions_path_str else None
    links = [link.strip() for link in (links_text or "").split(",") if link.strip()]

    config_path = Path(config_path_str) if config_path_str else Path(CONFIG_FILE)
    cfg = load_config(config_path.resolve())

    existing_manifest = workspace.manifest_path(cfg.plan.workspace_dir)
    if existing_manifest.exists() and not yes:
        click.confirm(f"Overwrite existing {existing_manifest}?", abort=True)

    written = workspace.init_workspace(cfg.plan.workspace_dir, brief, code, conventions, links)
    click.echo(f"Wrote {written}")


if __name__ == "__main__":
    cli()
