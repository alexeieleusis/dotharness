"""Orchestrator: run one work item through intake, plan, build, then push and open the PR.

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
import time
import tomllib
from collections.abc import Callable, Sequence
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, Protocol

from sisyphus import prbody
from sisyphus.cleanup import DELETED, KEPT, PrInfo, cleanup_branch
from sisyphus.config import SisyphusConfig
from sisyphus.digest import (
    DigestReport,
    DoneEntry,
    NeedsYou,
    StageTiming,
    needs_from_stop,
    write_digest,
)
from sisyphus.intake import AgentRunner, BoundaryViolation, resolve_brief_path, run_intake
from sisyphus.lock import clone_lock
from sisyphus.paths import knowledge_file, tool_dir
from sisyphus.plan import leaf_stems, plan_path
from sisyphus.plan import read_spf_config as _read_spf_config
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
from sisyphus.stages import (
    BUILD_RETRY_KEY,
    BUILD_STAGE,
    INTAKE_STAGE,
    LEAF_LIMIT_KEY,
    OPEN_PR_STAGE,
    PLAN_STAGE,
    PUSH_STAGE,
    STAGE_ORDER,
)
from sisyphus.stoprecord import (
    StopRecord,
    build_stop_record,
    clear_stop_record,
    fill_resume_template,
    is_abandoned,
    read_stop_record,
    write_stop_record,
)
from sisyphus.subproc import CommandResult, run_command

PROMPT_FILE = "sisyphus/orchestration-prompt.md"
CHEAT_SHEET = Path("docs") / "lifecycle-cheat-sheet.md"
PLAN_PROMPT_FILE = "spec-prism-flow/plan-generation-prompt.md"
BUILD_PROMPT_FILE = "spec-prism-flow/build-execution-prompt.md"
SPF_CONFIG_NAME = ".spec-prism-flow.toml"
STOP_SUFFIX = ".stop.json"
BRANCH_PREFIX = "sisyphus/"
PACKAGE_MARKERS = ("Makefile", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")

BRANCH_CREATED_KEY = "branch-created"
GATE_FAILED_KEY = "gate-failed"  # additive: 1 once a gate command failed, so status and reruns still see it
PR_NUMBER_KEY = "pr-number"  # additive: the PR number, so the branch cleanup can ask gh about it by number

_STOP_CHECKPOINTS = {PLAN_STAGE: {"C2"}, BUILD_STAGE: {"C3", "C4", "C5"}}


class OrchestratorError(Exception):
    """Raised for an unusable setup or an unexpected state that is not one of C1..C8."""


class GitHubError(Exception):
    """Raised by a :class:`PullRequestClient` for an unrecoverable failure (auth, network). Maps to C8."""


SubprocessRunner = Callable[[Sequence[str], Path | None], CommandResult]


def default_subprocess_runner(argv: Sequence[str], cwd: Path | None) -> CommandResult:
    """Real :class:`SubprocessRunner` for the CLI: :func:`run_command` without an env."""
    return run_command(argv, cwd)


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
        """Open one draft PR from ``branch`` into ``base``; a human marks it ready."""
        ...

    def pr_state(self, ref: str) -> PrInfo | None:
        """``gh pr view <number or branch>``: the PR's state and head SHA (any state), or ``None`` if none."""
        ...


@dataclass(frozen=True)
class OrchestratorOptions:
    """Per-run inputs that are not in ``.sisyphus.toml``.

    ``base_branch`` is the PR base, a required parameter with no default here (the CLI resolves
    ``--base`` > ``[project].base_branch`` > ``main`` and passes the result in). ``default_branch`` is the
    repository default branch when it differs from ``base_branch``; both are never pushed to.
    ``spf_config_path`` defaults to ``<repo_dir>/.spec-prism-flow.toml``: the base each item's own config
    (``<workspace_dir>/plan-<n>/.spec-prism-flow.toml``) is copied from, with ``plan.workspace_dir`` and
    ``plan.phase_dir`` set for the item. ``phase_dir`` overrides the item's ``plan.phase_dir``.
    ``resume_command`` is the exact command stored in the stop record (the CLI supplies it with the config
    path and base); the library default has a placeholder for the config path.
    """

    base_branch: str
    default_branch: str | None = None
    spf_config_path: Path | None = None
    phase_dir: Path | None = None
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


def plan_dir_name(item: str) -> str:
    """Per-item plan directory name: ``plan-<n>`` for ``#<n>``, else ``plan-<slug>-<8 hex of sha256>``.

    The slug is truncated, so non-numeric references carry a hash of the full reference to keep distinct items apart.
    """
    match = re.fullmatch(r"#?(\d+)", item.strip())
    if match:
        return f"plan-{match.group(1)}"
    slug = re.sub(r"[^a-z0-9]+", "-", item.lower()).strip("-")[:40].strip("-") or "item"
    return f"plan-{slug}-{hashlib.sha256(item.encode()).hexdigest()[:8]}"


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _toml_string(key)


def _toml_string(value: str) -> str:
    return json.dumps(value).replace("\x7f", "\\u007f")  # a JSON string is a valid TOML basic string


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return repr(value)
    if isinstance(value, float):
        if value != value:
            return "nan"
        return {float("inf"): "inf", float("-inf"): "-inf"}.get(value, repr(value))
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + "}"
    raise OrchestratorError(f"cannot write a {type(value).__name__} value to the spec-prism-flow config")


def _dump_table(data: dict[str, Any], path: tuple[str, ...], lines: list[str]) -> None:
    """Append ``data``'s scalars as ``key = value`` lines, then recurse into sub-tables and arrays of tables."""
    for key, value in data.items():
        if not _is_table(value) and not _is_table_array(value):
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, value in data.items():
        sub = (*path, key)
        header = ".".join(_toml_key(k) for k in sub)
        if _is_table(value):
            lines += ["", f"[{header}]"]
            _dump_table(value, sub, lines)
        elif _is_table_array(value):
            for item in value:
                lines += ["", f"[[{header}]]"]
                _dump_table(item, sub, lines)


def _is_table(value: object) -> bool:
    return isinstance(value, dict)


def _is_table_array(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def dump_spf_config(data: dict[str, Any]) -> str:
    """Serialise a parsed config back to TOML: nested tables, arrays of tables, quoted keys and datetimes."""
    lines: list[str] = []
    _dump_table(data, (), lines)
    return "\n".join(lines).strip() + "\n"


def stop_report_path(record: RunRecord) -> Path:
    return record.path.with_name(record.path.name + STOP_SUFFIX)


def read_spf_config(path: Path) -> dict[str, Any] | None:
    """Read ``.spec-prism-flow.toml`` with ``tomllib``; ``None`` when the file is absent."""
    try:
        return _read_spf_config(path)
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


@dataclass(frozen=True)
class GateEntry:
    """One ``[[build.gate]]`` table: a command and, optionally, the repo-relative package dirs it applies to."""

    run: str
    packages: tuple[str, ...] | None = None


_GATE_KEYS = frozenset({"run", "packages"})


def _gate_entry(path: Path, index: int, raw: object) -> GateEntry:
    where = f"{path}: build.gate[{index}]"
    if not isinstance(raw, dict):
        raise OrchestratorError(f"{where} must be a table")
    unknown = sorted(set(raw) - _GATE_KEYS)
    if unknown:
        raise OrchestratorError(f"{where} has unknown keys: {', '.join(map(str, unknown))}")
    run = raw.get("run")
    if not isinstance(run, str) or not run.strip():
        raise OrchestratorError(f"{where}.run must be a non-empty string")
    packages = raw.get("packages")
    if packages is None:
        return GateEntry(run)
    if not isinstance(packages, list) or not all(isinstance(p, str) and p.strip() for p in packages):
        raise OrchestratorError(f"{where}.packages must be a list of non-empty strings")
    for pkg in packages:
        pure = Path(pkg)
        if pure.is_absolute() or ".." in pure.parts:
            raise OrchestratorError(f"{where}.packages: {pkg!r} must be repo-relative with no '..' segment")
    return GateEntry(run, tuple(packages))


def read_gate_entries(path: Path) -> list[GateEntry] | None:
    """``[[build.gate]]`` entries, validated; ``None`` when the file, the table or the key is absent or empty."""
    data = read_spf_config(path)
    build = data.get("build") if data else None
    raw = build.get("gate") if isinstance(build, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
        raise OrchestratorError(f"{path}: build.gate must be an array of tables")
    return [_gate_entry(path, i, e) for i, e in enumerate(raw)] or None


def _gate_applies(entry: GateEntry, repo_dir: Path, directory: Path) -> bool:
    """An entry without ``packages`` applies everywhere; otherwise only to a listed directory (equality)."""
    return entry.packages is None or any((repo_dir / pkg).resolve() == directory.resolve() for pkg in entry.packages)


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


@dataclass(frozen=True)
class GateFailure:
    """The first failing gate command: what ran, where, its exit code and the output tail."""

    command: str
    directory: Path
    returncode: int
    tail: str


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
        self.base_spf = options.spf_config_path or self.repo / SPF_CONFIG_NAME
        self.plan_dir = self.workspace / plan_dir_name(item)
        self.handoff_dir = self.plan_dir / "handoffs"
        self.spf = self.plan_dir / SPF_CONFIG_NAME  # per-item copy of base_spf; see write_item_config
        self.tracker = LimitTracker(config.limits, clock)
        self.rec = RunRecord.open(self.workspace, item)
        self.prior_cp = self.rec.checkpoint
        self.pr: PullRequest | None = None
        self.gated = False
        self.gate_failure: GateFailure | None = None
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
        return [INTAKE_STAGE, PLAN_STAGE, BUILD_STAGE, PUSH_STAGE, OPEN_PR_STAGE]

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
            and self.gate_failure is None
            and not self.rec.retries.get(GATE_FAILED_KEY)
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
        stored_pr = self.rec.retries.get(PR_NUMBER_KEY)
        try:
            outcome = cleanup_branch(
                lambda args: self.sh(["git", *args], self.repo),
                item=self.item,
                branch=self.branch,
                base=self.opt.base_branch,
                pr_lookup=self.gh.pr_state,
                pr_ref=str(stored_pr) if stored_pr else None,
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
        return fill_resume_template(template, self.item)

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
            "handoff dir": str(self.handoff_dir),
            "target repo dir": str(self.repo),
            "target branch": self.branch,
            "phase config path": str(self.spf),
            "cheat sheet path": str(tool_dir(home, root) / CHEAT_SHEET),
            "plan prompt path": str(knowledge_file(PLAN_PROMPT_FILE, home, root)),
            "build prompt path": str(knowledge_file(BUILD_PROMPT_FILE, home, root)),
            "max retry cycles": str(DEFAULT_MAX_RETRY_CYCLES),
            "stop report path": str(stop_report_path(self.rec)),
        }

    def write_item_config(self) -> None:
        """Write the item's own spec-prism-flow config: the repo's file with this item's plan directories.

        The repo's file is shared by every item, so its ``plan.workspace_dir``/``plan.phase_dir`` belong to
        whichever item ran last. Agents, the leaf count and the gate commands all read the per-item copy.
        """
        data = read_spf_config(self.base_spf) or {}
        plan = dict(data.get("plan") or {})
        plan["workspace_dir"] = str(self.plan_dir)
        plan["phase_dir"] = str(self.opt.phase_dir or self.plan_dir / "phases")
        data["plan"] = plan
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        self.spf.write_text(dump_spf_config(data), encoding="utf-8")

    def run_agent_stage(self, stage: str) -> None:
        self.write_item_config()
        if stage == BUILD_STAGE:
            self.handoff_dir.mkdir(parents=True, exist_ok=True)
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
        self.write_item_config()
        phase_dir = self.opt.phase_dir
        if phase_dir is None:
            phase_dir = plan_path(read_spf_config(self.spf), self.repo, "phase_dir")
            if phase_dir is None:
                raise OrchestratorError(
                    f"cannot find the leaf files: set plan.phase_dir in {self.spf} or pass phase_dir"
                )
        if not phase_dir.is_dir():
            raise OrchestratorError(f"phase directory {phase_dir} does not exist after the plan stage")
        return len(leaf_stems(phase_dir))

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
            request = parse_push_argv(
                ["push", "origin", self.branch], history_rewrite=False, needs_rebase=self.needs_rebase()
            )
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
            # Validate the gate config before any PR exists: a malformed entry must not leave a PR with no gate result.
            self.write_item_config()
            read_build_commands(self.spf)
            read_gate_entries(self.spf)
            try:
                self.pr = self.gh.find_pr(self.branch)
                if self.pr is None:
                    body = self.build_pr_body()
                    self.pr = self.gh.open_pr(self.branch, self.opt.base_branch, self.build_pr_title(), body)
            except GitHubError as exc:
                raise _Stop(Checkpoint.C8, str(exc)) from exc
            if self.rec.retries.get(PR_NUMBER_KEY) != self.pr.number:
                self.rec.retries[PR_NUMBER_KEY] = self.pr.number
                self.rec.save()
            self.gate()

    def build_pr_title(self) -> str:
        """Derive the PR title from the requirements or brief H1; never raises (FR-13).

        Any failure falls back to ``Sisyphus: <item>``. Only local files are read (FR-16).
        """
        try:
            plan_dir = prbody.locate_plan_dir(self.spf, self.repo) or self.plan_dir
            brief = resolve_brief_path(self.config, self.item)
            return prbody.derive_title(brief, plan_dir, self.workspace) or prbody.fallback_title(self.item)
        except Exception:  # title derivation must never stop the run: fall back to the plain title
            return prbody.fallback_title(self.item)

    def build_pr_body(self) -> str:
        """Assemble the PR description from the workspace artifacts; never raises (FR-12, D-6).

        Any failure removes only the affected section; if assembly itself fails the plain header is used.
        """
        header = f"Work item: {self.item}\n\n{prbody.HEADER_NOTICE}"
        try:
            return self._assemble_pr_body()
        except Exception:  # assembly must never stop the run: fall back to the header alone
            return header

    def _assemble_pr_body(self) -> str:
        plan_dir = prbody.locate_plan_dir(self.spf, self.repo) or self.plan_dir
        phase_dir = self.opt.phase_dir or prbody.locate_phase_dir(self.spf, self.repo) or plan_dir / "phases"

        def run_git(args: Sequence[str]) -> str | None:
            try:
                res = self.git(*args, ok=False)
            except OSError:
                return None
            return res.stdout if res.returncode == 0 else None

        return prbody.build_body(
            self.item,
            brief_path=resolve_brief_path(self.config, self.item),
            plan_dir=plan_dir,
            phase_dir=phase_dir,
            base_branch=self.opt.base_branch,
            workspace=self.workspace,
            run_git=run_git,
        )

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def gate(self) -> None:
        """Run the gate commands in each touched package dir at HEAD; record the first failure.

        Per directory, the plain ``build.commands`` run first, then the ``[[build.gate]]`` entries that apply
        to it, in file order. An entry with ``packages`` applies only to a touched directory equal to one of
        them; an entry without ``packages`` applies to every touched directory. When neither source exists,
        when there are no changed files, or when only entries exist and none applies, note that the gate was
        not run and return. A failing command is recorded in ``gate_failure`` and noted; the remaining commands
        are skipped and nothing is raised, so the run still completes.
        """
        number = self.pr_number()
        self.write_item_config()
        commands = read_build_commands(self.spf)
        entries = read_gate_entries(self.spf)
        if commands is None and entries is None:
            self.note(f"gate not run for PR #{number}: no build.commands in {self.spf}")
            return
        changed = self.git("diff", "--name-only", f"origin/{self.opt.base_branch}...HEAD").stdout.split("\n")
        dirs = package_dirs(self.repo, [c for c in changed if c.strip()])
        if not dirs:
            self.note(f"gate not run for PR #{number}: no changed files to gate")
            return
        plan: list[tuple[Path, str]] = []
        for directory in dirs:
            plan.extend((directory, c) for c in commands or [])
            plan.extend((directory, e.run) for e in entries or [] if _gate_applies(e, self.repo, directory))
        if not plan:
            self.note(
                f"gate not run for PR #{number}: no [[build.gate]] entry applies to the touched package directories"
            )
            return
        for directory, command in plan:
            res = self.sh(shlex.split(command), directory)
            if res.returncode != 0:
                tail = _tail(res.stderr or res.stdout)
                self.gate_failure = GateFailure(command, directory, res.returncode, tail)
                self.rec.retries[GATE_FAILED_KEY] = 1
                self.rec.save()
                self.note(f"gate command '{command}' failed in {directory} (exit {res.returncode}): {tail}")
                return
        self.gated = True
        if self.rec.retries.get(GATE_FAILED_KEY):
            self.rec.retries[GATE_FAILED_KEY] = 0
            self.rec.save()


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
        if run.gate_failure is not None:
            f = run.gate_failure
            evidence.append(f"gate failed: {f.command} in {f.directory} (exit {f.returncode})")
        elif run.rec.retries.get(GATE_FAILED_KEY):
            evidence.append("gate failed in an earlier run")
        else:
            evidence.append("gate passed" if run.gated else "gate not run")
        if run.cleanup_done:
            evidence.append(run.cleanup_done)
        done = (DoneEntry(f"{run.item}: draft PR #{run.pr.number} opened", tuple(evidence)),)
    elif run.cleanup_done:
        done = (DoneEntry(f"{run.item}: local branch removed", (run.cleanup_done,)),)
    deviations = list(run.notes)
    if run.gate_failure is None and run.rec.retries.get(GATE_FAILED_KEY) and not run.gated:
        deviations.append(f"{run.item}: gate failed in an earlier run and was not rerun")
    return DigestReport(
        needs_you=needs,
        done=done,
        deviations=tuple(deviations),
        limits_hit=limits,
        timings=tuple(run.timings),
    )
