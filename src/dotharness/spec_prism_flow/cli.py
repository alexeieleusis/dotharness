from pathlib import Path
from typing import Annotated, NoReturn

import typer

from dotharness.spec_prism_flow import decompose, draft_phases, overview_stage, requirements_stage, review, workspace
from dotharness.spec_prism_flow.config import ConfigError, SpecPrismFlowConfig, load_config, resolve_config_path
from dotharness.spec_prism_flow.decompose_journal import JOURNAL_FILENAME

app = typer.Typer(
    name="spec_prism_flow",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
cli = app  # alias kept for dotharness.cli (``from dotharness.spec_prism_flow.cli import cli as spec_prism_flow``)

plan_app = typer.Typer(
    name="plan",
    help="Requirements refinement: brief -> phase corpus.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(plan_app, name="plan")

ConfigOption = Annotated[Path | None, typer.Option("--config", help="Config file to use.")]
YesOption = Annotated[bool, typer.Option("--yes", help="Skip the overwrite-confirmation prompt.")]


def _fail(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def _require_existing_path(path: Path, label: str) -> Path:
    if not path.exists():
        _fail(f"{label} does not exist: {path}")
    return path


def _require_readable_file(path: Path, label: str) -> Path:
    _require_existing_path(path, label)
    try:
        with path.open("rb") as f:
            f.read(1)
    except OSError as e:
        _fail(f"{label} is not readable: {path} ({e})")
    return path


def _load_cfg_or_raise(config_path: Path | None) -> SpecPrismFlowConfig:
    try:
        return load_config(resolve_config_path(str(config_path) if config_path else None).resolve())
    except ConfigError as e:
        _fail(str(e))


@plan_app.command("init")
def plan_init(
    brief_path: Annotated[Path, typer.Argument()],
    code: Annotated[Path | None, typer.Option("--code", help="Existing path to prior code.")] = None,
    conventions: Annotated[Path | None, typer.Option("--conventions", help="Existing path to conventions.")] = None,
    links: Annotated[str | None, typer.Option("--links", help="Comma-separated URLs, stored verbatim.")] = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Create a plan workspace from BRIEF_PATH and write init_manifest.json."""
    brief = _require_readable_file(brief_path, "BRIEF_PATH")
    code_path = _require_existing_path(code, "--code") if code else None
    conventions_path = _require_existing_path(conventions, "--conventions") if conventions else None
    link_list = [link.strip() for link in (links or "").split(",") if link.strip()]

    cfg = _load_cfg_or_raise(config)

    existing_manifest = workspace.manifest_path(cfg.plan.workspace_dir)
    if existing_manifest.exists() and not yes:
        typer.confirm(f"Overwrite existing {existing_manifest}?", abort=True)

    written = workspace.init_workspace(cfg.plan.workspace_dir, brief, code_path, conventions_path, link_list)
    typer.echo(f"Wrote {written}")


@plan_app.command("draft-overview")
def plan_draft_overview(config: ConfigOption = None, yes: YesOption = False) -> None:
    """Hand off to an agent to draft 00-overview.md from init_manifest.json."""
    cfg = _load_cfg_or_raise(config)

    target_path = cfg.plan.workspace_dir / overview_stage.OVERVIEW_FILENAME
    if target_path.exists() and not yes:
        typer.confirm(f"Overwrite existing {target_path}?", abort=True)

    try:
        overview_path, open_questions_path = overview_stage.run_draft_overview(cfg)
    except overview_stage.OverviewError as e:
        _fail(str(e))

    typer.echo(f"Wrote {overview_path}")
    typer.echo(f"Open questions: {open_questions_path}" if open_questions_path else "No open questions raised.")
    typer.echo(
        "Edit OPEN_QUESTIONS.md (if present) or 00-overview.md directly, then run `plan draft-requirements` when ready."
    )


@plan_app.command("draft-requirements")
def plan_draft_requirements(config: ConfigOption = None, yes: YesOption = False) -> None:
    """Hand off to an agent to draft requirements.md from the approved overview."""
    cfg = _load_cfg_or_raise(config)

    target_path = cfg.plan.workspace_dir / requirements_stage.REQUIREMENTS_FILENAME
    if target_path.exists() and not yes:
        typer.confirm(f"Overwrite existing {target_path}?", abort=True)

    try:
        requirements_path = requirements_stage.run_draft_requirements(cfg)
    except requirements_stage.RequirementsError as e:
        _fail(str(e))

    typer.echo(f"Wrote {requirements_path}")
    typer.echo("Review/edit requirements.md, then proceed to `plan decompose` when ready.")


@plan_app.command("decompose")
def plan_decompose(
    config: ConfigOption = None,
    depth_cap: Annotated[
        int,
        typer.Option(
            "--depth-cap",
            show_default=True,
            help="Max recursion depth before escalating a would-be split for human review.",
        ),
    ] = decompose.DEFAULT_DEPTH_CAP,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Continue an interrupted run, reusing every generator result already recorded in the journal.",
        ),
    ] = False,
    yes: YesOption = False,
) -> None:
    """Recursively decompose requirements.md into a leaf tree and derive docs/phases/graph.json."""
    cfg = _load_cfg_or_raise(config)

    journal_path = cfg.plan.workspace_dir / JOURNAL_FILENAME
    if journal_path.exists() and not resume and not yes:
        resume = typer.confirm(
            f"Found journal {journal_path.name} from an interrupted run. Resume it? (No discards it and starts over)",
            default=True,
        )

    target_path = cfg.plan.workspace_dir / decompose.TREE_FILENAME
    if target_path.exists() and not yes and not (resume and journal_path.exists()):
        typer.confirm(f"Overwrite existing {target_path}?", abort=True)

    try:
        tree_path, graph_path = decompose.run_decompose(cfg, depth_cap=depth_cap, resume=resume)
    except decompose.DecomposeError as e:
        _fail(str(e))

    typer.echo(f"Wrote {tree_path}")
    typer.echo(f"Wrote {graph_path}")
    typer.echo("Review the tree and graph, then proceed to `plan draft-phases` when ready.")


@plan_app.command("draft-phases")
def plan_draft_phases(config: ConfigOption = None, yes: YesOption = False) -> None:
    """Render each leaf of the approved decomposition tree into a `docs/phases/NN-name-leaf.md` file."""
    cfg = _load_cfg_or_raise(config)

    try:
        target_paths = draft_phases.target_phase_paths(cfg)
    except draft_phases.DraftPhasesError as e:
        _fail(str(e))

    existing_paths = [path for path in target_paths if path.exists()]
    if existing_paths and not yes:
        names = ", ".join(path.name for path in existing_paths)
        typer.confirm(f"Overwrite existing phase file(s) ({names})?", abort=True)

    try:
        result = draft_phases.run_draft_phases(cfg)
    except draft_phases.DraftPhasesError as e:
        _fail(str(e))

    typer.echo(f"{result.leaves_drafted} leaves drafted, {result.internal_nodes_skipped} internal nodes skipped.")
    for path in result.written:
        typer.echo(f"Wrote {path}")
    if result.outliers:
        typer.echo("Sizing outliers (flagged, not blocking):")
        for outlier in result.outliers:
            typer.echo(f"  - {outlier}")
    typer.echo("Review the phase files, then run `plan review` when ready.")


@plan_app.command("review")
def plan_review(config: ConfigOption = None) -> None:
    """Run the four independent consistency checks over the drafted phase corpus."""
    cfg = _load_cfg_or_raise(config)

    try:
        report = review.run_review(cfg)
    except review.ReviewError as e:
        _fail(str(e))

    for check in report.checks:
        typer.echo(f"{check.name}: {'PASS' if check.passed else 'FAIL'}")
        for violation in check.violations:
            typer.echo(f"  - {violation}")

    if not report.all_passed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
