"""Single-shot ``sisyphus`` CLI (D-17): ``sisyphus run`` and ``sisyphus intake``.

One invocation, one config, no daemon. ``run`` takes the per-clone lock non-blockingly once, holds it for the whole invocation, skips picking
when an earlier Sisyphus PR still awaits review, otherwise picks up to ``max_tickets_per_run`` items,
runs each through the orchestrator and writes one merged digest. Credentials come only from the
inherited environment and ``[source.host].token_cmd``.

Everything external is built by a services factory (``main(obj=factory)`` in tests), so no test runs a
real agent, ``gh``, ``git push`` or the harness. The harness is reached only through the orchestrator's
subprocess calls; this module never imports it.

Exit codes: 0 for success, nothing to do, lock held, or a run stopped at a checkpoint (its digest is
written, the checkpoint is in the Needs-you section); 1 for an unhandled error, a config error or a
digest that cannot be written (message on stderr).
"""

# ruff: noqa: TRY003
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from sisyphus import gaterecord
from sisyphus.cleanup import PrInfo
from sisyphus.config import ConfigError, SisyphusConfig, load_config
from sisyphus.digest import DigestReport, DigestWriteError, NeedsYou, describe_reset, needs_from_record, write_digest
from sisyphus.intake import AgentRunner, run_intake
from sisyphus.lock import LockHeld, clone_lock
from sisyphus.orchestrator import (
    GitHubError,
    OrchestratorOptions,
    PullRequest,
    PullRequestClient,
    RunResult,
    SubprocessRunner,
    default_subprocess_runner,
    run_item,
)
from sisyphus.pick import (
    Candidate,
    GhCli,
    GhPullRequestLookup,
    GitHubIssueTracker,
    LinearTracker,
    PickError,
    PullRequestLookup,
    TrackerClient,
    find_awaiting_review,
    pick_items,
)
from sisyphus.rails import PERMISSION_FLAG, LimitTracker
from sisyphus.resume import (
    AUTO_RETRY_KEY,
    cleared,
    is_stopped,
    iter_records,
    list_waiting,
    reset_stage_retries,
)
from sisyphus.runrecord import RunRecord, record_path
from sisyphus.stages import INTAKE_STAGE
from sisyphus.stoprecord import (
    CLEARS_BY,
    build_abandoned_record,
    build_stop_record,
    clear_stop_record,
    fill_resume_template,
    is_abandoned,
    read_stop_record,
    write_stop_record,
)

AGENT_COMMANDS = {
    "claude": lambda prompt: ["claude", "-p", prompt, PERMISSION_FLAG],
    "opencode": lambda prompt: ["opencode", "run", prompt],
}
TOKEN_CMD_TIMEOUT = 30
DEFAULT_BASE_BRANCH = "main"
AWAITING_REVIEW = "awaiting-review"


class AgentError(Exception):
    """The agent backend exited non-zero or could not be started."""


# ------------------------------------------------------------- real services


def run_token_cmd(token_cmd: str | None, run: Callable[..., Any] = subprocess.run) -> str | None:
    """Run ``[source.host].token_cmd`` (no shell) and return its stripped stdout; ``None`` when unset."""
    if not token_cmd:
        return None
    try:
        proc = run(shlex.split(token_cmd), capture_output=True, text=True, check=False, timeout=TOKEN_CMD_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise ConfigError(f"[source.host] token_cmd failed to run: {exc}") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ConfigError(f"[source.host] token_cmd failed (exit {proc.returncode}) or printed no token")
    return proc.stdout.strip()


def make_agent_runner(
    backend: str | None,
    cwd: Path,
    run: Callable[..., Any] = subprocess.run,
    timeout_minutes: float | None = None,
) -> AgentRunner:
    """Subprocess wrapper for the configured agent backend, run in ``cwd`` (the dedicated clone).

    ``claude`` runs ``claude -p <prompt> --dangerously-skip-permissions``; ``opencode`` runs
    ``opencode run <prompt>`` (D-11). A non-zero exit or a run past ``timeout_minutes`` raises
    :class:`AgentError`, so a hung agent stops at a checkpoint and the clone lock is released.
    """
    if backend not in AGENT_COMMANDS:
        raise ConfigError("[agent] backend must be set to 'claude' or 'opencode' to run an agent")
    build = AGENT_COMMANDS[backend]

    def runner(prompt: str) -> None:
        try:
            proc = run(build(prompt), cwd=cwd, check=False, timeout=timeout_minutes * 60 if timeout_minutes else None)
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f"the {backend} backend timed out after {timeout_minutes} minutes") from exc
        except OSError as exc:
            raise AgentError(f"cannot start the {backend} backend: {exc}") from exc
        if proc.returncode != 0:
            raise AgentError(f"the {backend} backend exited with status {proc.returncode}")

    return runner


_PR_URL = re.compile(r"/pull/(\d+)")


class GhPullRequestClient:
    """Real :class:`PullRequestClient`: ``gh pr list`` / ``gh pr create`` in the dedicated clone."""

    def __init__(self, gh: GhCli) -> None:
        self.gh = gh

    def find_pr(self, branch: str) -> PullRequest | None:
        try:
            rows = self.gh.json("pr", "list", "--head", branch, "--state", "open", "--json", "number,url")
        except PickError as exc:
            raise GitHubError(str(exc)) from exc
        for row in rows or []:
            return PullRequest(int(row["number"]), row.get("url", ""))
        return None

    def pr_state(self, ref: str) -> PrInfo | None:
        """``gh pr view <number or branch> --json number,state,headRefOid``; any state (used by branch cleanup)."""
        try:
            row = self.gh.json("pr", "view", ref, "--json", "number,state,headRefOid")
        except PickError as exc:
            raise GitHubError(str(exc)) from exc
        return _pr_info(row) if isinstance(row, dict) else None

    def open_pr(self, branch: str, base: str, title: str, body: str) -> PullRequest:
        res = self.gh.run("pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body)
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
    state_dir: Path | None = None
    today: datetime | None = None
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    repo_info: Callable[[], tuple[str, str | None]] | None = field(default=None)


ServicesFactory = Callable[[SisyphusConfig], Services]


def _no_backend(_prompt: str) -> None:
    raise ConfigError("[agent] backend is not set")


def default_services(config: SisyphusConfig) -> Services:
    """Real services for ``config``: ``gh``, Linear, the configured agent backend and ``git``."""
    repo_dir = Path(config.project.repo_dir or ".").expanduser()
    token = run_token_cmd(config.source.host.token_cmd if config.source.host else None)
    gh = GhCli(repo_dir, token)
    work_item = config.source.work_item
    if work_item is not None and work_item.kind == "linear":
        tracker: TrackerClient = LinearTracker(token or os.environ.get("LINEAR_API_KEY", ""))
    else:
        tracker = GitHubIssueTracker(gh)
    runner = (
        make_agent_runner(config.agent.backend, repo_dir, timeout_minutes=config.limits.max_run_minutes)
        if config.agent.backend
        else _no_backend
    )

    def repo_info() -> tuple[str, str | None]:
        """``(repo_slug, default_branch)`` from ``gh repo view``; falls back to the path-based slug."""
        try:
            data = gh.json("repo", "view", "--json", "nameWithOwner,defaultBranchRef")
            slug = gaterecord.repo_slug_from_name(data["nameWithOwner"])
            default = (data.get("defaultBranchRef") or {}).get("name")
        except (PickError, KeyError, TypeError):
            return gaterecord.fallback_repo_slug(repo_dir), None
        return slug, default

    return Services(
        tracker=tracker,
        lookup=GhPullRequestLookup(gh),
        pr_client=GhPullRequestClient(gh),
        agent_runner=runner,
        repo_info=repo_info,
    )


# --------------------------------------------------------------------- helpers


def _fail(message: str) -> click.ClickException:
    return click.ClickException(message)


def _load(config_path: str) -> SisyphusConfig:
    try:
        return load_config(config_path)
    except ConfigError as exc:
        raise _fail(f"config error: {exc}") from exc


def _require(config: SisyphusConfig, *keys: str) -> None:
    for key in keys:
        if not getattr(config.project, key):
            raise _fail(f"config error: [project] {key} is required")


def _services(factory: ServicesFactory | None, config: SisyphusConfig) -> Services:
    try:
        return (factory or default_services)(config)
    except (ConfigError, PickError) as exc:
        raise _fail(f"config error: {exc}") from exc


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
        return f"{result.item}: PR #{result.pr.number} {result.pr.url}".rstrip()
    return f"{result.item}: done"


def _resume_template(config_path: str, base: str | None) -> str:
    cmd = f"sisyphus resume {{item}} --config {shlex.quote(str(Path(config_path).resolve()))}"
    return f"{cmd} --base {shlex.quote(base)}" if base else cmd


def _resume_command(config_path: str, item: str, base: str | None = None, *, again: bool = False) -> str:
    return fill_resume_template(_resume_template(config_path, base), item, again=again)


def _resolve_options(
    config: SisyphusConfig, services: Services, base: str | None, config_path: str = ""
) -> OrchestratorOptions:
    base = base or config.project.base_branch or DEFAULT_BASE_BRANCH
    slug, default_branch = (
        services.repo_info()
        if services.repo_info
        else (gaterecord.fallback_repo_slug(config.project.repo_dir or "."), None)
    )
    return OrchestratorOptions(
        base_branch=base,
        repo_slug=slug,
        default_branch=default_branch,
        ci_fix=config.source.ci is not None,
        state_dir=services.state_dir,
        lock_dir=services.lock_dir,
        resume_command=_resume_template(config_path, base) if config_path else None,
    )


# ------------------------------------------------------------------------ CLI


@click.group()
def main() -> None:
    """Sisyphus: single-shot autonomous development run (cron or launchd friendly)."""


config_option = click.option(
    "--config", "config_path", required=True, type=click.Path(dir_okay=False), help=".sisyphus.toml path."
)


@dataclass
class _RunState:
    """What one ``run`` invocation has seen so far; rendered into the single merged digest."""

    results: list[RunResult] = field(default_factory=list)
    records: list[RunRecord] = field(default_factory=list)
    needs: list[NeedsYou] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    limits_hit: list[str] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return bool(self.results or self.records or self.needs or self.notes or self.limits_hit)


def _waiting_need(rec: RunRecord) -> NeedsYou | None:
    need = needs_from_record(rec)
    if need is None:
        return None
    return NeedsYou(need.item, need.checkpoint, f"Awaiting you. {need.question}", need.action, need.resume)


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


def _pick_phase(config: SisyphusConfig, services: Services, state: _RunState) -> list[Candidate]:
    """Auto-resume cleared items, then (unless a PR awaits review) pick new ones. The caller holds the clone lock.

    Items waiting at a checkpoint never stop picking. Resumed and new items share ``max_tickets_per_run``.
    """
    assert config.project.workspace_dir  # noqa: S101
    workspace = Path(config.project.workspace_dir).expanduser()
    limit = config.limits.max_tickets_per_run
    resumed = _resume_phase(config, services, state, limit)
    awaiting = find_awaiting_review(workspace, services.pr_client.find_pr)
    for a in awaiting:
        state.needs.append(
            NeedsYou(
                a.item,
                AWAITING_REVIEW,
                f"PR #{a.pr.number} {a.pr.url} from an earlier run still awaits review, so no new item "
                "was picked. Review or merge it.",
            )
        )
    if awaiting:
        return resumed
    new = pick_items(
        config.source.work_item,
        services.tracker,
        services.lookup,
        workspace_dir=workspace,
        limit=limit - len(resumed),
    )
    return [*resumed, *new]


def _run_one(
    cand: Candidate, config: SisyphusConfig, services: Services, options: OrchestratorOptions, state: _RunState
) -> None:
    assert config.project.workspace_dir  # noqa: S101
    try:
        result = run_item(
            cand.ref,
            config,
            options,
            agent_runner=services.agent_runner,
            subprocess_runner=services.subprocess_runner,
            pr_client=services.pr_client,
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
    click.echo(_summarise(result))


def _run_phase(
    picked: Sequence[Candidate],
    config: SisyphusConfig,
    services: Services,
    base: str | None,
    state: _RunState,
    config_path: str = "",
) -> None:
    """Run each picked item (the caller holds the clone lock, so ``run_item`` is told not to), honouring max_run_minutes."""
    if not picked:
        return
    if not config.agent.backend:
        raise _fail("config error: [agent] backend is not set")
    options = _resolve_options(config, services, base, config_path)
    tracker = LimitTracker(config.limits, services.clock)
    for cand in picked:
        if "max_run_minutes" in tracker.status().reached:
            state.limits_hit.append(
                f"max_run_minutes ({config.limits.max_run_minutes}) reached before starting {cand.ref}"
            )
            return
        tracker.record_ticket()
        _run_one(cand, config, services, options, state)


def _write_digest(config: SisyphusConfig, services: Services, state: _RunState) -> DigestWriteError | None:
    report = _merge(
        [r.report for r in state.results],
        DigestReport(needs_you=tuple(state.needs), deviations=tuple(state.notes), limits_hit=tuple(state.limits_hit)),
    )
    try:
        path = write_digest(state.records, config, report, services.today)
    except DigestWriteError as exc:
        click.echo(f"error: {exc}", err=True)
        return exc
    if state.results or state.needs:
        click.echo(f"digest: {path}")
    return None


def _phase_and_digest(
    config: SisyphusConfig, services: Services, state: _RunState, phase: Callable[[], None]
) -> tuple[Exception | None, DigestWriteError | None]:
    """Run ``phase`` (an error is noted, not raised), then attempt the digest; returns both outcomes."""
    error: Exception | None = None
    try:
        phase()
    except Exception as exc:
        error = exc
        if not state.notes:
            state.notes.append(f"run failed: {exc}")
    return error, _write_digest(config, services, state) if state.has_content else None


def _run_locked(
    config: SisyphusConfig,
    services: Services,
    state: _RunState,
    phase: Callable[[], None],
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
            error, digest_error = _phase_and_digest(config, services, state, phase)
    except LockHeld:
        return
    if error is not None:
        raise _fail(str(error)) from error
    if digest_error is not None:
        raise click.exceptions.Exit(1)


@main.command("run")
@config_option
@click.option(
    "--base", default=None, help=f"PR base branch (default: [project].base_branch, else {DEFAULT_BASE_BRANCH!r})."
)
@click.pass_obj
def run_command(factory: ServicesFactory | None, config_path: str, base: str | None) -> None:
    """Resume cleared stopped items, pick new ones (together at most max_tickets_per_run), run, write one digest."""
    config = _load(config_path)
    _require(config, "repo_dir", "workspace_dir")
    assert config.project.repo_dir  # noqa: S101
    services = _services(factory, config)
    state = _RunState()

    def phase() -> None:
        picked = _pick_phase(config, services, state)
        _run_phase(picked, config, services, base, state, config_path)

    # One non-blocking lock for the whole invocation: awaiting-review check, pick, every item, digest.
    _run_locked(config, services, state, phase)


@main.command("intake")
@click.argument("work_item_ref")
@config_option
@click.pass_obj
def intake_command(factory: ServicesFactory | None, work_item_ref: str, config_path: str) -> None:
    """Draft the brief for WORK_ITEM_REF (intake stage only) and report any open decisions."""
    config = _load(config_path)
    _require(config, "repo_dir", "workspace_dir", "brief_path_template")
    assert config.project.repo_dir and config.project.workspace_dir  # noqa: S101
    services = _services(factory, config)
    if not config.agent.backend and factory is None:
        raise _fail("config error: [agent] backend is not set")
    try:
        with clone_lock(config.project.repo_dir, services.lock_dir):
            record = RunRecord.open(Path(config.project.workspace_dir).expanduser(), work_item_ref)
            result = run_intake(work_item_ref, config, services.agent_runner, record=record)
    except LockHeld:
        return
    except Exception as exc:
        raise _fail(str(exc)) from exc
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
    click.echo(f"brief: {result.brief_path}")
    for decision in result.open_decisions:
        click.echo(f"open decision (C1): {decision}")


def _check_resumable(rec: RunRecord, again: bool) -> None:
    """Exit 1 unless ``rec`` is stopped (or, with ``--again``, abandoned; the abandoned mark is then cleared)."""
    stop = read_stop_record(rec.path)
    if is_abandoned(stop):
        if not again:
            raise _fail(
                f"{rec.item} is abandoned; run `sisyphus resume {shlex.quote(rec.item)} --config <config> --again` "
                "to re-enable it"
            )
        clear_stop_record(rec.path)
    elif again:
        raise _fail(f"{rec.item} is not abandoned; --again only re-enables an abandoned item")
    elif not is_stopped(rec, stop):
        raise _fail(f"{rec.item} is not stopped (stage {rec.stage}, {rec.stage_status}); nothing to resume")


@main.command("resume")
@click.argument("work_item_ref")
@config_option
@click.option("--base", default=None, help="PR base branch (default: [project].base_branch, else 'main').")
@click.option("--again", is_flag=True, help="Re-enable an abandoned item: clear the abandoned mark, then resume.")
@click.option(
    "--reset-retries",
    is_flag=True,
    help="Zero the retry counters of the item's recorded stage (see docs/usage.md for the exact keys) before resuming.",
)
@click.pass_obj
def resume_item_command(
    factory: ServicesFactory | None,
    work_item_ref: str,
    config_path: str,
    base: str | None,
    again: bool,
    reset_retries: bool,
) -> None:
    """Resume a stopped WORK_ITEM_REF from its recorded stage (explicit; no need to wait for the blocker check).

    --reset-retries (explicit only, never done by `run`) first gives the recorded stage a fresh retry budget.
    """
    config = _load(config_path)
    _require(config, "repo_dir", "workspace_dir")
    assert config.project.repo_dir and config.project.workspace_dir  # noqa: S101
    services = _services(factory, config)
    state = _RunState()

    def prepare() -> None:
        assert config.project.workspace_dir  # noqa: S101
        workspace = Path(config.project.workspace_dir).expanduser()
        if not record_path(workspace, work_item_ref).exists():
            raise _fail(f"no run record for {work_item_ref}; nothing to resume")
        rec = RunRecord.open(workspace, work_item_ref)
        _check_resumable(rec, again)
        if reset_retries:
            changed = reset_stage_retries(rec, services.now())
            click.echo(
                f"{work_item_ref}: reset retries at stage {rec.stage or 'none'}: "
                + (", ".join(f"{k} {v}->0" for k, v in changed.items()) or "nothing to reset")
            )

    def phase() -> None:
        _run_phase([Candidate(work_item_ref)], config, services, base, state, config_path)

    _run_locked(config, services, state, phase, prepare)


@main.command("abandon")
@click.argument("work_item_ref")
@config_option
@click.pass_obj
def abandon_command(factory: ServicesFactory | None, work_item_ref: str, config_path: str) -> None:
    """Abandon WORK_ITEM_REF: the explicit way out of a dead end (C7 and others).

    Marks the item abandoned and removes its waiting state so it no longer blocks auto-resume or picking.
    Touches only Sisyphus's own state files: never the remote branch, any PR, the local branch, the run
    record or the digest, and never rewrites history.
    """
    config = _load(config_path)
    _require(config, "repo_dir", "workspace_dir")
    assert config.project.repo_dir and config.project.workspace_dir  # noqa: S101
    services = _services(factory, config)
    try:
        with clone_lock(config.project.repo_dir, services.lock_dir):
            workspace = Path(config.project.workspace_dir).expanduser()
            if not record_path(workspace, work_item_ref).exists():
                raise _fail(f"no run record for {work_item_ref}; nothing to abandon")
            rec = RunRecord.open(workspace, work_item_ref)
            if is_abandoned(read_stop_record(rec.path)):
                click.echo(f"{work_item_ref}: already abandoned")
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
    except LockHeld:
        return
    click.echo(f"{work_item_ref}: abandoned (branch, any PR and the run record left as they are)")


def _status_block(rec: RunRecord, awaiting: str, config: SisyphusConfig, config_path: str) -> list[str]:
    stop = read_stop_record(rec.path)
    need = needs_from_record(rec)
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
        lines.append(f"  question: {need.question}")
        if need.action:
            lines.append(f"  action: {need.action}")
        resume = need.resume or _resume_command(config_path, rec.item)
        lines.append(f"  resume: {resume}")
        how = stop.clears_by if stop else CLEARS_BY.get(rec.checkpoint, "explicit")
        if rec.checkpoint is None:
            how = "explicit"
        done = ""
        if how != "explicit":
            is_cleared = cleared(rec, stop, config, datetime.now(UTC))
            done = f" (cleared now: {'yes' if is_cleared else 'no'})"
        lines.append(f"  auto-resume: {how}{done}")
    return lines


@main.command("status")
@click.argument("work_item_ref", required=False)
@config_option
@click.pass_obj
def status_command(factory: ServicesFactory | None, work_item_ref: str | None, config_path: str) -> None:
    """Show stage, checkpoint, question, action and resume command per item (read-only; no lock, no agent)."""
    config = _load(config_path)
    _require(config, "workspace_dir")
    assert config.project.workspace_dir  # noqa: S101
    workspace = Path(config.project.workspace_dir).expanduser()
    if work_item_ref is not None and not record_path(workspace, work_item_ref).exists():
        raise _fail(f"no run record for {work_item_ref}")
    records = [r for r in iter_records(workspace) if work_item_ref in (None, r.item)]
    if not records:
        click.echo("no items")
        return
    try:
        reviews: set[str] | None = {
            a.item for a in find_awaiting_review(workspace, _services(factory, config).pr_client.find_pr)
        }
    except Exception:
        reviews = None
    for rec in records:
        if rec.checkpoint is not None:
            awaiting = "you"
        elif is_abandoned(stop := read_stop_record(rec.path)):
            awaiting = "none"
        elif stop is not None:
            awaiting = "error"
        elif reviews is None:
            awaiting = "unknown (PR lookup failed)"
        else:
            awaiting = "review" if rec.item in reviews else "none"
        click.echo("\n".join(_status_block(rec, awaiting, config, config_path)))


__all__ = ["Services", "default_services", "main", "make_agent_runner"]
