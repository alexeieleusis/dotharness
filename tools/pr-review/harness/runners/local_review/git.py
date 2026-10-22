"""Git preconditions, base-ref resolution and the single `git` helper for `harness run local-review`.

Everything here is offline and read-only: no `gh`, no `git fetch`/`pull`/
`remote update`, nothing written inside the reviewed repo. Failures raise
`LocalReviewError` (caller prints the message to stderr and exits non-zero);
nothing-to-review raises the unrelated `NothingToReviewError` (caller prints
the message and exits 0), so the two cannot be confused.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from harness.runners.common import build_subprocess_env, run_cmd
from harness.runners.local_review.types import LocalReviewError, LocalReviewTarget, NothingToReviewError

_GIT_TIMEOUT_SECONDS = 30
_GIT_TIMEOUT_RETURNCODE = 124  # same convention as timeout(1)
MAX_DIRTY_PATHS_SHOWN = 5
DEFAULT_BASE_FALLBACK = "main"
_NO_UNTRACKED_FLAG = "--untracked-files=no"


def git(working_dir: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run `git *args` in `working_dir` through `common.run_cmd`; never raises on failure or timeout.

    Contract: 30s timeout (`_GIT_TIMEOUT_SECONDS`), `check=False`, stdout/stderr decoded to text.
    A timeout is returned as returncode 124 with empty stdout and a "timed out" stderr.
    Environment: the current process environment unless the caller passes an explicit `env`.
    """
    cmd = ["git", *args]
    try:
        proc = run_cmd(
            cmd,
            cwd=str(working_dir),
            env=dict(os.environ) if env is None else env,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Surface a timeout as a failed command so callers' returncode handling covers it.
        return subprocess.CompletedProcess(
            cmd, _GIT_TIMEOUT_RETURNCODE, "", f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS}s"
        )
    return subprocess.CompletedProcess(
        cmd,
        proc.returncode,
        _decode(proc.stdout),
        _decode(proc.stderr),
    )


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    return data if isinstance(data, str) else data.decode("utf-8", errors="replace")


def _ref_exists(working_dir: Path, ref: str) -> bool:
    return git(working_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def resolve_base_ref(working_dir: Path, base: str | None = None, config_base: str | None = None) -> str:
    """First existing ref in order: `base` (--base), `config_base`, origin/HEAD target, `main`.

    An explicit `base` or `config_base` that does not resolve is an error and never
    falls through. The origin/HEAD and `main` rules fall through quietly.
    """
    for label, value in (("--base", base), ("[local_review].base", config_base)):
        if value:
            if _ref_exists(working_dir, value):
                return value
            raise LocalReviewError(f"base ref {value!r} (from {label}) does not resolve to a commit")  # noqa: TRY003

    tried: list[str] = []
    origin_head = git(working_dir, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    target = origin_head.stdout.strip() if origin_head.returncode == 0 else ""
    if target:
        if _ref_exists(working_dir, target):
            return target
        tried.append(f"{target} (origin/HEAD)")
    else:
        tried.append("origin/HEAD (not set)")
    if _ref_exists(working_dir, DEFAULT_BASE_FALLBACK):
        return DEFAULT_BASE_FALLBACK
    tried.append(DEFAULT_BASE_FALLBACK)
    raise LocalReviewError(
        "could not resolve a base ref; tried: --base (not given), [local_review].base (not set), "
        + ", ".join(tried)
        + ". Pass --base REF."
    )


def compute_merge_base(working_dir: Path, base_ref: str) -> str:
    """`git merge-base <base_ref> HEAD`."""
    result = git(working_dir, "merge-base", base_ref, "HEAD")
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        raise LocalReviewError(  # noqa: TRY003
            f"no merge base between {base_ref!r} and HEAD: {result.stderr.strip() or 'unrelated histories'}"
        )
    return sha


def check_output_dir_outside_repo(working_dir: Path, output_root: Path) -> None:
    """Refuse an output root equal to or inside `working_dir` (after ~ and symlink resolution)."""
    repo = working_dir.expanduser().resolve()
    out = output_root.expanduser().resolve()
    if out == repo or repo in out.parents:
        raise LocalReviewError(  # noqa: TRY003
            f"output directory {out} is inside the reviewed repo {repo}; choose a location outside it"
        )


def check_templates(knowledge_dir: Path, names: Sequence[str]) -> None:
    """Every `names` entry must exist under `knowledge_dir/pr-review/`."""
    for name in names:
        path = knowledge_dir / "pr-review" / name
        if not path.is_file():
            raise LocalReviewError(f"prompt template not found: {path}")  # noqa: TRY003


def check_preconditions(
    working_dir: Path,
    *,
    base: str | None = None,
    config_base: str | None = None,
    output_root: Path | None = None,
    review_phase: bool = True,
) -> LocalReviewTarget:
    """FR-2 items 1-5 plus the output-dir-inside-repo refusal. Template checks are separate.

    Raises LocalReviewError on failure, NothingToReviewError when there is nothing to review
    (only when `review_phase`).
    """
    working_dir = working_dir.expanduser().resolve()
    if output_root is not None:
        check_output_dir_outside_repo(working_dir, output_root)

    top = git(working_dir, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise LocalReviewError(f"{working_dir} is not inside a git repository")  # noqa: TRY003
    if Path(top.stdout.strip()).resolve() != working_dir:
        raise LocalReviewError(  # noqa: TRY003
            f"repo.working_dir {working_dir} is not the toplevel of its git repo ({top.stdout.strip()})"
        )

    head_ref = git(working_dir, "symbolic-ref", "--short", "HEAD")
    branch = head_ref.stdout.strip()
    if head_ref.returncode != 0 or not branch:
        raise LocalReviewError("HEAD is detached; check out a branch to review")  # noqa: TRY003

    status = git(working_dir, "status", "--porcelain", _NO_UNTRACKED_FLAG)
    if status.returncode != 0:
        raise LocalReviewError(f"git status failed: {status.stderr.strip()}")  # noqa: TRY003
    dirty = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    if dirty:
        shown = ", ".join(dirty[:MAX_DIRTY_PATHS_SHOWN])
        more = f" (and {len(dirty) - MAX_DIRTY_PATHS_SHOWN} more)" if len(dirty) > MAX_DIRTY_PATHS_SHOWN else ""
        raise LocalReviewError(  # noqa: TRY003
            f"uncommitted changes to tracked files: {shown}{more}; uncommitted changes are not reviewed "
            "- commit or stash them first"
        )

    base_ref = resolve_base_ref(working_dir, base, config_base)
    head = git(working_dir, "rev-parse", "HEAD")
    head_sha = head.stdout.strip()
    if head.returncode != 0 or not head_sha:
        raise LocalReviewError(f"cannot resolve HEAD: {head.stderr.strip()}")  # noqa: TRY003
    merge_base = compute_merge_base(working_dir, base_ref)

    if review_phase and (branch == base_ref or merge_base == head_sha):
        raise NothingToReviewError(f"Nothing to review: {branch} has no commits beyond {base_ref}")  # noqa: TRY003

    return LocalReviewTarget(branch=branch, base_ref=base_ref, base_sha=merge_base, head_sha=head_sha)


def _git_env() -> dict[str, str]:
    return build_subprocess_env([], {})


def _commit_time(wdir: Path, sha: str) -> int:
    proc = git(wdir, "show", "-s", "--format=%ct", sha)
    try:
        return int(proc.stdout.strip()) if proc.returncode == 0 else -1
    except ValueError:
        return -1


def _current_branch(wdir: Path) -> str:
    proc = git(wdir, "symbolic-ref", "--short", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _tracked_dirty(wdir: Path) -> list[str]:
    """Tracked paths with uncommitted changes (untracked files ignored, 11.A-10). Raises on git failure."""
    proc = git(wdir, "status", "--porcelain", _NO_UNTRACKED_FLAG)
    if proc.returncode != 0:
        raise LocalReviewError(f"git status failed: {proc.stderr.strip()}")  # noqa: TRY003
    return [ln[3:] for ln in proc.stdout.splitlines() if ln.strip()]


def _head(wdir: Path) -> str:
    proc = git(wdir, "rev-parse", "HEAD")
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise LocalReviewError(f"cannot resolve HEAD: {proc.stderr.strip()}")  # noqa: TRY003
    return sha


def _commit_parents(wdir: Path, rev: str) -> list[str]:
    proc = git(wdir, "rev-list", "--parents", "-n", "1", rev)
    return proc.stdout.split()[1:] if proc.returncode == 0 else []
