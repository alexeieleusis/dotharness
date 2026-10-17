"""Orchestrator: run one work item through intake, plan, build, push, PR and optional ci-fix.

Everything that touches the outside world is injected: the agent runner (prompt in, nothing out),
the subprocess runner (argv and cwd in, :class:`CommandResult` out; used for ``git``, the gate
commands and the ``harness`` CLI) and the pull-request client (:class:`PullRequestClient`). The
module imports neither the harness nor the spec-prism-flow package. Stages run strictly one after another and
each writes progress through the run record before and after it runs. A checkpoint (C1..C8) stops
the run: it is recorded on the run record, rendered in the digest and returned in
:class:`RunResult`; no later stage runs.
"""

# ruff: noqa: TRY003
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import time
import tomllib
from collections.abc import Callable, Sequence
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sisyphus import gaterecord
from sisyphus.cleanup import DELETED, KEPT, PrInfo, cleanup_branch
from sisyphus.config import SisyphusConfig
from sisyphus.digest import (
    CI_FIX_STAGE,
    MERGE_STAGE,
    DigestReport,
    DoneEntry,
    NeedsYou,
    StageTiming,
    needs_from_stop,
    write_digest,
)
from sisyphus.intake import INTAKE_STAGE, AgentRunner, BoundaryViolation, resolve_brief_path, run_intake
from sisyphus.lock import clone_lock
from sisyphus.paths import knowledge_file, tool_dir
from sisyphus.rails import (
    DEFAULT_MAX_RETRY_CYCLES,
    Checkpoint,
    LimitTracker,
    RetryBound,
    parse_push_argv,
    push_guard,
)
from sisyphus.resume import answer_path, consume_answer, read_answer
from sisyphus.runrecord import STAGE_COMPLETE, RunRecord
from sisyphus.stoprecord import (
    StopRecord,
    build_stop_record,
    clear_stop_record,
    is_abandoned,
    read_stop_record,
    write_stop_record,
)

PLAN_STAGE = "plan"
BUILD_STAGE = "build"
PUSH_STAGE = "push"
OPEN_PR_STAGE = "open-pr"
STAGE_ORDER = (INTAKE_STAGE, PLAN_STAGE, BUILD_STAGE, PUSH_STAGE, OPEN_PR_STAGE, CI_FIX_STAGE, MERGE_STAGE)

PROMPT_FILE = "sisyphus/orchestration-prompt.md"
CHEAT_SHEET = Path("docs") / "lifecycle-cheat-sheet.md"
PLAN_PROMPT_FILE = "spec-prism-flow/plan-generation-prompt.md"
BUILD_PROMPT_FILE = "spec-prism-flow/build-execution-prompt.md"
SPF_CONFIG_NAME = ".spec-prism-flow.toml"
STOP_SUFFIX = ".stop.json"
LEAF_GLOB = "*-leaf.md"
BRANCH_PREFIX = "sisyphus/"
PACKAGE_MARKERS = ("Makefile", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")

BRANCH_CREATED_KEY = "branch-created"
PR_NUMBER_KEY = "pr-number"  # additive: the PR number, so the branch cleanup can ask gh about it by number
BUILD_RETRY_KEY = "build:retry-cycles"
CI_FIX_ATTEMPTS_KEY = "ci-fix:attempts"
LEAF_LIMIT_KEY = "plan:leaf-limit"

_STOP_CHECKPOINTS = {PLAN_STAGE: {"C2"}, BUILD_STAGE: {"C3", "C4", "C5"}}


class OrchestratorError(Exception):
    """Raised for an unusable setup or an unexpected state that is not one of C1..C8."""


class GitHubError(Exception):
    """Raised by a :class:`PullRequestClient` for an unrecoverable failure (auth, network). Maps to C8."""


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one subprocess call."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


SubprocessRunner = Callable[[Sequence[str], Path | None], CommandResult]


def default_subprocess_runner(argv: Sequence[str], cwd: Path | None) -> CommandResult:
    """Real :class:`SubprocessRunner` for the CLI: ``subprocess.run`` with captured text output."""
    try:
        proc = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603
    except OSError as exc:
        return CommandResult(127, "", str(exc))
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str = ""


class PullRequestClient(Protocol):
    """GitHub access for the open-PR stage. Raise :class:`GitHubError` on unrecoverable failures."""

    def find_pr(self, branch: str) -> PullRequest | None:
        """Return the open PR whose head is ``branch``, or ``None``."""
        ...

    def open_pr(self, branch: str, base: str, title: str, body: str) -> PullRequest:
        """Open one PR from ``branch`` into ``base``."""
        ...

    def pr_state(self, ref: str) -> PrInfo | None:
        """``gh pr view <number or branch>``: the PR's state and head SHA (any state), or ``None`` if none."""
        ...


@dataclass(frozen=True)
class OrchestratorOptions:
    """Per-run inputs that are not in ``.sisyphus.toml``.

    ``base_branch`` is the PR base, a required parameter with no default here (the CLI resolves
    ``--base`` > ``[project].base_branch`` > ``main`` and passes the result in). ``repo_slug`` is the
    hyphenated ``owner-repo`` slug the gate record is filed under. ``default_branch`` is the
    repository default branch when it differs from ``base_branch``; both are never pushed to.
    ``spf_config_path`` defaults to ``<repo_dir>/.spec-prism-flow.toml`` (``build.commands`` and
    ``plan.phase_dir`` are read from it). ``phase_dir`` overrides ``plan.phase_dir``.
    ``resume_command`` is the exact command stored in the stop record (the CLI supplies it with the config
    path and base); the library default has a placeholder for the config path.
    """

    base_branch: str
    repo_slug: str
    default_branch: str | None = None
    ci_fix: bool = False
    spf_config_path: Path | None = None
    phase_dir: Path | None = None
    state_dir: Path | None = None
    lock_dir: Path | None = None
    home: Path | None = None
    checkout_root: Path | None = None
    resume_command: str | None = None


@dataclass(frozen=True)
class RunResult:
    item: str
    checkpoint: Checkpoint | None
    detail: str
    stages_run: tuple[str, ...]
    stages_skipped: tuple[str, ...]
    branch: str
    pr: PullRequest | None
    head_sha: str | None
    gate_recorded: bool
    digest_path: Path | None
    record: RunRecord
    report: DigestReport

    @property
    def completed(self) -> bool:
        return self.checkpoint is None


class _Stop(Exception):
    def __init__(self, checkpoint: Checkpoint, detail: str, questions: tuple[str, ...] = ()) -> None:
        super().__init__(detail)
        self.checkpoint = checkpoint
        self.detail = detail
        self.questions = questions


# ------------------------------------------------------------------ helpers


def branch_name(item: str) -> str:
    """Deterministic Sisyphus branch for ``item``: ``sisyphus/<slug>-<8 hex of sha256>``."""
    slug = re.sub(r"[^a-z0-9]+", "-", item.lower()).strip("-")[:40].strip("-") or "item"
    return f"{BRANCH_PREFIX}{slug}-{hashlib.sha256(item.encode()).hexdigest()[:8]}"


def stop_report_path(record: RunRecord) -> Path:
    return record.path.with_name(record.path.name + STOP_SUFFIX)


def read_spf_config(path: Path) -> dict[str, Any] | None:
    """Read ``.spec-prism-flow.toml`` with ``tomllib``; ``None`` when the file is absent."""
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise OrchestratorError(f"cannot read {path}: {exc}") from exc


def read_build_commands(path: Path) -> list[str] | None:
    """``build.commands`` from the file, or ``None`` when the file, the key or any command is absent."""
    data = read_spf_config(path)
    cmds = data.get("build", {}).get("commands") if data else None
    if cmds is None:
        return None
    if not isinstance(cmds, list) or not all(isinstance(c, str) and c.strip() for c in cmds):
        raise OrchestratorError(f"{path}: build.commands must be a list of non-empty strings")
    return list(cmds) or None


def package_dirs(repo_dir: Path, changed_files: Sequence[str]) -> list[Path]:
    """Nearest ancestor directory (below or at ``repo_dir``) of each changed file holding a package marker.

    Files with no such ancestor map to ``repo_dir``. Sorted and de-duplicated.
    """
    found: set[Path] = set()
    for name in changed_files:
        d = (repo_dir / name).parent
        while True:
            if any((d / m).exists() for m in PACKAGE_MARKERS):
                break
            if d == repo_dir or repo_dir not in d.parents:
                d = repo_dir
                break
            d = d.parent
        found.add(d)
    return sorted(found)


def build_orchestration_prompt(template: str, values: dict[str, str]) -> str:
    """Substitute ``<key>`` for each entry of ``values``; unknown angle-bracket text is left alone."""
    for key, value in values.items():
        template = template.replace(f"<{key}>", value)
    return template


def _tail(text: str, n: int = 400) -> str:
    text = text.strip()
    return text[-n:]


# --------------------------------------------------------------- the run


class _Run:
    def __init__(
        self,
        item: str,
        config: SisyphusConfig,
        options: OrchestratorOptions,
        agent_runner: AgentRunner,
        subprocess_runner: SubprocessRunner,
        pr_client: PullRequestClient,
        clock: Callable[[], float],
    ) -> None:
        if not config.project.repo_dir:
            raise OrchestratorError("[project].repo_dir is not set")
        if not config.project.workspace_dir:
            raise OrchestratorError("[project].workspace_dir is not set")
        if not options.base_branch:
            raise OrchestratorError("base_branch must be set (config or CLI input)")
        gaterecord.validate_repo_slug(options.repo_slug)
        self.item = item
        self.config = config
        self.opt = options
        self.agent = agent_runner
        self.sh = subprocess_runner
        self.gh = pr_client
        self.clock = clock
        self.repo = Path(config.project.repo_dir).expanduser()
        self.workspace = Path(config.project.workspace_dir).expanduser()
        self.branch = branch_name(item)
        self.spf = options.spf_config_path or self.repo / SPF_CONFIG_NAME
        self.tracker = LimitTracker(config.limits, clock)
        self.rec = RunRecord.open(self.workspace, item)
        self.prior_cp = self.rec.checkpoint
        self.pr: PullRequest | None = None
        self.head_sha: str | None = None
        self.gate_recorded = False
        self.notes: list[str] = []
        self.done_stages: list[str] = []
        self.skipped: list[str] = []
        self.timings: list[StageTiming] = []
        self.stop: _Stop | None = None
        self.stop_rec: StopRecord | None = None
        self.was_abandoned = False
        self.cleanup_done: str | None = None

    # -- git / process helpers

    def git(self, *args: str, network: bool = False, ok: bool = True) -> CommandResult:
        res = self.sh(["git", *args], self.repo)
        if res.returncode != 0 and ok:
            msg = f"git {' '.join(args)} failed (exit {res.returncode}): {_tail(res.stderr)}"
            if network:
                raise _Stop(Checkpoint.C8, msg)
            raise OrchestratorError(msg)
        return res

    def rev_parse_head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def created_branches(self) -> set[str]:
        return {self.branch} if self.rec.retries.get(BRANCH_CREATED_KEY) else set()

    # -- stage driver

    def stages(self) -> list[str]:
        wanted = [INTAKE_STAGE, PLAN_STAGE, BUILD_STAGE, PUSH_STAGE, OPEN_PR_STAGE]
        if self.opt.ci_fix:
            wanted.append(CI_FIX_STAGE)
        if self.config.merge.enabled:
            wanted.append(MERGE_STAGE)
        return wanted

    def start_index(self) -> int:
        """Index into ``STAGE_ORDER`` of the first stage that must still run."""
        stage = self.rec.stage
        if stage not in STAGE_ORDER:
            return 0
        idx = STAGE_ORDER.index(stage)
        if self.rec.stage_status != STAGE_COMPLETE:
            return idx
        # A complete stage is finished, whether the run stopped after it (limit checked between stages)
        # or cleanly. The one exception is intake stopped at C1: it is re-entered to re-parse the brief.
        if self.prior_cp == Checkpoint.C1.value and stage == INTAKE_STAGE:
            return idx
        return idx + 1

    def execute(self) -> Checkpoint | None:
        start = self.start_index()
        if self.prior_cp is not None:
            self.rec.set_checkpoint(None)
        handlers: dict[str, Callable[[], None]] = {
            INTAKE_STAGE: self.stage_intake,
            PLAN_STAGE: self.stage_plan,
            BUILD_STAGE: self.stage_build,
            PUSH_STAGE: self.stage_push,
            OPEN_PR_STAGE: self.stage_open_pr,
            CI_FIX_STAGE: self.stage_ci_fix,
            MERGE_STAGE: self.stage_merge,
        }
        try:
            self.prepare_clone(start)
            for stage in self.stages():
                if STAGE_ORDER.index(stage) < start:
                    self.skipped.append(stage)
                    continue
                self.check_time()
                began = self.clock()
                handlers[stage]()
                self.done_stages.append(stage)
                self.timings.append(StageTiming(self.item, stage, self.clock() - began))
        except _Stop as stop:
            self.rec.set_checkpoint(stop.checkpoint.value)
            self.stop = stop
            self.stop_rec = self.write_stop(stop.checkpoint.value, stop.detail, stop.questions)
            return stop.checkpoint
        self.was_abandoned = is_abandoned(self.rec.path)
        clear_stop_record(self.rec.path)
        return None

    # -- local branch cleanup after a clean finish (see sisyphus.cleanup)

    def finished_cleanly(self) -> bool:
        """True only when the item's wanted stages are all complete and nothing marks it stopped."""
        wanted = [STAGE_ORDER.index(s) for s in self.stages()]
        return (
            self.stop is None
            and not self.was_abandoned
            and self.rec.checkpoint is None
            and read_stop_record(self.rec.path) is None
            and self.rec.stage in STAGE_ORDER
            and self.rec.stage_status == STAGE_COMPLETE
            and STAGE_ORDER.index(self.rec.stage) >= max(wanted)
            and BRANCH_CREATED_KEY in self.rec.retries
        )

    def cleanup_branch(self) -> None:
        """Delete the local branch if (and only if) every safety check passes; otherwise keep it and say why."""
        if not self.finished_cleanly():
            return
        try:
            outcome = cleanup_branch(
                lambda args: self.sh(["git", *args], self.repo),
                item=self.item,
                branch=self.branch,
                base=self.opt.base_branch,
                pr_lookup=self.gh.pr_state,
                pr_ref=str(stored) if (stored := self.rec.retries.get(PR_NUMBER_KEY)) else None,
            )
        except Exception as exc:  # a failed cleanup never turns a clean finish into an error
            self.note(f"Branch kept: {self.item} {self.branch} cleanup failed: {type(exc).__name__}: {exc}")
            return
        if outcome.status == DELETED:
            self.cleanup_done = outcome.message
        elif outcome.status == KEPT:
            self.note(outcome.message)

    def resume_command(self) -> str:
        template = self.opt.resume_command or "sisyphus resume {item} --config <your .sisyphus.toml>"
        return template.replace("{item}", shlex.quote(self.item))

    def write_stop(self, checkpoint: str | None, detail: str, questions: tuple[str, ...] = ()) -> StopRecord:
        stop = build_stop_record(
            self.item,
            checkpoint=checkpoint,
            stage=self.rec.stage,
            stage_status=self.rec.stage_status,
            detail=detail,
            resume_command=self.resume_command(),
            questions=questions,
            brief_path=str(resolve_brief_path(self.config, self.item)),
            answer_path=str(answer_path(self.rec.path)),
        )
        write_stop_record(self.rec.path, stop)
        return stop

    def record_error(self, exc: BaseException) -> None:
        """Write the stop record for an unexpected error; never masks the error itself."""
        with suppress(Exception):
            self.stop_rec = self.write_stop(None, f"{type(exc).__name__}: {exc}")

    # -- single dedicated clone: clean between items, each item on its own branch

    def ensure_clean(self) -> None:
        """Refuse (C8-style stop) when the clone has uncommitted changes. Never stashes or resets."""
        dirty = self.git("status", "--porcelain", "--untracked-files=all").stdout.strip()
        if dirty:
            raise _Stop(
                Checkpoint.C8,
                f"the dedicated clone {self.repo} has uncommitted changes (for example after a crash); "
                "Sisyphus will not stash, reset or discard them. Commit or discard them yourself. "
                f"First lines: {_tail(dirty, 200)}",
            )

    def prepare_clone(self, start: int) -> None:
        """Before the first stage: a new item needs a clean clone; a later-stage resume goes back to its branch."""
        if self.rec.stage is None:
            self.ensure_clean()
            return
        if start > STAGE_ORDER.index(BUILD_STAGE) and any(STAGE_ORDER.index(st) >= start for st in self.stages()):
            self.ensure_clean()
            if self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != self.branch:
                if self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{self.branch}", ok=False).returncode == 0:
                    self.git("checkout", self.branch)
                    return
                # No local branch: expected after a clean finish (it is deleted once verified on origin).
                # From the PR stage on the branch is already pushed, so work from the remote head, detached.
                if start < STAGE_ORDER.index(OPEN_PR_STAGE):
                    raise OrchestratorError(f"cannot resume: branch {self.branch} does not exist in {self.repo}")
                fetched = self.git("fetch", "origin", self.branch, ok=False)
                if fetched.returncode != 0:
                    raise OrchestratorError(
                        f"cannot resume: branch {self.branch} is not in {self.repo} and not on origin: {_tail(fetched.stderr)}"
                    )
                self.git("checkout", "--detach", "FETCH_HEAD")

    def check_time(self) -> None:
        if self.tracker.elapsed_minutes() >= self.config.limits.max_run_minutes:
            raise _Stop(Checkpoint.C6, f"max_run_minutes ({self.config.limits.max_run_minutes}) reached")

    # -- intake

    def stage_intake(self) -> None:
        reparse = (
            self.prior_cp == Checkpoint.C1.value
            and self.rec.stage == INTAKE_STAGE
            and self.rec.stage_status == STAGE_COMPLETE
        )
        try:
            result = run_intake(
                self.item,
                self.config,
                self.agent,
                self.rec,
                self.opt.home,
                self.opt.checkout_root,
                reparse_only=reparse,
            )
        except BoundaryViolation as exc:
            raise _Stop(Checkpoint.C5, str(exc)) from exc
        if result.blocked:
            texts = "; ".join(f"{d.number}. {d.text}" for d in result.open_decisions)
            raise _Stop(
                Checkpoint.C1,
                f"open decisions in {result.brief_path}: {texts}",
                tuple(f"{d.number}. {d.text}" for d in result.open_decisions),
            )

    # -- agent stages (plan, build)

    def prompt_values(self, stage: str) -> dict[str, str]:
        home, root = self.opt.home, self.opt.checkout_root
        return {
            "stage": stage,
            "work-item reference": self.item,
            "brief path": str(resolve_brief_path(self.config, self.item)),
            "workspace dir": str(self.workspace),
            "target repo dir": str(self.repo),
            "target branch": self.branch,
            "phase config path": str(self.spf),
            "cheat sheet path": str(tool_dir(home, root) / CHEAT_SHEET),
            "plan prompt path": str(knowledge_file(PLAN_PROMPT_FILE, home, root)),
            "build prompt path": str(knowledge_file(BUILD_PROMPT_FILE, home, root)),
            "max retry cycles": str(DEFAULT_MAX_RETRY_CYCLES),
            "stop report path": str(stop_report_path(self.rec)),
        }

    def run_agent_stage(self, stage: str) -> None:
        template = knowledge_file(PROMPT_FILE, self.opt.home, self.opt.checkout_root).read_text(encoding="utf-8")
        prompt = build_orchestration_prompt(template, self.prompt_values(stage))
        stop_file = stop_report_path(self.rec)
        stop_file.unlink(missing_ok=True)
        answer = read_answer(self.rec.path)
        if answer:
            prompt += f"\n\nThe user answered your earlier stop. Their answer:\n\n{answer}\n"
        self.agent(prompt)
        if answer:
            consume_answer(self.rec.path)
        if stop_file.is_file():
            self.raise_stop_report(stage, stop_file)

    def raise_stop_report(self, stage: str, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cp = data["checkpoint"]
            detail = str(data.get("detail", ""))
            leaf = str(data.get("leaf", ""))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise OrchestratorError(f"unreadable stop report {path}: {exc}") from exc
        if cp not in _STOP_CHECKPOINTS[stage]:
            raise OrchestratorError(f"stop report {path}: checkpoint {cp!r} is not valid for stage {stage}")
        text = f"leaf {leaf}: {detail}" if leaf else detail
        if stage == BUILD_STAGE and cp == "C5":
            self.rec.increment_retry(BUILD_RETRY_KEY)
        raise _Stop(Checkpoint(cp), text)

    def stage_plan(self) -> None:
        # After a leaf-limit stop the leaves already exist: only re-check the limit, no new agent run.
        replanned = not (self.prior_cp == Checkpoint.C6.value and self.rec.retries.get(LEAF_LIMIT_KEY))
        with self.rec.stage_scope(PLAN_STAGE):
            if replanned:
                self.run_agent_stage(PLAN_STAGE)
            leaves = self.count_leaves()
            limit = self.config.limits.max_leaves_per_run
            if leaves > limit:
                self.rec.increment_retry(LEAF_LIMIT_KEY)
                raise _Stop(Checkpoint.C6, f"the plan has {leaves} leaves; max_leaves_per_run is {limit}")

    def count_leaves(self) -> int:
        phase_dir = self.opt.phase_dir
        if phase_dir is None:
            data = read_spf_config(self.spf)
            raw = data.get("plan", {}).get("phase_dir") if data else None
            if not isinstance(raw, str) or not raw:
                raise OrchestratorError(
                    f"cannot find the leaf files: set plan.phase_dir in {self.spf} or pass phase_dir"
                )
            phase_dir = Path(raw).expanduser()
            if not phase_dir.is_absolute():
                phase_dir = self.repo / phase_dir
        if not phase_dir.is_dir():
            raise OrchestratorError(f"phase directory {phase_dir} does not exist after the plan stage")
        return len(list(phase_dir.glob(LEAF_GLOB)))

    def prepare_branch(self) -> None:
        base = self.opt.base_branch
        self.ensure_clean()
        self.git("fetch", "origin", base, network=True)
        exists = self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{self.branch}", ok=False).returncode == 0
        if exists:
            if not self.created_branches():
                raise OrchestratorError(f"branch {self.branch} exists but Sisyphus did not create it; not touching it")
            self.git("checkout", self.branch)
            return
        self.git("checkout", "-b", self.branch, f"origin/{base}")
        self.rec.increment_retry(BRANCH_CREATED_KEY)

    def stage_build(self) -> None:
        with self.rec.stage_scope(BUILD_STAGE):
            bound = RetryBound(DEFAULT_MAX_RETRY_CYCLES, self.rec.retries.get(BUILD_RETRY_KEY, 0)).result()
            if bound.escalate:
                raise _Stop(Checkpoint.C5, f"build already failed {bound.attempts} retry cycles; not launching again")
            self.prepare_branch()
            self.run_agent_stage(BUILD_STAGE)
            current = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if current != self.branch:
                raise OrchestratorError(f"build left the checkout on {current!r}, expected {self.branch!r}")
            ahead = self.git("rev-list", "--count", f"origin/{self.opt.base_branch}..HEAD").stdout.strip()
            if not ahead.isdigit() or int(ahead) == 0:
                raise OrchestratorError(f"build produced no commits on {self.branch}")

    # -- push

    def needs_rebase(self) -> bool:
        remote = self.git("ls-remote", "--heads", "origin", self.branch, network=True).stdout.strip()
        if not remote:
            return False
        self.git("fetch", "origin", self.branch, network=True)
        return self.git("merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD", ok=False).returncode != 0

    def stage_push(self) -> None:
        with self.rec.stage_scope(PUSH_STAGE):
            request = parse_push_argv(["push", "origin", self.branch], needs_rebase=self.needs_rebase())
            protected = {self.opt.base_branch, *([self.opt.default_branch] if self.opt.default_branch else [])}
            for default in sorted(protected):
                decision = push_guard(request, default, self.created_branches())
                if not decision.allowed:
                    if decision.checkpoint is not None:
                        raise _Stop(decision.checkpoint, decision.reason)
                    raise OrchestratorError(f"push refused: {decision.reason}")
            res = self.git("push", "origin", self.branch, ok=False)
            if res.returncode != 0:
                raise _Stop(Checkpoint.C8, f"git push failed (exit {res.returncode}): {_tail(res.stderr)}")

    # -- PR + gate

    def pr_number(self) -> int:
        if self.pr is None:
            try:
                self.pr = self.gh.find_pr(self.branch)
            except GitHubError as exc:
                raise _Stop(Checkpoint.C8, str(exc)) from exc
            if self.pr is None:
                raise OrchestratorError(f"no open PR found for branch {self.branch}")
        return self.pr.number

    def stage_open_pr(self) -> None:
        with self.rec.stage_scope(OPEN_PR_STAGE):
            try:
                self.pr = self.gh.find_pr(self.branch)
                if self.pr is None:
                    body = f"Work item: {self.item}\n\nOpened by Sisyphus. Review required; this PR is not approved."
                    self.pr = self.gh.open_pr(self.branch, self.opt.base_branch, f"Sisyphus: {self.item}", body)
            except GitHubError as exc:
                raise _Stop(Checkpoint.C8, str(exc)) from exc
            if self.rec.retries.get(PR_NUMBER_KEY) != self.pr.number:
                self.rec.retries[PR_NUMBER_KEY] = self.pr.number
                self.rec.save()
            self.gate()

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def gate(self) -> None:
        """Run the gate commands at HEAD and write the gate record only if every command exits 0."""
        number = self.pr_number()
        commands = read_build_commands(self.spf)
        if commands is None:
            self.note(f"no gate record for PR #{number}: no build.commands in {self.spf}")
            return
        sha = self.rev_parse_head()
        changed = self.git("diff", "--name-only", f"origin/{self.opt.base_branch}...HEAD").stdout.split("\n")
        dirs = package_dirs(self.repo, [c for c in changed if c.strip()])
        if not dirs:
            self.note(f"no gate record for PR #{number}: no changed files to gate")
            return
        for directory in dirs:
            for command in commands:
                res = self.sh(shlex.split(command), directory)
                if res.returncode != 0:
                    raise _Stop(
                        Checkpoint.C5,
                        f"gate command '{command}' failed in {directory} (exit {res.returncode}): "
                        f"{_tail(res.stderr or res.stdout)}",
                    )
        gaterecord.write_gate_record(self.opt.repo_slug, number, sha, self.opt.state_dir, commands)
        self.head_sha = sha
        self.gate_recorded = True

    # -- ci-fix and merge

    def sync_head(self) -> bool:
        """Fast-forward the clone to the remote branch; True when the head moved."""
        before = self.rev_parse_head()
        self.git("fetch", "origin", self.branch, network=True)
        remote = self.git("rev-parse", "FETCH_HEAD").stdout.strip()
        if remote != before and self.git("merge", "--ff-only", "FETCH_HEAD", ok=False).returncode != 0:
            raise _Stop(Checkpoint.C7, f"{self.branch} cannot be updated without a rebase or force-push")
        return self.rev_parse_head() != before

    def stage_ci_fix(self) -> None:
        number = self.pr_number()
        cap = self.config.limits.max_fix_attempts_per_pr
        with self.rec.stage_scope(CI_FIX_STAGE):
            attempts = self.rec.retries.get(CI_FIX_ATTEMPTS_KEY, 0)
            self.tracker.fix_attempts[str(number)] = attempts
            settled = False
            while attempts < cap:
                res = self.sh(["harness", "run", "ci-fix", "--pr", str(number)], self.repo)
                attempts = self.rec.increment_retry(CI_FIX_ATTEMPTS_KEY)
                self.tracker.record_fix_attempt(str(number))
                if res.returncode != 0:
                    raise _Stop(
                        Checkpoint.C8, f"harness run ci-fix failed (exit {res.returncode}): {_tail(res.stderr)}"
                    )
                if not self.sync_head():
                    settled = True
                    break
                self.gate()  # a new head SHA makes the old record stale
            if not settled:
                raise _Stop(Checkpoint.C6, f"max_fix_attempts_per_pr ({cap}) reached for PR #{number}")
            if gaterecord.read_gate_record(self.opt.repo_slug, number, self.opt.state_dir) != self.rev_parse_head():
                self.gate()

    def stage_merge(self) -> None:
        number = self.pr_number()
        with self.rec.stage_scope(MERGE_STAGE):
            res = self.sh(["harness", "run", "merge", "--pr", str(number)], self.repo)
            if res.returncode != 0:
                raise _Stop(Checkpoint.C8, f"harness run merge failed (exit {res.returncode}): {_tail(res.stderr)}")


def run_item(
    item: str,
    config: SisyphusConfig,
    options: OrchestratorOptions,
    *,
    agent_runner: AgentRunner,
    subprocess_runner: SubprocessRunner,
    pr_client: PullRequestClient,
    clock: Callable[[], float] = time.monotonic,
    today: datetime | None = None,
    write_digest_file: bool = True,
    lock_held: bool = False,
) -> RunResult:
    """Run one work item through the stages, stopping at the first checkpoint.

    Takes the per-clone lock for the whole run (``LockHeld`` propagates before any state is
    touched) unless ``lock_held=True``, which means the caller (a CLI command) already holds
    ``clone_lock(repo_dir, options.lock_dir)`` for the invocation; flock is not re-entrant, so
    locking again would contend with the caller. Writes the digest even when a stage raises; a digest failure raises
    ``DigestWriteError`` (chained to the stage error if any). ``write_digest_file=False`` lets a
    multi-item caller aggregate ``RunResult.record`` and ``RunResult.report`` into one digest.
    Unexpected failures (agent runner errors, ``RunRecordError``, ``IntakeError``,
    :class:`OrchestratorError`) propagate and leave the stage in progress.
    """
    if not config.project.repo_dir:
        raise OrchestratorError("[project].repo_dir is not set")
    with nullcontext() if lock_held else clone_lock(config.project.repo_dir, options.lock_dir):
        run = _Run(item, config, options, agent_runner, subprocess_runner, pr_client, clock)
        error: BaseException | None = None
        checkpoint: Checkpoint | None = None
        try:
            checkpoint = run.execute()
            if checkpoint is None:
                run.cleanup_branch()
        except Exception as exc:
            error = exc
            run.record_error(exc)
        report = _report(run)
        digest_path: Path | None = None
        if write_digest_file:
            try:
                digest_path = write_digest([run.rec], config, report, today)
            except Exception as exc:
                raise exc from error
        if error is not None:
            raise error
        return RunResult(
            item=item,
            checkpoint=checkpoint,
            detail=run.stop.detail if run.stop else "",
            stages_run=tuple(run.done_stages),
            stages_skipped=tuple(run.skipped),
            branch=run.branch,
            pr=run.pr,
            head_sha=run.head_sha,
            gate_recorded=run.gate_recorded,
            digest_path=digest_path,
            record=run.rec,
            report=report,
        )


def _report(run: _Run) -> DigestReport:
    needs: tuple[NeedsYou, ...] = ()
    limits: tuple[str, ...] = ()
    if run.stop_rec is not None:
        needs = (needs_from_stop(run.stop_rec),)
    if run.stop is not None and run.stop.checkpoint is Checkpoint.C6:
        limits = (f"{run.item}: {run.stop.detail}",)
    done: tuple[DoneEntry, ...] = ()
    if run.pr is not None:
        evidence = [run.pr.url or f"PR #{run.pr.number}", f"branch {run.branch}"]
        evidence.append(f"gate record written for {run.head_sha}" if run.gate_recorded else "no gate record written")
        if run.cleanup_done:
            evidence.append(run.cleanup_done)
        done = (DoneEntry(f"{run.item}: PR #{run.pr.number} open", tuple(evidence)),)
    elif run.cleanup_done:
        done = (DoneEntry(f"{run.item}: local branch removed", (run.cleanup_done,)),)
    return DigestReport(
        needs_you=needs,
        done=done,
        deviations=tuple(run.notes),
        limits_hit=limits,
        timings=tuple(run.timings),
    )
