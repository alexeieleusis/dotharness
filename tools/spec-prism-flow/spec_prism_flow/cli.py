from pathlib import Path

import click

from spec_prism_flow import decompose, draft_phases, overview_stage, requirements_stage, review, workspace
from spec_prism_flow.build import parallel_runner, phase_runner, track_runner
from spec_prism_flow.build.agent_runner import branch_name
from spec_prism_flow.build.completion_log import load_all
from spec_prism_flow.build.errors import OrchestrationError
from spec_prism_flow.build.resume_state import resume_state_path
from spec_prism_flow.config import ConfigError, load_config, resolve_config_path
from spec_prism_flow.phase_file import parse_phase_file


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
@click.option("--yes", is_flag=True, default=False, help="Skip the overwrite-confirmation prompt.")
def plan_decompose(config_path_str, depth_cap, yes):
    """Recursively decompose requirements.md into a leaf tree and derive docs/phases/graph.json."""
    cfg = _load_cfg_or_raise(config_path_str)

    target_path = cfg.plan.workspace_dir / decompose.TREE_FILENAME
    if target_path.exists() and not yes:
        click.confirm(f"Overwrite existing {target_path}?", abort=True)

    try:
        tree_path, graph_path = decompose.run_decompose(cfg, depth_cap=depth_cap)
    except decompose.DecomposeError as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"Wrote {tree_path}")
    click.echo(f"Wrote {graph_path}")
    click.echo("Review the tree and graph, then proceed to `plan draft-phases` when ready.")


@cmd_plan.command("draft-phases")
@click.option("--config", "config_path_str", default=None, type=click.Path(), help="Config file to use.")
@click.option("--yes", is_flag=True, default=False, help="Skip the overwrite-confirmation prompt.")
def plan_draft_phases(config_path_str, yes):
    """Render each leaf of the approved decomposition tree into a `docs/phases/NN-name-leaf.md` file."""
    cfg = _load_cfg_or_raise(config_path_str)

    try:
        target_paths = draft_phases.target_phase_paths(cfg)
    except draft_phases.DraftPhasesError as e:
        raise click.ClickException(str(e)) from e

    existing_paths = [path for path in target_paths if path.exists()]
    if existing_paths and not yes:
        names = ", ".join(path.name for path in existing_paths)
        click.confirm(f"Overwrite existing phase file(s) ({names})?", abort=True)

    try:
        result = draft_phases.run_draft_phases(cfg)
    except draft_phases.DraftPhasesError as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"{result.leaves_drafted} leaves drafted, {result.internal_nodes_skipped} internal nodes skipped.")
    for path in result.written:
        click.echo(f"Wrote {path}")
    if result.outliers:
        click.echo("Sizing outliers (flagged, not blocking):")
        for outlier in result.outliers:
            click.echo(f"  - {outlier}")
    click.echo("Review the phase files, then run `plan review` when ready.")


@cmd_plan.command("review")
@click.option("--config", "config_path_str", default=None, type=click.Path(), help="Config file to use.")
def plan_review(config_path_str):
    """Run the four independent consistency checks over the drafted phase corpus."""
    cfg = _load_cfg_or_raise(config_path_str)

    try:
        report = review.run_review(cfg)
    except review.ReviewError as e:
        raise click.ClickException(str(e)) from e

    for check in report.checks:
        click.echo(f"{check.name}: {'PASS' if check.passed else 'FAIL'}")
        for violation in check.violations:
            click.echo(f"  - {violation}")

    if not report.all_passed:
        raise SystemExit(1)


@cmd_build.command("run")
@click.option("--start", "start_phase", default=None, type=int, help="First phase number to run (sequential only).")
@click.option("--stop", "stop_phase", default=None, type=int, help="Last phase number to run (sequential only).")
@click.option(
    "--dry-run", is_flag=True, default=False, help="Exercise the dry-run toolchain instead of live subprocess calls."
)
@click.option(
    "--resume",
    is_flag=True,
    default=False,
    help="Resume a phase from its persisted resume state, if any (sequential mode only).",
)
@click.option("--strict", is_flag=True, default=False, help="Enable the hard manual-test merge gate.")
@click.option("--config", "config_path_str", default=None, type=click.Path(), help="Config file to use.")
def build_run(start_phase, stop_phase, dry_run, resume, strict, config_path_str):
    """Drive the phase corpus to completion against the clone in the current working
    directory -- sequentially or in parallel, per config.build.workers."""
    cfg = _load_cfg_or_raise(config_path_str)

    workers = cfg.build.workers
    if workers <= 0:
        raise click.UsageError(f"config.build.workers must be a positive integer, got {workers}")  # noqa: TRY003
    if workers > 1 and (start_phase is not None or stop_phase is not None or resume):
        raise click.UsageError(  # noqa: TRY003
            "--start/--stop/--resume are only supported in sequential mode (config.build.workers == 1)"
        )

    clone = Path.cwd()
    try:
        if workers == 1:
            results = track_runner.run_track(
                cfg,
                clone,
                start_phase=start_phase,
                stop_phase=stop_phase,
                dry_run=dry_run,
                resume=resume,
                strict=strict,
            )
        else:
            results = parallel_runner.run_parallel(cfg, clone, workers=workers, dry_run=dry_run, strict=strict)
    except OrchestrationError as exc:
        click.echo(str(exc))
        if exc.next_command:
            click.echo(f"Next: {exc.next_command}")
        raise SystemExit(exc.exit_code) from None

    for result in results:
        click.echo(f"Phase {result.phase_number:02d}: {'merged' if result.merged else 'not merged'}")


_STATUS_COLUMNS = (("Phase", 7), ("Name", 40), ("Status", 12), ("Address cycles", 15), ("Escalations", 12))


def _status_row(values: tuple[str, ...]) -> str:
    return "  ".join(value.ljust(width) for value, (_, width) in zip(values, _STATUS_COLUMNS, strict=True))


@cmd_build.command("status")
@click.option("--config", "config_path_str", default=None, type=click.Path(), help="Config file to use.")
def build_status(config_path_str):
    """Print a merged/escalated/in progress/pending row per phase, classified from
    the completion log and Phase 08's resume-state file presence -- against the
    clone in the current working directory."""
    cfg = _load_cfg_or_raise(config_path_str)
    clone = Path.cwd()
    repo = phase_runner.repo_slug(clone)

    phases = [parse_phase_file(path) for path in track_runner.discover_phase_files(cfg.plan.phase_dir)]
    records_by_number = {
        record.phase_number: record for record in load_all(clone / track_runner.COMPLETION_LOG_JSON_RELPATH)
    }

    click.echo(_status_row(tuple(name for name, _ in _STATUS_COLUMNS)))
    for phase in phases:
        record = records_by_number.get(phase.number)
        if record is not None and record.pr_merged_at is not None:
            status = "merged"
        elif record is not None and record.escalation_reason is not None:
            status = "escalated"
        else:
            state_path = resume_state_path(cfg, repo, branch_name(phase))
            status = "in progress" if state_path.exists() else "pending"

        address_cycles = record.address_comments_cycles if record is not None else 0
        escalations = record.human_escalations if record is not None else 0
        click.echo(_status_row((f"{phase.number:02d}", phase.name, status, str(address_cycles), str(escalations))))


if __name__ == "__main__":
    cli()
