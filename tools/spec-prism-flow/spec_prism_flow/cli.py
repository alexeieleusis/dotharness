from pathlib import Path

import click

from spec_prism_flow import decompose, overview_stage, requirements_stage, workspace
from spec_prism_flow.config import ConfigError, load_config, resolve_config_path


@click.group()
def cli():
    pass  # Click group container; subcommands are registered via @cli.command()


@cli.group("plan")
def cmd_plan():
    """Requirements refinement: brief -> phase corpus (Feature A)."""


@cli.group("build")
def cmd_build():
    """Phase execution: phase corpus -> merged code (Feature B)."""


def _require_existing_path(path_str: str, label: str) -> Path:
    path = Path(path_str)
    if not path.exists():
        raise click.ClickException(f"{label} does not exist: {path}")  # noqa: TRY003
    return path


def _require_readable_file(path_str: str, label: str) -> Path:
    path = _require_existing_path(path_str, label)
    try:
        with open(path, "rb") as f:
            f.read(1)
    except OSError as e:
        raise click.ClickException(f"{label} is not readable: {path} ({e})") from e  # noqa: TRY003
    return path


def _load_cfg_or_raise(config_path_str: str | None):
    try:
        return load_config(resolve_config_path(config_path_str).resolve())
    except ConfigError as e:
        raise click.ClickException(str(e)) from e


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

    cfg = _load_cfg_or_raise(config_path_str)

    existing_manifest = workspace.manifest_path(cfg.plan.workspace_dir)
    if existing_manifest.exists() and not yes:
        click.confirm(f"Overwrite existing {existing_manifest}?", abort=True)

    written = workspace.init_workspace(cfg.plan.workspace_dir, brief, code, conventions, links)
    click.echo(f"Wrote {written}")


@cmd_plan.command("draft-overview")
@click.option("--config", "config_path_str", default=None, type=click.Path(), help="Config file to use.")
@click.option("--yes", is_flag=True, default=False, help="Skip the overwrite-confirmation prompt.")
def plan_draft_overview(config_path_str, yes):
    """Hand off to an agent to draft 00-overview.md from init_manifest.json."""
    cfg = _load_cfg_or_raise(config_path_str)

    target_path = cfg.plan.workspace_dir / overview_stage.OVERVIEW_FILENAME
    if target_path.exists() and not yes:
        click.confirm(f"Overwrite existing {target_path}?", abort=True)

    try:
        overview_path, open_questions_path = overview_stage.run_draft_overview(cfg)
    except overview_stage.OverviewError as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"Wrote {overview_path}")
    click.echo(f"Open questions: {open_questions_path}" if open_questions_path else "No open questions raised.")
    click.echo(
        "Edit OPEN_QUESTIONS.md (if present) or 00-overview.md directly, then run `plan draft-requirements` when ready."
    )


@cmd_plan.command("draft-requirements")
@click.option("--config", "config_path_str", default=None, type=click.Path(), help="Config file to use.")
@click.option("--yes", is_flag=True, default=False, help="Skip the overwrite-confirmation prompt.")
def plan_draft_requirements(config_path_str, yes):
    """Hand off to an agent to draft requirements.md from the approved overview."""
    cfg = _load_cfg_or_raise(config_path_str)

    target_path = cfg.plan.workspace_dir / requirements_stage.REQUIREMENTS_FILENAME
    if target_path.exists() and not yes:
        click.confirm(f"Overwrite existing {target_path}?", abort=True)

    try:
        requirements_path = requirements_stage.run_draft_requirements(cfg)
    except requirements_stage.RequirementsError as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"Wrote {requirements_path}")
    click.echo("Review/edit requirements.md, then proceed to `plan decompose` when ready.")


@cmd_plan.command("decompose")
@click.option("--config", "config_path_str", default=None, type=click.Path(), help="Config file to use.")
@click.option(
    "--depth-cap",
    default=decompose.DEFAULT_DEPTH_CAP,
    type=int,
    show_default=True,
    help="Max recursion depth before escalating a would-be split for human review.",
)
def plan_decompose(config_path_str, depth_cap):
    """Recursively decompose requirements.md into a leaf tree and derive docs/phases/graph.json."""
    cfg = _load_cfg_or_raise(config_path_str)

    try:
        tree_path, graph_path = decompose.run_decompose(cfg, depth_cap=depth_cap)
    except decompose.DecomposeError as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"Wrote {tree_path}")
    click.echo(f"Wrote {graph_path}")
    click.echo("Review the tree and graph, then proceed to `plan draft-phases` when ready.")


if __name__ == "__main__":
    cli()
