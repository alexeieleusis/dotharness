"""Address phase: review selection, finding collection and the per-finding address loop."""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from harness.backend import Backend
from harness.config import HarnessConfig
from harness.runners.common import build_subprocess_env, get_file_diff, get_vibe_heal_context, is_ancestor
from harness.runners.local_review.findings import AddressFinding, parse_findings
from harness.runners.local_review.git import (
    MAX_DIRTY_PATHS_SHOWN,
    _commit_parents,
    _commit_time,
    _current_branch,
    _git_env,
    _head,
    _tracked_dirty,
)
from harness.runners.local_review.prompts import ADDRESS_TEMPLATE, build_address_prompt
from harness.runners.local_review.store import (
    DESIGN_NAME,
    MANIFEST_NAME,
    RESOLUTIONS_DIR,
    STATUS_DONE,
    _file_output_path,
    read_manifest,
    review_dir_for,
    write_finding_status,
)
from harness.runners.local_review.types import LocalReviewError, LocalReviewTarget

logger = logging.getLogger(__name__)

OUTCOME_FIXED = "fixed"
OUTCOME_DECLINED = "declined"
OUTCOME_FAILED = "failed"


def address_usage_error(address: bool, skip_review: bool, review_dir: str | Path | None) -> str | None:
    if skip_review and not address:
        return "--skip-review requires --address"
    if review_dir and not address:
        return "--review-dir requires --address"
    if review_dir and not skip_review:
        return "--review-dir requires --skip-review"
    return None


@dataclass(frozen=True)
class FindingCollection:
    open: list[AddressFinding]  # ordered: per-file by path then line, then design in file order
    skipped_not_open: int
    unparseable: int


def _manifest_matches(manifest: dict | None, target: LocalReviewTarget, wdir: Path) -> bool:
    if manifest is None or manifest.get("branch") != target.branch:
        return False
    sha = manifest.get("head_sha")
    if not isinstance(sha, str) or not sha:
        return False
    if sha == target.head_sha:
        return True
    return is_ancestor(sha, target.head_sha, str(wdir), _git_env())


def _check_explicit_review_dir(chosen: Path, target: LocalReviewTarget, wdir: Path) -> Path:
    manifest = read_manifest(chosen)
    if manifest is None:
        raise LocalReviewError(f"{chosen} does not contain a usable {MANIFEST_NAME}")  # noqa: TRY003
    if manifest.get("branch") != target.branch:
        raise LocalReviewError(  # noqa: TRY003
            f"review in {chosen} is for branch {manifest.get('branch')!r}, not {target.branch!r}"
        )
    if not _manifest_matches(manifest, target, wdir):
        raise LocalReviewError(  # noqa: TRY003
            f"review in {chosen} was made at {manifest.get('head_sha')}, which is not HEAD or an ancestor of HEAD"
        )
    return chosen


def _matching_review_candidates(
    branch_dir: Path, target: LocalReviewTarget, wdir: Path
) -> list[tuple[int, float, str, Path]]:
    """(commit time, manifest mtime, name, dir) for each review under `branch_dir` that matches `target`."""
    candidates: list[tuple[int, float, str, Path]] = []
    if not branch_dir.is_dir():
        return candidates
    for cand in sorted(branch_dir.iterdir()):
        manifest = read_manifest(cand) if cand.is_dir() else None
        if not _manifest_matches(manifest, target, wdir):
            continue
        sha = str((manifest or {}).get("head_sha"))
        try:
            mtime = (cand / MANIFEST_NAME).stat().st_mtime
        except OSError:
            continue
        candidates.append((_commit_time(wdir, sha), mtime, cand.name, cand))
    return candidates


def select_review_dir(
    config: HarnessConfig,
    target: LocalReviewTarget,
    output_root: Path,
    *,
    review_dir: str | Path | None = None,
    skip_review: bool = False,
    reviewed_dir: Path | None = None,
) -> Path:
    """FR-10 step 1 (7.2). Offline; raises LocalReviewError when no review directory qualifies."""
    wdir = Path(config.repo.working_dir).expanduser().resolve()
    if review_dir:
        return _check_explicit_review_dir(Path(review_dir).expanduser(), target, wdir)
    if not skip_review:
        chosen = reviewed_dir or review_dir_for(output_root, config.repo_slug, target.branch, target.head_sha)
        if read_manifest(chosen) is None:
            raise LocalReviewError(f"no completed review found in {chosen}")  # noqa: TRY003
        return chosen
    branch_dir = review_dir_for(output_root, config.repo_slug, target.branch, target.head_sha).parent
    candidates = _matching_review_candidates(branch_dir, target, wdir)
    if not candidates:
        raise LocalReviewError(  # noqa: TRY003
            f"no earlier review found for branch {target.branch!r} under {branch_dir}; run `harness run local-review` first"
        )
    return max(candidates)[3]


def _read_findings(path: Path, pass_name: str) -> tuple[list[AddressFinding], int, int]:
    """(open findings, skipped-not-open count, unparseable count) for one review markdown file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("cannot read %s; skipping", path)
        return [], 0, 0
    found: list[AddressFinding] = []
    skipped = unparseable = 0
    for blk in parse_findings(text):
        if not blk.valid or not blk.id:
            logger.warning("ignoring unparseable or unstamped finding %r in %s", blk.title, path)
            unparseable += 1
        elif blk.status != "open":
            skipped += 1
        else:
            found.append(AddressFinding(blk.id, pass_name, path, blk))
    return found, skipped, unparseable


def collect_findings(review_dir: Path) -> FindingCollection:
    """FR-10 step 2 (7.3). Reads only `done` passes; returns open findings in deterministic order."""
    manifest = read_manifest(review_dir)
    if manifest is None:
        raise LocalReviewError(f"{review_dir} does not contain a usable {MANIFEST_NAME}")  # noqa: TRY003
    sources = [
        (_file_output_path(review_dir, changed), "file", True)
        for changed in sorted(path for path, st in manifest["files"].items() if st == STATUS_DONE)
    ]
    if manifest.get("design") == STATUS_DONE:
        sources.append((review_dir / DESIGN_NAME, "design", False))
    open_findings: list[AddressFinding] = []
    skipped = unparseable = 0
    for path, pass_name, by_line in sources:
        found, n_skipped, n_unparseable = _read_findings(path, pass_name)
        open_findings.extend(sorted(found, key=lambda f: f.line) if by_line else found)  # stable on ties
        skipped += n_skipped
        unparseable += n_unparseable
    return FindingCollection(open_findings, skipped, unparseable)


@dataclass
class AddressResult:
    """Per-finding outcomes of the address loop (finding id -> fixed|declined|failed)."""

    outcomes: dict[str, str] = field(default_factory=dict)
    not_reached: int = 0  # open findings never sent because the phase stopped early
    stop_reason: str | None = None

    def count(self, outcome: str) -> int:
        return sum(1 for v in self.outcomes.values() if v == outcome)

    @property
    def stopped(self) -> bool:
        return self.stop_reason is not None


def _state_problem(wdir: Path, branch: str) -> str | None:
    """Description of a wrong branch or a dirty tracked tree, or None (FR-2 items 1-3 re-check)."""
    try:
        current = _current_branch(wdir)
        if current != branch:
            return f"current branch is {current or '(detached HEAD)'!r}, expected {branch!r}"
        dirty = _tracked_dirty(wdir)
    except LocalReviewError as exc:
        return str(exc)
    if dirty:
        shown = ", ".join(dirty[:MAX_DIRTY_PATHS_SHOWN])
        more = f" (and {len(dirty) - MAX_DIRTY_PATHS_SHOWN} more)" if len(dirty) > MAX_DIRTY_PATHS_SHOWN else ""
        return f"uncommitted changes to tracked files: {shown}{more}"
    return None


def _resolution_decision(path: Path) -> str | None:
    """`fixed` / `declined` from the first line of the resolution file, else None."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.strip().splitlines()
    first = lines[0].strip().lower() if lines else ""
    for decision in (OUTCOME_FIXED, OUTCOME_DECLINED):
        if first == f"decision: {decision}":
            return decision
    return None


def _make_backend(config: HarnessConfig) -> Backend:
    return Backend(
        config.harness.backend,
        config.harness.backend_timeout_seconds,
        config.harness.path_prepend,
        dict(config.harness.env),
        expected_repo_name=config.repo.name if config.repo.name_provided else None,
    )


@dataclass(frozen=True)
class _AddressContext:
    target: LocalReviewTarget
    wdir: Path
    env: dict[str, str]
    backend: Backend
    instructions: str
    vibe: str | None
    review_head: str
    res_dir: Path

    @property
    def wdir_s(self) -> str:
        return str(self.wdir)


def _fail_finding(result: AddressResult, finding: AddressFinding, reason: str) -> None:
    result.outcomes[finding.id] = OUTCOME_FAILED
    logger.error("finding %s: failed (%s)", finding.id, reason)


def _stop_phase(result: AddressResult, finding: AddressFinding, reason: str) -> None:
    result.stop_reason = reason
    logger.error("address phase stopped: %s", reason)
    _fail_finding(result, finding, reason)


def _call_address_backend(ctx: _AddressContext, finding: AddressFinding, prompt: str) -> str:
    """Run the backend for one finding; return an error description, or "" when it exited 0."""
    try:
        proc = ctx.backend.run(prompt, cwd=ctx.wdir_s, context=f"local-review address {finding.id}")
    except subprocess.TimeoutExpired:
        return "backend timed out"
    except Exception as exc:
        logger.exception("finding %s: backend call failed", finding.id)
        return f"backend call failed ({exc})"
    return "" if proc.returncode == 0 else f"backend exited {proc.returncode}"


def _post_call_stop_reasons(ctx: _AddressContext, pre_sha: str) -> tuple[str, list[str]]:
    """7.4 checks after a backend call; return (new HEAD or "", reasons to stop the phase)."""
    reasons: list[str] = []
    try:
        new_head = _head(ctx.wdir)
        if not is_ancestor(pre_sha, new_head, ctx.wdir_s, ctx.env):
            reasons.append(f"history rewritten: {pre_sha} is not an ancestor of HEAD {new_head}")
        now_branch = _current_branch(ctx.wdir)
        if now_branch != ctx.target.branch:
            reasons.append(f"branch changed from {ctx.target.branch!r} to {now_branch or '(detached HEAD)'!r}")
        dirty = _tracked_dirty(ctx.wdir)
        if dirty:
            reasons.append(f"tracked files have uncommitted changes: {', '.join(dirty[:MAX_DIRTY_PATHS_SHOWN])}")
    except LocalReviewError as exc:
        return "", [str(exc)]
    return new_head, reasons


def _verify_resolution(
    ctx: _AddressContext, resolution: Path, pre_sha: str, new_head: str
) -> tuple[str, str | None, str]:
    """7.3: cross-check the resolution file against git; return (decision, commit, error)."""
    decision = _resolution_decision(resolution)
    if decision is None:
        return "", None, f"resolution file {resolution} is missing, empty or its first line is not 'decision: ...'"
    if decision == OUTCOME_FIXED:
        if new_head == pre_sha or _commit_parents(ctx.wdir, new_head) != [pre_sha]:
            return decision, None, f"decision is fixed but HEAD is not exactly one new commit on {pre_sha[:12]}"
        return decision, new_head, ""
    if new_head != pre_sha:
        return decision, None, "decision is declined but HEAD moved"
    return decision, None, ""


def _pre_call_head(ctx: _AddressContext, finding: AddressFinding, result: AddressResult) -> str | None:
    """HEAD before the backend call, or None after stopping the phase on a bad state."""
    problem = _state_problem(ctx.wdir, ctx.target.branch)
    if problem:
        _stop_phase(result, finding, f"before finding {finding.id}: {problem}")
        return None
    try:
        return _head(ctx.wdir)
    except LocalReviewError as exc:
        _stop_phase(result, finding, str(exc))
        return None


def _run_address_backend(ctx: _AddressContext, finding: AddressFinding, resolution: Path) -> str:
    """Build the address prompt (with the file diff) and run the backend; return its error or ""."""
    diff = ""
    if finding.file:
        diff = get_file_diff(
            finding.file, ctx.target.base_ref, ctx.wdir_s, ctx.env, rev_range=f"{ctx.target.base_sha} HEAD"
        )
    prompt = build_address_prompt(ctx.instructions, finding, resolution.resolve(), ctx.review_head, diff, ctx.vibe)
    return _call_address_backend(ctx, finding, prompt)


def _record_outcome(result: AddressResult, finding: AddressFinding, decision: str, commit: str | None) -> None:
    """Write the finding status and record `fixed` / `declined`; a write error fails the finding."""
    try:
        write_finding_status(finding.source_file, finding.id, decision, commit)
    except (LocalReviewError, OSError) as exc:
        _fail_finding(result, finding, f"cannot record status: {exc}")
        return
    outcome = OUTCOME_FIXED if decision == OUTCOME_FIXED else OUTCOME_DECLINED
    result.outcomes[finding.id] = outcome
    logger.info("finding %s: %s%s", finding.id, outcome, f" ({commit})" if commit else "")


def _address_one(ctx: _AddressContext, finding: AddressFinding, result: AddressResult) -> bool:
    """Address one finding and record its outcome; return False when the phase must stop."""
    pre_sha = _pre_call_head(ctx, finding, result)
    if pre_sha is None:
        return False
    resolution = ctx.res_dir / f"{finding.id}.md"
    if resolution.resolve().parent != ctx.res_dir.resolve():
        _fail_finding(result, finding, "finding id resolves outside the resolutions directory")
        return True
    resolution.unlink(missing_ok=True)  # a stale file must not be mistaken for this run's answer
    backend_error = _run_address_backend(ctx, finding, resolution)

    # 7.4: stop-the-phase checks, after every call, even a failed one. No repair (G3).
    new_head, stop_reasons = _post_call_stop_reasons(ctx, pre_sha)
    if stop_reasons:
        _stop_phase(result, finding, f"after finding {finding.id}: " + "; ".join(stop_reasons))
        return False
    if backend_error:
        _fail_finding(result, finding, backend_error)
        return True

    decision, commit, error = _verify_resolution(ctx, resolution, pre_sha, new_head)
    if error:
        _fail_finding(result, finding, error)
    else:
        _record_outcome(result, finding, decision, commit)
    return True


def _build_address_context(
    config: HarnessConfig, target: LocalReviewTarget, review_dir: Path, backend: Backend | None
) -> _AddressContext:
    wdir = Path(config.repo.working_dir).expanduser().resolve()
    env = build_subprocess_env(config.harness.path_prepend, config.harness.env)
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "color.diff", "GIT_CONFIG_VALUE_0": "never"})
    review_head = target.head_sha
    manifest = read_manifest(review_dir)
    if manifest and isinstance(manifest.get("head_sha"), str):
        review_head = manifest["head_sha"]
    return _AddressContext(
        target=target,
        wdir=wdir,
        env=env,
        backend=backend or _make_backend(config),
        instructions=(Path(config.harness.knowledge_dir) / "pr-review" / ADDRESS_TEMPLATE).read_text(encoding="utf-8"),
        vibe=get_vibe_heal_context(config.repo.subdirs, str(wdir), target.branch) or None,
        review_head=review_head,
        res_dir=review_dir / RESOLUTIONS_DIR,
    )


def _address_loop(ctx: _AddressContext, pending: list[AddressFinding], result: AddressResult) -> None:
    """Address each pending finding in order; on a stop, count the rest as not reached."""
    for index, finding in enumerate(pending):
        if not _address_one(ctx, finding, result):
            result.not_reached = len(pending) - index - 1
            break


def address_findings(
    config: HarnessConfig,
    target: LocalReviewTarget,
    review_dir: Path,
    collection: FindingCollection,
    *,
    backend: Backend | None = None,
) -> AddressResult:
    """FR-10 steps 3-4: one backend call and at most one commit per open finding, verified by git.

    A finding becomes `fixed` / `declined` only when the resolution file and git agree (7.3);
    otherwise it is `failed` and keeps `status: open`. Stops (no repair, no further backend calls)
    when history was rewritten, tracked files are dirty, or the branch changed (7.4).
    """
    ctx = _build_address_context(config, target, review_dir, backend)
    result = AddressResult()
    pending = list(collection.open)
    ctx.res_dir.mkdir(parents=True, exist_ok=True)
    _address_loop(ctx, pending, result)
    return result


def address_summary_line(collection: FindingCollection, result: AddressResult) -> str:
    skipped = collection.skipped_not_open + result.not_reached
    return (
        f"findings: {result.count(OUTCOME_FIXED)} fixed, {result.count(OUTCOME_DECLINED)} declined, "
        f"{result.count(OUTCOME_FAILED)} failed, {skipped} skipped (not open), {collection.unparseable} unparseable"
    )


def run_address_phase(
    config: HarnessConfig,
    target: LocalReviewTarget,
    review_dir: Path,
    collection: FindingCollection,
    *,
    backend: Backend | None = None,
) -> bool:
    """Run the address loop, print the FR-9 summary line, return True when the exit code should be 0.

    False when any finding failed, any block was unparseable, or the phase stopped early.
    Not-reached findings after an early stop count as skipped, not failed.
    """
    result = address_findings(config, target, review_dir, collection, backend=backend)
    print(address_summary_line(collection, result))
    if result.stopped:
        print(f"ERROR: address phase stopped early: {result.stop_reason}", file=sys.stderr)
    return not (result.stopped or result.count(OUTCOME_FAILED) or collection.unparseable)
