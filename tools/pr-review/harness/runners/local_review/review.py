"""Review phase: per-pass runner, mutation guard and the `run_local_review` CLI entry."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from harness.backend import Backend
from harness.config import HarnessConfig
from harness.lock import acquire_lock
from harness.repo_guard import head_sha as read_head_sha
from harness.runners.common import (
    build_file_review_section,
    build_subprocess_env,
    get_changed_files,
    get_file_diff,
    get_vibe_heal_context,
)
from harness.runners.local_review.address import (
    _make_backend,
    address_usage_error,
    collect_findings,
    run_address_phase,
    select_review_dir,
)
from harness.runners.local_review.findings import stamp_findings_file
from harness.runners.local_review.git import (
    _NO_UNTRACKED_FLAG,
    check_output_dir_outside_repo,
    check_preconditions,
    check_templates,
    git,
)
from harness.runners.local_review.prompts import (
    ADDRESS_TEMPLATE,
    DESIGN_TEMPLATE,
    FILE_TEMPLATE,
    REVIEW_TEMPLATES,
    SUMMARY_TEMPLATE,
    build_change_context,
    build_change_description,
    build_design_prompt,
    build_file_prompt,
    build_summary_prompt,
)
from harness.runners.local_review.store import (
    DESIGN_NAME,
    NO_FINDINGS_TEXT,
    STATUS_DONE,
    STATUS_FAILED,
    SUMMARY_NAME,
    _count_resolved,
    _file_output_path,
    _seed_manifest,
    _write_index,
    read_manifest,
    resolve_output_root,
    review_dir_for,
    write_manifest,
)
from harness.runners.local_review.types import (
    LocalReviewError,
    LocalReviewTarget,
    NothingToReviewError,
    RepoMutatedError,
)

logger = logging.getLogger(__name__)


@dataclass
class ReviewResult:
    review_dir: Path
    manifest: dict
    ran: list[str] = field(default_factory=list)  # pass names that made a backend call
    skipped: list[str] = field(default_factory=list)  # pass names skipped as already done
    failed: list[str] = field(default_factory=list)  # pass names that failed

    @property
    def ok(self) -> bool:
        return not self.failed


def _guard_violation(working_dir: Path, expected_head: str) -> str | None:
    """Description of what the backend changed, or None. Untracked files are ignored (11.A-10)."""
    problems: list[str] = []
    try:
        now = read_head_sha(working_dir)
    except Exception as exc:
        now = ""
        problems.append(f"HEAD unreadable ({exc})")
    if now and now != expected_head:
        problems.append(f"HEAD moved from {expected_head} to {now}")
    status = git(working_dir, "status", "--porcelain", _NO_UNTRACKED_FLAG)
    if status.returncode != 0:
        problems.append(f"git status failed: {status.stderr.strip()}")
    elif status.stdout.strip():
        changed = ", ".join(ln[3:] for ln in status.stdout.splitlines() if ln.strip())
        problems.append(f"tracked files changed: {changed}")
    return "; ".join(problems) or None


def _warn_discarded_statuses(review_dir: Path) -> None:
    counts = _count_resolved(review_dir)
    if counts["fixed"] or counts["declined"]:
        logger.warning(
            "--force discards address-phase statuses in %s: %d fixed, %d declined finding(s) will be overwritten",
            review_dir,
            counts["fixed"],
            counts["declined"],
        )


def _finish_file_output(out: Path) -> None:
    """Default a missing per-file output to "no findings", then stamp ids."""
    if not out.is_file():
        out.write_text(NO_FINDINGS_TEXT, encoding="utf-8")
    stamp_findings_file(out, "file")


def _single_pass_output_ok(name: str, out: Path, files_all_done: bool) -> bool:
    """Validate a finished summary/design pass: output present and non-empty; summary needs all files done."""
    if not out.is_file() or not out.read_text(encoding="utf-8").strip():
        logger.error("local-review %s: backend exited 0 but %s is missing or empty", name, out.name)
        return False
    if name == "summary" and not files_all_done:
        logger.warning("local-review summary: some file passes failed; summary recorded as failed")
        return False  # re-runs next time together with the failed files
    return True


class _ReviewPasses:
    """State and per-pass steps of one review-phase run (extracted from `run` to keep it flat)."""

    def __init__(
        self,
        config: HarnessConfig,
        target: LocalReviewTarget,
        review_dir: Path,
        manifest: dict,
        backend: Backend,
        env: dict[str, str],
        files: list[str],
        extra_knowledge: str | None,
    ) -> None:
        self.target = target
        self.review_dir = review_dir
        self.manifest = manifest
        self.backend = backend
        self.env = env
        self.files = files
        self.extra_knowledge = extra_knowledge
        self.wdir = Path(config.repo.working_dir)
        self.wdir_s = str(self.wdir)
        knowledge = Path(config.harness.knowledge_dir) / "pr-review"
        self.file_instr = (knowledge / FILE_TEMPLATE).read_text(encoding="utf-8")
        self.summary_instr = (knowledge / SUMMARY_TEMPLATE).read_text(encoding="utf-8")
        self.design_instr = (knowledge / DESIGN_TEMPLATE).read_text(encoding="utf-8")
        description = build_change_description(self.wdir, target)
        self.context = build_change_context(config.repo.name, self.wdir, target, description)
        self.vibe = get_vibe_heal_context(config.repo.subdirs, self.wdir_s, target.branch) or None
        self.result = ReviewResult(review_dir=review_dir, manifest=manifest)
        self.guard_head = read_head_sha(self.wdir)
        self.guard_errors: list[str] = []
        self.diff_cache: dict[str, str] = {}

    def section_for(self, f: str) -> str:
        if f not in self.diff_cache:
            self.diff_cache[f] = get_file_diff(
                f, self.target.base_ref, self.wdir_s, self.env, rev_range=f"{self.target.base_sha} HEAD"
            )
        return build_file_review_section(f, self.diff_cache[f], os.path.join(self.wdir_s, f))

    def call_backend(self, prompt: str, label: str) -> bool:
        """True when the backend exited 0 without timing out and the repo is unchanged."""
        ok = False
        try:
            proc = self.backend.run(prompt, cwd=self.wdir_s, context=label)
            ok = proc.returncode == 0
            if not ok:
                logger.error("%s: backend exited %d", label, proc.returncode)
        except subprocess.TimeoutExpired:
            logger.exception("%s: backend timed out", label)
        except Exception:
            logger.exception("%s: backend call failed", label)
        violation = _guard_violation(self.wdir, self.guard_head)
        if violation:
            msg = f"{label}: backend modified the repository: {violation}"
            self.guard_errors.append(msg)
            logger.error("%s", msg)
            return False
        return ok

    def record(self, name: str, ok: bool, store: Callable[[str], None]) -> None:
        store(STATUS_DONE if ok else STATUS_FAILED)
        write_manifest(self.review_dir, self.manifest)
        if not ok:
            self.result.failed.append(name)

    def file_pass(self, f: str) -> None:
        if self.manifest["files"].get(f) == STATUS_DONE:
            self.result.skipped.append(f"file:{f}")
            return
        out = _file_output_path(self.review_dir, f)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        section = self.section_for(f)
        prompt = build_file_prompt(self.file_instr, out, section, self.context, self.extra_knowledge, self.vibe)
        self.result.ran.append(f"file:{f}")
        ok = self.call_backend(prompt, f"local-review file {f}")
        if ok:
            _finish_file_output(out)
        self.record(f"file:{f}", ok, lambda st: self.manifest["files"].__setitem__(f, st))

    def run_file_passes(self) -> None:
        for f in self.files:
            if self.guard_errors:
                break
            self.file_pass(f)

    def single_pass(
        self, name: str, filename: str, build: Callable[[Path], str], stamp: str | None, files_all_done: bool
    ) -> None:
        if self.guard_errors:
            return
        if self.manifest.get(name) == STATUS_DONE and (name != "summary" or files_all_done):
            self.result.skipped.append(name)
            return
        out = self.review_dir / filename
        out.unlink(missing_ok=True)
        self.result.ran.append(name)
        ok = self.call_backend(build(out), f"local-review {name}")
        ok = ok and _single_pass_output_ok(name, out, files_all_done)
        if ok and stamp:
            stamp_findings_file(out, stamp)
        self.record(name, ok, lambda st: self.manifest.__setitem__(name, st))

    def run_summary_and_design(self) -> None:
        files_all_done = all(self.manifest["files"].get(f) == STATUS_DONE for f in self.files)
        self.single_pass(
            "summary",
            SUMMARY_NAME,
            lambda out: build_summary_prompt(
                self.summary_instr, out, self.files, self.context, self.extra_knowledge, self.vibe
            ),
            None,
            files_all_done,
        )
        self.single_pass(
            "design",
            DESIGN_NAME,
            lambda out: build_design_prompt(
                self.design_instr,
                out,
                "".join(self.section_for(f) for f in self.files),
                self.context,
                self.extra_knowledge,
                self.vibe,
            ),
            "design",
            files_all_done,
        )


def run(
    config: HarnessConfig,
    target: LocalReviewTarget,
    *,
    output_root: Path,
    force: bool = False,
    backend: Backend | None = None,
    extra_knowledge: str | None = None,
) -> ReviewResult:
    """Review phase after the lock is held and `check_preconditions` / `check_templates` passed.

    Writes only under `<output_root>/<repo_slug>/<branch>/<head_sha>/`. Passes: one per changed
    file not yet `done`, then summary, then design. The manifest is rewritten atomically after
    every pass. Returns a ReviewResult (`ok` False when any pass failed; caller maps to exit code).
    Raises RepoMutatedError (after writing manifest and index) when the backend moved HEAD or
    changed tracked files; LocalReviewError for an unusable branch name / output root.
    """
    wdir = Path(config.repo.working_dir)
    check_output_dir_outside_repo(wdir, output_root)
    review_dir = review_dir_for(output_root.expanduser(), config.repo_slug, target.branch, target.head_sha)
    env = build_subprocess_env(config.harness.path_prepend, config.harness.env)
    # keep user git config (color.diff=always) out of the diff text sent to the model
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "color.diff", "GIT_CONFIG_VALUE_0": "never"})
    if backend is None:
        backend = _make_backend(config)

    previous = None if force else read_manifest(review_dir)
    if force:
        _warn_discarded_statuses(review_dir)
    manifest = _seed_manifest(target, previous)

    files = get_changed_files(target.base_ref, str(wdir), env, rev_range=f"{target.base_sha}..HEAD")
    for f in files:
        _file_output_path(review_dir, f)  # validate before any backend call
    review_dir.mkdir(parents=True, exist_ok=True)
    manifest["files"] = {f: manifest["files"][f] for f in files if f in manifest["files"]}

    passes = _ReviewPasses(config, target, review_dir, manifest, backend, env, files, extra_knowledge)
    passes.run_file_passes()
    passes.run_summary_and_design()

    write_manifest(review_dir, manifest)
    _write_index(review_dir, manifest, files)
    if passes.guard_errors:
        raise RepoMutatedError("; ".join(passes.guard_errors))
    return passes.result


def _pass_lines(result: ReviewResult) -> list[str]:
    manifest = result.manifest
    statuses = list(manifest.get("files", {}).values())
    lines = [f"per-file: {sum(1 for st in statuses if st == STATUS_DONE)}/{len(statuses)} done"]
    for name in ("summary", "design"):
        lines.append(f"{name}: {manifest.get(name, 'not run')}")
    return lines


def run_local_review(
    config: HarnessConfig,
    base: str | None = None,
    output_dir: str | Path | None = None,
    force: bool = False,
    address: bool = False,
    skip_review: bool = False,
    review_dir: str | Path | None = None,
    *,
    backend: Backend | None = None,
) -> int:
    """CLI entry for `harness run local-review`; returns the process exit code.

    Takes the shared checkout lock first (same shape as `self_review.run`), then runs the
    locked body. The module-level `run` is the phase-05 review-phase runner and keeps its name,
    so this entry is `run_local_review`. `skip_review` and `review_dir` are only valid with
    `address` (and `review_dir` only with `skip_review`); violations are usage errors (11.A-19).
    """
    usage_error = address_usage_error(address, skip_review, review_dir)
    if usage_error:
        raise LocalReviewError(usage_error)
    with acquire_lock(config.lock_key):
        return _run_locked(
            config, base, output_dir, force, backend, address=address, skip_review=skip_review, review_dir=review_dir
        )


def _run_review_phase(
    config: HarnessConfig,
    target: LocalReviewTarget,
    output_root: Path,
    force: bool,
    backend: Backend | None,
) -> ReviewResult | None:
    """Run the review passes and print the summary; None when the backend mutated the repo (exit 1)."""
    extra = None
    kfile = config.harness.review_knowledge_file
    if kfile and kfile.exists():
        extra = kfile.read_text(encoding="utf-8")
    try:
        result = run(config, target, output_root=output_root, force=force, backend=backend, extra_knowledge=extra)
    except RepoMutatedError as exc:
        rdir = review_dir_for(output_root, config.repo_slug, target.branch, target.head_sha)
        print(f"Review directory: {rdir}")
        print(f"ERROR: {exc}", file=sys.stderr)
        return None
    print(f"Review directory: {result.review_dir}")
    for line in _pass_lines(result):
        print(line)
    return result


def _select_and_address(
    config: HarnessConfig,
    target: LocalReviewTarget,
    output_root: Path,
    backend: Backend | None,
    review_dir: str | Path | None,
    skip_review: bool,
    reviewed_dir: Path | None,
) -> bool:
    chosen = select_review_dir(
        config, target, output_root, review_dir=review_dir, skip_review=skip_review, reviewed_dir=reviewed_dir
    )
    print(f"Addressing review: {chosen}")
    collection = collect_findings(chosen)
    print(
        f"Findings to address: {len(collection.open)} open, "
        f"{collection.skipped_not_open} skipped (not open), {collection.unparseable} unparseable"
    )
    return run_address_phase(config, target, chosen, collection, backend=backend)


def _check_preconditions_or_none(
    config: HarnessConfig, base: str | None, output_root: Path, skip_review: bool
) -> LocalReviewTarget | None:
    """The review target; None (message printed) when there is nothing to review (exit 0)."""
    try:
        return check_preconditions(
            Path(config.repo.working_dir),
            base=base,
            config_base=config.local_review.base,
            output_root=output_root,
            review_phase=not skip_review,
        )
    except NothingToReviewError as exc:
        print(str(exc))
        return None


def _check_phase_templates(config: HarnessConfig, skip_review: bool, address: bool) -> None:
    templates = ([] if skip_review else list(REVIEW_TEMPLATES)) + ([ADDRESS_TEMPLATE] if address else [])
    check_templates(Path(config.harness.knowledge_dir), templates)


def _exit_code(review_ok: bool, address_ok: bool = True) -> int:
    return 0 if (review_ok and address_ok) else 1


def _run_phases(
    config: HarnessConfig,
    base: str | None,
    output_dir: str | Path | None,
    force: bool,
    backend: Backend | None,
    *,
    address: bool,
    skip_review: bool,
    review_dir: str | Path | None,
) -> int:
    """Preconditions, templates, review phase, address phase; may raise LocalReviewError."""
    output_root = resolve_output_root(config, output_dir)
    target = _check_preconditions_or_none(config, base, output_root, skip_review)
    if target is None:
        return 0
    _check_phase_templates(config, skip_review, address)
    print(f"Base: {target.base_ref} (merge base {target.base_sha})")
    print(f"Head: {target.head_sha}")
    review_ok = True
    reviewed_dir: Path | None = None
    if not skip_review:
        result = _run_review_phase(config, target, output_root, force, backend)
        if result is None:
            return 1
        review_ok = result.ok
        reviewed_dir = result.review_dir
    if not address:
        return _exit_code(review_ok)
    address_ok = _select_and_address(config, target, output_root, backend, review_dir, skip_review, reviewed_dir)
    return _exit_code(review_ok, address_ok)


def _run_locked(
    config: HarnessConfig,
    base: str | None,
    output_dir: str | Path | None,
    force: bool,
    backend: Backend | None,
    *,
    address: bool = False,
    skip_review: bool = False,
    review_dir: str | Path | None = None,
) -> int:
    try:
        return _run_phases(
            config, base, output_dir, force, backend, address=address, skip_review=skip_review, review_dir=review_dir
        )
    except LocalReviewError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
