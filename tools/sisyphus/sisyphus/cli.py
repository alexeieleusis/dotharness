"""Single-shot ``sisyphus`` CLI (D-17): ``sisyphus run`` and ``sisyphus intake``.

One invocation, one config, no daemon. ``run`` takes the per-clone lock non-blockingly, skips picking
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
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from sisyphus import gaterecord
from sisyphus.config import ConfigError, SisyphusConfig, load_config
from sisyphus.digest import DigestReport, DigestWriteError, NeedsYou, write_digest
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
from sisyphus.runrecord import RunRecord, record_path

AGENT_COMMANDS = {
    "claude": lambda prompt: ["claude", "-p", prompt, PERMISSION_FLAG],
    "opencode": lambda prompt: ["opencode", "run", prompt],
}
TOKEN_CMD_TIMEOUT = 30
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


def make_agent_runner(backend: str | None, cwd: Path, run: Callable[..., Any] = subprocess.run) -> AgentRunner:
    """Subprocess wrapper for the configured agent backend, run in ``cwd`` (the dedicated clone).

    ``claude`` runs ``claude -p <prompt> --dangerously-skip-permissions``; ``opencode`` runs
    ``opencode run <prompt>`` (D-11). A non-zero exit raises :class:`AgentError`.
    """
    if backend not in AGENT_COMMANDS:
        raise ConfigError("[agent] backend must be set to 'claude' or 'opencode' to run an agent")
    build = AGENT_COMMANDS[backend]

    def runner(prompt: str) -> None:
        try:
            proc = run(build(prompt), cwd=cwd, check=False)
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

    def open_pr(self, branch: str, base: str, title: str, body: str) -> PullRequest:
        res = self.gh._run(
            ["gh", "pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body],
            self.gh.cwd,
            self.gh.env,
        )
        match = _PR_URL.search(res.stdout)
        if res.returncode != 0 or not match:
            raise GitHubError(
                f"gh pr create failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()[-300:]}"
            )
        return PullRequest(int(match.group(1)), res.stdout.strip().splitlines()[-1])


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
    runner = make_agent_runner(config.agent.backend, repo_dir) if config.agent.backend else _no_backend

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


def _resolve_options(config: SisyphusConfig, services: Services, base: str | None) -> OrchestratorOptions:
    if not base:
        raise _fail(
            "config error: no base branch; pass --base <branch> (the .sisyphus.toml schema has no base-branch key "
            "and no default is assumed)"
        )
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


def _pick_phase(config: SisyphusConfig, services: Services, state: _RunState) -> list[Candidate]:
    """Under the clone lock: report an earlier PR awaiting review, else pick items. Raises LockHeld."""
    assert config.project.repo_dir and config.project.workspace_dir  # noqa: S101
    workspace = Path(config.project.workspace_dir).expanduser()
    with clone_lock(config.project.repo_dir, services.lock_dir):
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
            return []
        return pick_items(
            config.source.work_item,
            services.tracker,
            services.lookup,
            workspace_dir=workspace,
            limit=config.limits.max_tickets_per_run,
        )


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
        )
    except LockHeld:
        raise
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
    picked: Sequence[Candidate], config: SisyphusConfig, services: Services, base: str | None, state: _RunState
) -> None:
    """Run each picked item (outside the CLI lock; the orchestrator takes its own), honouring max_run_minutes."""
    if not picked:
        return
    if not config.agent.backend:
        raise _fail("config error: [agent] backend is not set")
    options = _resolve_options(config, services, base)
    tracker = LimitTracker(config.limits, services.clock)
    for cand in picked:
        if "max_run_minutes" in tracker.status().reached:
            state.limits_hit.append(
                f"max_run_minutes ({config.limits.max_run_minutes}) reached before starting {cand.ref}"
            )
            return
        tracker.record_ticket()
        try:
            _run_one(cand, config, services, options, state)
        except LockHeld:
            return


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


@main.command("run")
@config_option
@click.option("--base", default=None, help="PR base branch (no default; required when there is work to run).")
@click.pass_obj
def run_command(factory: ServicesFactory | None, config_path: str, base: str | None) -> None:
    """Pick up to max_tickets_per_run items, run each through the orchestrator, write one digest."""
    config = _load(config_path)
    _require(config, "repo_dir", "workspace_dir")
    services = _services(factory, config)
    state = _RunState()
    error: Exception | None = None
    try:
        picked = _pick_phase(config, services, state)
        _run_phase(picked, config, services, base, state)
    except LockHeld:
        return
    except Exception as exc:
        error = exc
        if not state.notes:
            state.notes.append(f"run failed: {exc}")
    digest_error = _write_digest(config, services, state) if state.has_content else None
    if error is not None:
        raise _fail(str(error)) from error
    if digest_error is not None:
        raise click.exceptions.Exit(1)


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
    click.echo(f"brief: {result.brief_path}")
    for decision in result.open_decisions:
        click.echo(f"open decision (C1): {decision}")


__all__ = ["Services", "default_services", "main", "make_agent_runner"]
