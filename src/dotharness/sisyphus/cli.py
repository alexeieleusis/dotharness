"""Single-shot ``sisyphus`` CLI (D-17): ``sisyphus run`` and ``sisyphus intake``.

One invocation, one config, no daemon. ``run`` takes the per-clone lock non-blockingly once, holds it for the whole invocation, skips picking
when an earlier Sisyphus PR still awaits review, otherwise picks up to ``max_tickets_per_run`` items,
runs each through the orchestrator and writes one merged digest. Credentials come only from the
inherited environment and ``[source.host].token_cmd``.

Everything external is built by a services factory (``app invoked with obj=factory`` in tests), so no test runs a
real agent, ``gh``, ``git push`` or the harness. The harness is reached only through the orchestrator's
subprocess calls; this module never imports it.

Exit codes: 0 for success, nothing to do, lock held, or a run stopped at a checkpoint (its digest is
written, the checkpoint is in the Needs-you section); 1 for an unhandled error, a config error or a
digest that cannot be written (message on stderr).
"""

# ruff: noqa: TRY003
from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from dotharness.ai_backend import launch
from dotharness.cli_common import fail
from dotharness.sisyphus.agent import Launcher, make_agent_runner
from dotharness.sisyphus.archive import archive_item, check_archive_dir, resolve_archive_dir
from dotharness.sisyphus.cleanup import PrInfo
from dotharness.sisyphus.config import ConfigError, SisyphusConfig, load_config
from dotharness.sisyphus.digest import (
    DigestReport,
    DigestWriteError,
    NeedsYou,
    describe_reset,
    indent_block,
    needs_from_record,
    needs_from_stop,
    write_digest,
)
from dotharness.sisyphus.intake import AgentRunner, run_intake
from dotharness.sisyphus.lock import LockHeld, clone_lock
from dotharness.sisyphus.orchestrator import (
    PR_NUMBER_KEY,
    GitHubError,
    OrchestratorOptions,
    PullRequest,
    PullRequestClient,
    RunResult,
    SubprocessRunner,
    default_subprocess_runner,
    run_item,
)
from dotharness.sisyphus.pick import (
    Candidate,
    FileTracker,
    GhCli,
    GhLabelRemover,
    GhPullRequestLookup,
    GitHubIssueTracker,
    LabelRemover,
    LinearTracker,
    PickError,
    PullRequestLookup,
    TrackerClient,
    label_removal_number,
    pick_items,
)
from dotharness.sisyphus.present import PresentResult, present_stops
from dotharness.sisyphus.rails import LimitTracker
from dotharness.sisyphus.resume import (
    AUTO_RETRY_KEY,
    cleared,
    is_stopped,
    iter_records,
    list_waiting,
    reset_stage_retries,
)
from dotharness.sisyphus.runrecord import STAGE_IN_PROGRESS, RunRecord, record_path
from dotharness.sisyphus.settled import is_settled
from dotharness.sisyphus.stages import INTAKE_STAGE
from dotharness.sisyphus.stoprecord import (
    CLEARS_BY,
    StopRecord,
    build_abandoned_record,
    build_stop_record,
    clear_stop_record,
    fill_resume_template,
    is_abandoned,
    read_stop_record,
    read_stop_text,
    stop_record_path,
    write_stop_record,
    write_stop_text,
)
from dotharness.sisyphus.subproc import run_command as run_subprocess

TOKEN_CMD_TIMEOUT = 30
DEFAULT_BASE_BRANCH = "main"


# ------------------------------------------------------------- real services


def run_token_cmd(token_cmd: str | None, run: Callable[..., Any] = subprocess.run) -> str | None:
    """Run ``[source.host].token_cmd`` (no shell) and return its stripped stdout; ``None`` when unset."""
    if not token_cmd:
        return None
    try:
        proc = run(shlex.split(token_cmd), capture_output=True, text=True, check=False, timeout=TOKEN_CMD_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        # Never interpolate str(exc): TimeoutExpired/FileNotFoundError embed argv, which may hold a secret.
        why = type(exc).__name__ + (f": {exc.strerror}" if isinstance(exc, OSError) and exc.strerror else "")
        raise ConfigError(f"[source.host] token_cmd failed to run ({why})") from None
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ConfigError(f"[source.host] token_cmd failed (exit {proc.returncode}) or printed no token")
    return proc.stdout.strip()


_PR_URL = re.compile(r"/pull/(\d+)")


class GhPullRequestClient:
    """Real :class:`PullRequestClient`: ``gh pr list`` / ``gh pr create`` in the dedicated clone."""

    def __init__(self, gh: GhCli) -> None:
        self.gh = gh

    async def find_pr(self, branch: str) -> PullRequest | None:
        try:
            rows = await self.gh.json("pr", "list", "--head", branch, "--state", "open", "--json", "number,url")
        except PickError as exc:
            raise GitHubError(str(exc)) from exc
        for row in rows or []:
            return PullRequest(int(row["number"]), row.get("url", ""))
        return None

    async def pr_state(self, ref: str) -> PrInfo | None:
        """``gh pr view <number or branch> --json number,state,headRefOid``; any state (used by branch cleanup)."""
        try:
            row = await self.gh.json("pr", "view", ref, "--json", "number,state,headRefOid")
        except PickError as exc:
            raise GitHubError(str(exc)) from exc
        return _pr_info(row) if isinstance(row, dict) else None

    async def open_pr(self, branch: str, base: str, title: str, body: str) -> PullRequest:
        res = await self.gh.run(
            "pr", "create", "--draft", "--head", branch, "--base", base, "--title", title, "--body", body
        )
        match = _PR_URL.search(res.stdout)
        if res.returncode != 0 or not match:
            raise GitHubError(
                f"gh pr create failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()[-300:]}"
            )
        return PullRequest(int(match.group(1)), res.stdout.strip().splitlines()[-1])


def _pr_info(row: dict[str, Any]) -> PrInfo:
    number = row.get("number")
    return PrInfo(
        int(number) if isinstance(number, int) else None, str(row.get("state") or ""), str(row.get("headRefOid") or "")
    )


PR_STATE_TIMEOUT = 10.0  # seconds per `gh pr view`; fixed, not configurable


class GhPrStateLookup:
    """PR state by number: ``gh pr view <n> --json state`` through :class:`GhCli`, bounded by a fixed timeout."""

    def __init__(self, repo_dir: Path, token: str | None, timeout: float = PR_STATE_TIMEOUT) -> None:
        async def run(argv: Sequence[str], cwd: Path | None, env: Any) -> Any:
            return await run_subprocess(argv, cwd, env, timeout=timeout)

        self.gh = GhCli(repo_dir, token, run)

    async def __call__(self, number: int) -> str:
        row = await self.gh.json("pr", "view", str(number), "--json", "state")
        state = row.get("state") if isinstance(row, dict) else None
        if not isinstance(state, str) or not state:
            raise PickError(f"gh pr view {number} returned no state")
        return state.upper()


class GhTimeboundLabelRemover(GhLabelRemover):
    """:class:`GhLabelRemover` whose ``gh`` call is bounded by :data:`PR_STATE_TIMEOUT` (a timeout is exit 124)."""

    def __init__(self, repo_dir: Path, token: str | None, timeout: float = PR_STATE_TIMEOUT) -> None:
        async def run(argv: Sequence[str], cwd: Path | None, env: Any) -> Any:
            return await run_subprocess(argv, cwd, env, timeout=timeout)

        super().__init__(GhCli(repo_dir, token, run))


@dataclass
class Services:
    """Everything ``run`` and ``intake`` touch outside the process. Tests build fakes of these."""

    tracker: TrackerClient
    lookup: PullRequestLookup
    pr_client: PullRequestClient
    agent_runner: AgentRunner
    subprocess_runner: SubprocessRunner = default_subprocess_runner
    clock: Callable[[], float] = time.monotonic
    lock_dir: Path | None = None
    today: datetime | None = None
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    repo_info: Callable[[], Awaitable[str | None]] | None = field(default=None)
    # `status --check-prs`: PR number -> state (OPEN, MERGED, CLOSED); raises PickError when it cannot be read.
    pr_state: Callable[[int], Awaitable[str]] | None = None
    # Removes the pick label from a settled GitHub issue; `None` means no removal is available.
    label_remover: LabelRemover | None = None
    # Launcher for the stop-text presentation call (returns the RunResult, unlike `agent_runner`); tests pass a fake.
    present_launcher: Launcher = launch


ServicesFactory = Callable[[SisyphusConfig], Services]


async def _no_backend(_prompt: str) -> None:
    raise ConfigError("[agent] backend is not set")


def default_services(config: SisyphusConfig) -> Services:
    """Real services for ``config``: ``gh``, Linear, the configured agent backend and ``git``."""
    repo_dir = Path(config.project.repo_dir or ".").expanduser()
    token = run_token_cmd(config.source.host.token_cmd if config.source.host else None)
    gh = GhCli(repo_dir, token)
    work_item = config.source.work_item
    if work_item is not None and work_item.kind == "linear":
        tracker: TrackerClient = LinearTracker(os.environ.get("LINEAR_API_KEY", ""))
    elif work_item is not None and work_item.kind == "file":
        tracker = FileTracker()
    else:
        tracker = GitHubIssueTracker(gh)
    runner = (
        make_agent_runner(config.agent.backend, repo_dir, timeout_minutes=config.limits.max_run_minutes)
        if config.agent.backend
        else _no_backend
    )

    async def repo_info() -> str | None:
        """The repository default branch from ``gh repo view``, or ``None`` if it cannot be read."""
        try:
            data = await gh.json("repo", "view", "--json", "defaultBranchRef")
            return (data.get("defaultBranchRef") or {}).get("name")
        except (PickError, KeyError, TypeError, AttributeError):
            return None

    return Services(
        tracker=tracker,
        lookup=GhPullRequestLookup(gh),
        pr_client=GhPullRequestClient(gh),
        agent_runner=runner,
        repo_info=repo_info,
        pr_state=GhPrStateLookup(repo_dir, token),
        label_remover=GhTimeboundLabelRemover(repo_dir, token),
    )


# --------------------------------------------------------------------- helpers


def _load(config_path: str | Path) -> SisyphusConfig:
    try:
        return load_config(config_path)
    except ConfigError as exc:
        fail(f"config error: {exc}")


def _require(config: SisyphusConfig, *keys: str) -> None:
    for key in keys:
        if not getattr(config.project, key):
            fail(f"config error: [project] {key} is required")


def _services(factory: ServicesFactory | None, config: SisyphusConfig) -> Services:
    try:
        return (factory or default_services)(config)
    except (ConfigError, PickError) as exc:
        fail(f"config error: {exc}")


def _merge(parts: Sequence[DigestReport], extra: DigestReport) -> DigestReport:
    reports = [*parts, extra]
    return DigestReport(
        needs_you=tuple(x for r in reports for x in r.needs_you),
        done=tuple(x for r in reports for x in r.done),
        declined=tuple(x for r in reports for x in r.declined),
        deviations=tuple(x for r in reports for x in r.deviations),
        limits_hit=tuple(x for r in reports for x in r.limits_hit),
        timings=tuple(x for r in reports for x in r.timings),
    )


def _summarise(result: RunResult) -> str:
    if result.checkpoint is not None:
        return f"{result.item}: stopped at {result.checkpoint.value}: {result.detail}"
    if result.pr is not None:
        return f"{result.item}: draft PR #{result.pr.number} {result.pr.url}".rstrip()
    return f"{result.item}: done"


def _resume_template(config_path: str | Path, base: str | None) -> str:
    cmd = f"sisyphus resume {{item}} --config {shlex.quote(str(Path(config_path).resolve()))}"
    return f"{cmd} --base {shlex.quote(base)}" if base else cmd


def _resume_command(config_path: str | Path, item: str, base: str | None = None, *, again: bool = False) -> str:
    return fill_resume_template(_resume_template(config_path, base), item, again=again)


async def _resolve_options(
    config: SisyphusConfig, services: Services, base: str | None, config_path: str | Path = ""
) -> OrchestratorOptions:
    given = base or config.project.base_branch
    base = given or DEFAULT_BASE_BRANCH
    default_branch = await services.repo_info() if services.repo_info else None
    return OrchestratorOptions(
        base_branch=base,
        default_branch=default_branch,
        # The PR base stays `base`; only the intake clone prefers the repository default branch over "main".
        intake_base=given or default_branch or DEFAULT_BASE_BRANCH,
        lock_dir=services.lock_dir,
        resume_command=_resume_template(config_path, base) if config_path else None,
    )


# ------------------------------------------------------------------------ CLI


app = typer.Typer(
    name="sisyphus",
    help="Sisyphus: single-shot autonomous development run (cron or launchd friendly).",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
cli = app  # alias kept for dotharness.cli (``from dotharness.sisyphus.cli import cli as sisyphus``)

ConfigOption = Annotated[Path, typer.Option("--config", dir_okay=False, help=".sisyphus.toml path.")]
BaseOption = Annotated[
    str | None,
    typer.Option("--base", help=f"PR base branch (default: [project].base_branch, else {DEFAULT_BASE_BRANCH!r})."),
]
RefArgument = Annotated[str, typer.Argument(metavar="WORK_ITEM_REF")]


@dataclass
class _RunState:
    """What one ``run`` invocation has seen so far; rendered into the single merged digest."""

    results: list[RunResult] = field(default_factory=list)
    records: list[RunRecord] = field(default_factory=list)
    needs: list[NeedsYou] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    limits_hit: list[str] = field(default_factory=list)
    # Combined decision: rewrite checkpoint stop messages through the backend (off with `--raw` or the config switch).
    present: bool = True

    @property
    def has_content(self) -> bool:
        return bool(self.results or self.records or self.needs or self.notes or self.limits_hit)


def _waiting_need(rec: RunRecord) -> NeedsYou | None:
    need = needs_from_record(rec, read_stop_text(rec.path) or "")
    if need is None:
        return None
    return NeedsYou(
        need.item,
        need.checkpoint,
        f"Awaiting you. {need.question}",
        need.action,
        need.resume,
        need.text,
        need.record,
    )


def _resume_phase(config: SisyphusConfig, services: Services, state: _RunState, limit: int) -> list[Candidate]:
    """Stopped items whose blocker is cleared, oldest first, up to ``limit``; the rest are listed as awaiting you."""
    assert config.project.workspace_dir  # noqa: S101
    workspace = Path(config.project.workspace_dir).expanduser()
    now = services.now()
    ready: list[Candidate] = []
    for rec, stop in list_waiting(workspace):
        if not cleared(rec, stop, config, now):
            need = _waiting_need(rec)
            if need:
                state.needs.append(need)
        elif len(ready) >= limit:
            state.notes.append(f"{rec.item}: blocker cleared; resumes on a later run (max_tickets_per_run reached)")
        else:
            if CLEARS_BY.get(rec.checkpoint) == "retry":
                rec.increment_retry(AUTO_RETRY_KEY)
            ready.append(Candidate(rec.item))
    return ready


async def _pick_phase(config: SisyphusConfig, services: Services, state: _RunState) -> list[Candidate]:
    """Auto-resume cleared items, then pick new ones. The caller holds the clone lock.

    Items waiting at a checkpoint never stop picking. Resumed and new items share ``max_tickets_per_run``.
    """
    assert config.project.workspace_dir  # noqa: S101
    workspace = Path(config.project.workspace_dir).expanduser()
    limit = config.limits.max_tickets_per_run
    resumed = _resume_phase(config, services, state, limit)
    new = await pick_items(
        config.source.work_item,
        services.tracker,
        services.lookup,
        workspace_dir=workspace,
        archive_dir=resolve_archive_dir(config.archive.dir, config.project.workspace_dir),
        limit=limit - len(resumed),
    )
    return [*resumed, *new]


async def _run_one(
    cand: Candidate, config: SisyphusConfig, services: Services, options: OrchestratorOptions, state: _RunState
) -> None:
    assert config.project.workspace_dir  # noqa: S101
    try:
        result = await run_item(
            cand.ref,
            config,
            options,
            agent_runner=services.agent_runner,
            subprocess_runner=services.subprocess_runner,
            pr_client=services.pr_client,
            label_remover=services.label_remover,
            clock=services.clock,
            today=services.today,
            write_digest_file=False,
            lock_held=True,
        )
    except Exception as exc:
        workspace = Path(config.project.workspace_dir).expanduser()
        if record_path(workspace, cand.ref).exists():
            state.records.append(RunRecord.open(workspace, cand.ref))
        state.notes.append(f"{cand.ref}: run failed: {exc}")
        raise
    state.results.append(result)
    state.records.append(result.record)
    if result.checkpoint is None:
        typer.echo(_summarise(result))  # stop lines print at the end of the invocation, after the presentation step


async def _run_phase(
    picked: Sequence[Candidate],
    config: SisyphusConfig,
    services: Services,
    base: str | None,
    state: _RunState,
    config_path: str | Path = "",
) -> None:
    """Run each picked item (the caller holds the clone lock, so ``run_item`` is told not to), honouring max_run_minutes."""
    if not picked:
        return
    if not config.agent.backend:
        fail("config error: [agent] backend is not set")
    options = await _resolve_options(config, services, base, config_path)
    tracker = LimitTracker(config.limits, services.clock)
    for cand in picked:
        if "max_run_minutes" in tracker.status().reached:
            state.limits_hit.append(
                f"max_run_minutes ({config.limits.max_run_minutes}) reached before starting {cand.ref}"
            )
            return
        tracker.record_ticket()
        await _run_one(cand, config, services, options, state)


def _stopped_records(state: _RunState) -> list[StopRecord]:
    """Stop records of the items that stopped at a checkpoint in this invocation."""
    records: list[StopRecord] = []
    for result in state.results:
        if result.checkpoint is None:
            continue
        stop = read_stop_record(result.record.path)
        if stop is not None and not stop.abandoned and stop.checkpoint is not None:
            records.append(stop)
    return records


def _present_failure(result: PresentResult) -> str | None:
    if result.failure:
        return f"presentation failed: {result.failure}; showing raw stop text"
    if result.rejected:
        why = "; ".join(f"{item}: {reason}" for item, reason in result.rejected.items())
        return f"presentation rejected ({why}); showing raw stop text for those items"
    return None


async def _present_step(config: SisyphusConfig, services: Services, state: _RunState) -> None:
    """Restructure the stop texts of this invocation (one backend call) and write ``<enc>.stopped.md`` for each.

    Touches only workspace files and the backend. Never raises (except ``typer.Exit``) and never changes the stop.
    """
    if not state.present:
        return
    records = _stopped_records(state)
    if not records:
        return
    result = await present_stops(
        records,
        backend=config.agent.backend,
        launcher=services.present_launcher,
        timeout_minutes=config.limits.presentation_timeout_minutes,
    )
    paths = {r.item: r.record.path for r in state.results}
    for item, text in result.texts.items():
        try:
            write_stop_text(paths[item], text)
        except OSError as exc:
            typer.echo(f"warning: cannot write stop text for {item}: {exc}", err=True)
    line = _present_failure(result)
    if line:
        typer.echo(line, err=True)


def _echo_stops(state: _RunState) -> None:
    """Print one line per checkpoint stop: the restructured text when it exists on disk, else the raw summary."""
    for result in state.results:
        if result.checkpoint is None:
            continue
        text = read_stop_text(result.record.path)
        if text and text.strip():
            head = f"{result.item}: stopped at {result.checkpoint.value}:"
            typer.echo("\n".join([head, *indent_block(text, 2)]))
        else:
            typer.echo(_summarise(result))


def _write_digest(config: SisyphusConfig, services: Services, state: _RunState) -> DigestWriteError | None:
    # The orchestrator reports error stops only; each checkpoint stop gets exactly one entry here, with its text.
    paths = {r.item: r.record.path for r in state.results}
    stop_needs = tuple(
        needs_from_stop(stop, read_stop_text(paths[stop.item]) or "", str(stop_record_path(paths[stop.item])))
        for stop in _stopped_records(state)
    )
    report = _merge(
        [r.report for r in state.results],
        DigestReport(
            needs_you=(*stop_needs, *state.needs), deviations=tuple(state.notes), limits_hit=tuple(state.limits_hit)
        ),
    )
    try:
        path = write_digest(state.records, config, report, services.today)
    except DigestWriteError as exc:
        typer.echo(f"error: {exc}", err=True)
        return exc
    if state.results or state.needs:
        typer.echo(f"digest: {path}")
    return None


async def _phase_and_digest(
    config: SisyphusConfig, services: Services, state: _RunState, phase: Callable[[], Awaitable[None]]
) -> tuple[Exception | None, DigestWriteError | None]:
    """Run ``phase`` (an error is noted, not raised), then attempt the digest; returns both outcomes."""
    error: Exception | None = None
    try:
        await phase()
    except Exception as exc:
        error = exc
        if not state.notes and not isinstance(exc, typer.Exit):  # a typer.Exit already printed its message
            state.notes.append(f"run failed: {exc}")
    try:
        await _present_step(config, services, state)
    except typer.Exit:
        raise
    except Exception as exc:  # the stop is already written; a failed step only means raw text
        typer.echo(f"presentation failed: {type(exc).__name__}; showing raw stop text", err=True)
    _echo_stops(state)
    return error, _write_digest(config, services, state) if state.has_content else None


async def _run_locked(
    config: SisyphusConfig,
    services: Services,
    state: _RunState,
    phase: Callable[[], Awaitable[None]],
    prepare: Callable[[], None] | None = None,
) -> None:
    """Shared skeleton of ``run`` and ``resume``: one lock, ``prepare`` (errors propagate), ``phase``, digest, exit code.

    A lock held elsewhere exits 0 quietly. An error in ``phase`` is noted, the digest is still attempted, then the
    error maps to exit 1; a failed digest write alone also exits 1.
    """
    assert config.project.repo_dir  # noqa: S101
    try:
        with clone_lock(config.project.repo_dir, services.lock_dir):
            if prepare is not None:
                prepare()
            error, digest_error = await _phase_and_digest(config, services, state, phase)
    except LockHeld:
        return
    if isinstance(error, typer.Exit):
        raise error
    if error is not None:
        fail(str(error))
    if digest_error is not None:
        raise typer.Exit(1)


RawOption = Annotated[
    bool,
    typer.Option(
        "--raw",
        help="Show stop messages as written; make no presentation call and write no .stopped.md for this invocation.",
    ),
]


def present_enabled(config: SisyphusConfig, raw: bool) -> bool:
    """The one decision later wiring reads: present stops unless ``--raw`` or ``[presentation] present_stops = false``."""
    return config.presentation.present_stops and not raw


@app.command("run")
def run_command(ctx: typer.Context, config_path: ConfigOption, base: BaseOption = None, raw: RawOption = False) -> None:
    """Resume cleared stopped items, pick new ones (together at most max_tickets_per_run), run, write one digest."""
    factory: ServicesFactory | None = ctx.obj
    config = _load(config_path)
    _require(config, "repo_dir", "workspace_dir")
    assert config.project.repo_dir  # noqa: S101
    services = _services(factory, config)
    state = _RunState(present=present_enabled(config, raw))

    async def phase() -> None:
        picked = await _pick_phase(config, services, state)
        await _run_phase(picked, config, services, base, state, config_path)

    # One non-blocking lock for the whole invocation: pick, every item, digest.
    asyncio.run(_run_locked(config, services, state, phase))


@app.command("intake")
def intake_command(ctx: typer.Context, work_item_ref: RefArgument, config_path: ConfigOption) -> None:
    """Draft the brief for WORK_ITEM_REF (intake stage only) and report any open decisions."""
    factory: ServicesFactory | None = ctx.obj
    config = _load(config_path)
    _require(config, "repo_dir", "workspace_dir", "brief_path_template")
    assert config.project.repo_dir and config.project.workspace_dir  # noqa: S101
    services = _services(factory, config)
    if not config.agent.backend and factory is None:
        fail("config error: [agent] backend is not set")
    try:
        with clone_lock(config.project.repo_dir, services.lock_dir):
            record = RunRecord.open(Path(config.project.workspace_dir).expanduser(), work_item_ref)
            result = asyncio.run(run_intake(work_item_ref, config, services.agent_runner, record=record))
    except LockHeld:
        return
    except Exception as exc:
        fail(str(exc))
    rec_path = record_path(Path(config.project.workspace_dir).expanduser(), work_item_ref)
    if result.blocked:
        texts = tuple(f"{d.number}. {d.text}" for d in result.open_decisions)
        write_stop_record(
            rec_path,
            build_stop_record(
                work_item_ref,
                checkpoint="C1",
                stage=INTAKE_STAGE,
                stage_status="complete",
                detail=f"open decisions in {result.brief_path}: " + "; ".join(texts),
                resume_command=_resume_command(config_path, work_item_ref),
                questions=texts,
                brief_path=str(result.brief_path),
            ),
        )
    else:
        clear_stop_record(rec_path)
    typer.echo(f"brief: {result.brief_path}")
    for decision in result.open_decisions:
        typer.echo(f"open decision (C1): {decision}")


def _check_resumable(rec: RunRecord, again: bool) -> None:
    """Exit 1 unless ``rec`` is stopped (or, with ``--again``, abandoned; the abandoned mark is then cleared)."""
    stop = read_stop_record(rec.path)
    if is_abandoned(stop):
        if not again:
            fail(
                f"{rec.item} is abandoned; run `sisyphus resume {shlex.quote(rec.item)} --config <config> --again` "
                "to re-enable it"
            )
        clear_stop_record(rec.path)
    elif again:
        fail(f"{rec.item} is not abandoned; --again only re-enables an abandoned item")
    elif not is_stopped(rec, stop):
        fail(f"{rec.item} is not stopped (stage {rec.stage}, {rec.stage_status}); nothing to resume")


def _prepare_resume(
    config: SisyphusConfig, services: Services, work_item_ref: str, *, again: bool, reset_retries: bool
) -> None:
    """Under the lock: require a resumable record, then optionally zero the recorded stage's retry counters."""
    assert config.project.workspace_dir  # noqa: S101
    workspace = Path(config.project.workspace_dir).expanduser()
    if not record_path(workspace, work_item_ref).exists():
        fail(f"no run record for {work_item_ref}; nothing to resume")
    rec = RunRecord.open(workspace, work_item_ref)
    _check_resumable(rec, again)
    if reset_retries:
        changed = reset_stage_retries(rec, services.now())
        typer.echo(
            f"{work_item_ref}: reset retries at stage {rec.stage or 'none'}: "
            + (", ".join(f"{k} {v}->0" for k, v in changed.items()) or "nothing to reset")
        )


@app.command("resume")
def resume_item_command(
    ctx: typer.Context,
    work_item_ref: RefArgument,
    config_path: ConfigOption,
    base: BaseOption = None,
    raw: RawOption = False,
    again: Annotated[
        bool, typer.Option("--again", help="Re-enable an abandoned item: clear the abandoned mark, then resume.")
    ] = False,
    reset_retries: Annotated[
        bool,
        typer.Option(
            "--reset-retries",
            help="Zero the retry counters of the item's recorded stage (see docs/usage.md for the exact keys) before resuming.",
        ),
    ] = False,
) -> None:
    """Resume a stopped WORK_ITEM_REF from its recorded stage (explicit; no need to wait for the blocker check).

    --reset-retries (explicit only, never done by `run`) first gives the recorded stage a fresh retry budget.
    """
    factory: ServicesFactory | None = ctx.obj
    config = _load(config_path)
    _require(config, "repo_dir", "workspace_dir")
    assert config.project.repo_dir and config.project.workspace_dir  # noqa: S101
    services = _services(factory, config)
    state = _RunState(present=present_enabled(config, raw))

    def prepare() -> None:
        _prepare_resume(config, services, work_item_ref, again=again, reset_retries=reset_retries)

    async def phase() -> None:
        await _run_phase([Candidate(work_item_ref)], config, services, base, state, config_path)

    asyncio.run(_run_locked(config, services, state, phase, prepare))


@app.command("abandon")
def abandon_command(ctx: typer.Context, work_item_ref: RefArgument, config_path: ConfigOption) -> None:
    """Abandon WORK_ITEM_REF: the explicit way out of a dead end (C7 and others).

    Marks the item abandoned and removes its waiting state so it no longer blocks auto-resume or picking.
    Touches only Sisyphus's own state files, plus one remote effect: for a GitHub issue ref (``#N``) with a
    ``pick_label`` and ``remove_label_when_settled`` on, the pick label is now removed from the issue (a failure
    is reported on stderr and never changes the exit code). Never the remote branch, any PR, the local branch,
    the run record or the digest, and never rewrites history.
    """
    factory: ServicesFactory | None = ctx.obj
    config = _load(config_path)
    _require(config, "repo_dir", "workspace_dir")
    assert config.project.repo_dir and config.project.workspace_dir  # noqa: S101
    services = _services(factory, config)
    try:
        with clone_lock(config.project.repo_dir, services.lock_dir):
            workspace = Path(config.project.workspace_dir).expanduser()
            if not record_path(workspace, work_item_ref).exists():
                fail(f"no run record for {work_item_ref}; nothing to abandon")
            rec = RunRecord.open(workspace, work_item_ref)
            if is_abandoned(read_stop_record(rec.path)):
                typer.echo(f"{work_item_ref}: already abandoned")
                return
            prior = rec.checkpoint or ("an error" if read_stop_record(rec.path) else "no stop")
            write_stop_record(
                rec.path,
                build_abandoned_record(
                    work_item_ref,
                    stage=rec.stage,
                    stage_status=rec.stage_status,
                    prior_label=prior,
                    reenable_command=_resume_command(config_path, work_item_ref, again=True),
                    now=services.now(),
                ),
            )
            if rec.checkpoint is not None:
                rec.set_checkpoint(None)
            asyncio.run(_remove_pick_label(config, services, work_item_ref))
    except LockHeld:
        return
    typer.echo(
        f"{work_item_ref}: abandoned (branch, any PR and the run record left as they are; "
        "the issue's pick label is removed for a GitHub item when remove_label_when_settled is on)"
    )


async def _remove_pick_label(config: SisyphusConfig, services: Services, ref: str) -> None:
    """Remove the pick label from a settled GitHub issue; any failure is one stderr line, never an exit code."""
    number = label_removal_number(config, ref)
    wi = config.source.work_item
    if number is None or wi is None or not wi.pick_label or services.label_remover is None:
        return
    try:
        await services.label_remover.remove_label(number, wi.pick_label)
    except Exception as exc:  # label removal is best effort; abandon must still succeed
        detail = " ".join(str(exc).split())  # collapse multi-line gh stderr so this stays one line
        typer.echo(f"{ref}: pick label '{wi.pick_label}' not removed: {type(exc).__name__}: {detail}", err=True)


def _archive_one(config: SisyphusConfig, rec: RunRecord, archive_dir: Path) -> bool | None:
    """Archive one settled item. True: moved; False: refused or failed (reported on stderr); None: nothing to move."""
    try:
        result = archive_item(config, rec, archive_dir)
    except OSError as exc:
        typer.echo(f"{rec.item}: not archived: {exc}", err=True)
        return False
    for warning in result.warnings:
        typer.echo(f"warning: {warning}", err=True)
    if result.collision is not None:
        typer.echo(f"{rec.item}: not archived: {result.collision.name} already exists in {archive_dir}", err=True)
        return False
    if not result.moved:
        return None
    typer.echo(f"{rec.item} -> {archive_dir}")
    return True


@app.command("archive")
def archive_command(ctx: typer.Context, config_path: ConfigOption) -> None:
    """Move every settled item's files out of the workspace into the archive directory ([archive] dir).

    Settled is the same predicate `status` uses to hide items. In-progress, stopped and checkpointed
    items are never moved. Nothing is deleted and no PR lookup is made. A name that already
    exists in the archive refuses that item (reported on stderr, exit 1); the other items still move.
    """
    factory: ServicesFactory | None = ctx.obj
    config = _load(config_path)
    _require(config, "repo_dir", "workspace_dir")
    assert config.project.repo_dir and config.project.workspace_dir  # noqa: S101
    try:
        archive_dir = resolve_archive_dir(config.archive.dir, config.project.workspace_dir)
        check_archive_dir(archive_dir, config.project.workspace_dir)
    except ConfigError as exc:
        fail(f"config error: {exc}")
    services = _services(factory, config)
    workspace = Path(config.project.workspace_dir).expanduser()
    archived = 0
    refused = False
    try:
        with clone_lock(config.project.repo_dir, services.lock_dir):
            for rec in iter_records(workspace):
                if is_settled(rec, read_stop_record(rec.path)):
                    outcome = _archive_one(config, rec, archive_dir)
                    archived += outcome is True
                    refused = refused or outcome is False
    except LockHeld:
        return
    if archived:
        typer.echo(f"archived {archived} item(s)")
    if refused:
        raise typer.Exit(1)


def _need_lines(
    rec: RunRecord, stop: Any, need: NeedsYou, config: SisyphusConfig, config_path: str | Path
) -> list[str]:
    """Question, action, resume command and auto-resume mode for an item that is waiting on someone."""
    lines = [f"  question: {need.question}"]
    lines.extend(indent_block(need.text, 4))
    if need.action:
        lines.append(f"  action: {need.action}")
    lines.append(f"  resume: {need.resume or _resume_command(config_path, rec.item)}")
    how = "explicit"
    if rec.checkpoint is not None:
        how = stop.clears_by if stop else CLEARS_BY.get(rec.checkpoint, "explicit")
    done = ""
    if how != "explicit":
        is_cleared = cleared(rec, stop, config, datetime.now(UTC))
        done = f" (cleared now: {'yes' if is_cleared else 'no'})"
    lines.append(f"  auto-resume: {how}{done}")
    return lines


def _status_block(rec: RunRecord, awaiting: str, config: SisyphusConfig, config_path: str | Path) -> list[str]:
    stop = read_stop_record(rec.path)
    need = needs_from_record(rec, read_stop_text(rec.path) or "")
    lines = [
        rec.item,
        f"  stage: {rec.stage or 'none'}",
        f"  stage_status: {rec.stage_status or 'none'}",
        f"  checkpoint: {rec.checkpoint or 'none'}",
        f"  awaiting: {awaiting}",
    ]
    if stop is not None and stop.abandoned:
        lines.insert(1, "  state: abandoned")
        lines.append(f"  abandoned_at: {stop.stopped_at}")
        lines.append(f"  re-enable: {stop.resume_command}")
    lines.extend(f"  {describe_reset(rec.item, e)}" for e in rec.resets)
    if need is not None:
        lines.extend(_need_lines(rec, stop, need, config, config_path))
    return lines


async def _pr_closed(rec: RunRecord, stop: Any, pr_state: Callable[[int], Awaitable[str]]) -> bool:
    """True if the record's PR is MERGED or CLOSED. Fails open: any lookup failure warns on stderr and returns False."""
    number = rec.retries.get(PR_NUMBER_KEY)
    if not number:
        return False
    if rec.checkpoint is None and stop is None and rec.stage_status == STAGE_IN_PROGRESS:
        return False  # in progress with no stop record: may be running now, never hidden or looked up
    try:
        return await pr_state(number) in ("MERGED", "CLOSED")
    except (PickError, OSError, ValueError) as exc:
        typer.echo(f"warning: cannot check PR #{number} for {rec.item}: {exc}", err=True)
        return False


def _awaiting(rec: RunRecord, stop: Any) -> str:
    if rec.checkpoint is not None:
        return "you"
    return "error" if stop is not None and not is_abandoned(stop) else "none"


async def _is_hidden(
    rec: RunRecord, stop: Any, pr_state: Callable[[int], Awaitable[str]] | None, *, list_all: bool
) -> bool:
    """True if `status` hides ``rec``: settled, or (with ``pr_state``) its PR is merged or closed."""
    if not list_all and is_settled(rec, stop):
        return True
    return pr_state is not None and await _pr_closed(rec, stop, pr_state)


@app.command("status")
def status_command(
    ctx: typer.Context,
    config_path: ConfigOption,
    work_item_ref: Annotated[str | None, typer.Argument(metavar="[WORK_ITEM_REF]")] = None,
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Show every item. By default settled items (abandoned, finished) are hidden."),
    ] = False,
    check_prs: Annotated[
        bool,
        typer.Option(
            "--check-prs",
            help="Query GitHub for each shown item's recorded PR and hide items whose PR is merged or closed.",
        ),
    ] = False,
) -> None:
    """Show stage, checkpoint, question, action and resume command per item (read-only; no lock, no agent).

    Settled items (abandoned, or finished with no stop) are hidden unless --all or a WORK_ITEM_REF is given.
    With --check-prs, items whose recorded PR is merged or closed are hidden too (one gh lookup each, 10 s timeout).
    """
    factory: ServicesFactory | None = ctx.obj
    config = _load(config_path)
    _require(config, "workspace_dir")
    assert config.project.workspace_dir  # noqa: S101
    workspace = Path(config.project.workspace_dir).expanduser()
    if work_item_ref is not None and not record_path(workspace, work_item_ref).exists():
        fail(f"no run record for {work_item_ref}")
    records = [r for r in iter_records(workspace) if work_item_ref in (None, r.item)]
    if not records:
        typer.echo("no items")
        return
    pr_state = _services(factory, config).pr_state if check_prs and not show_all and work_item_ref is None else None

    async def render() -> int:
        hidden = 0
        for rec in records:
            stop = read_stop_record(rec.path)
            if await _is_hidden(rec, stop, pr_state, list_all=show_all or work_item_ref is not None):
                hidden += 1
                continue
            typer.echo("\n".join(_status_block(rec, _awaiting(rec, stop), config, config_path)))
        return hidden

    hidden = asyncio.run(render())
    if hidden == len(records):
        typer.echo(f"no items needing attention ({hidden} hidden; use --all)")


__all__ = ["Services", "app", "cli", "default_services", "make_agent_runner"]
