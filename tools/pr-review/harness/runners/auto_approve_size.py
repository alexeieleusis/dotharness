"""Pure size evaluation for the auto-approve command.

Decides, from ``git diff origin/<base>...HEAD`` on a checked-out PR branch, which files
are small enough to mark viewed and whether the whole PR is small enough to approve.
Read-only: no network and no AI/backend call.
"""

from dataclasses import dataclass, field

from harness.config import AutoApproveConfig
from harness.runners.common import TIMEOUT_GIT, run_cmd


@dataclass(frozen=True)
class SizeEvaluation:
    """Result of evaluating a PR's size.

    ``file_counts`` holds only files eligible to be marked viewed (text, not deleted, not renamed).
    ``total_lines`` also includes the churn of deleted and renamed text files, so they cannot be
    used to slip a large change under the approval threshold.
    """

    file_counts: dict[str, int] = field(default_factory=dict)
    total_lines: int = 0
    small_files: list[str] = field(default_factory=list)
    pr_is_small: bool = False


def _git_diff_z(args: list[str], base_branch: str, wdir: str, env: dict) -> list[str]:
    result = run_cmd(
        ["git", "diff", *args, "-z", f"origin/{base_branch}...HEAD"],
        cwd=wdir,
        env=env,
        timeout=TIMEOUT_GIT,
    )
    return [t for t in result.stdout.decode("utf-8", errors="replace").split("\0") if t]


def _excluded_files(base_branch: str, wdir: str, env: dict) -> set[str]:
    """Paths that are not counted files: deleted, renamed or copied."""
    excluded: set[str] = set()

    # --name-status -z: "<status>\0<path>\0" or "<R|C score>\0<old>\0<new>\0"
    tokens = _git_diff_z(["--name-status", "-M"], base_branch, wdir, env)
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if status[0] in "RC":
            excluded.update(tokens[i + 1 : i + 3])
            i += 3
        else:
            if status[0] == "D":
                excluded.add(tokens[i + 1])
            i += 2
    return excluded


def _numstat_counts(base_branch: str, wdir: str, env: dict) -> dict[str, int | None]:
    """Exact added+removed lines per path from ``git diff --numstat`` (None for binary files).

    Unlike counting ``+``/``-`` lines of a patch, this cannot mistake content such as a removed
    ``-- comment`` (``--- comment``) for a file header, so it never under-counts.
    """
    counts: dict[str, int | None] = {}
    # --numstat -z: "<add>\t<del>\t<path>\0", or for renames "<add>\t<del>\t\0<old>\0<new>\0".
    # Binary files print "-" for both counts.
    tokens = _git_diff_z(["--numstat", "-M"], base_branch, wdir, env)
    i = 0
    while i < len(tokens):
        added, deleted, path = tokens[i].split("\t", 2)
        if path == "":
            # rename/copy entry: keep its churn (keyed by new path) so it counts toward the total;
            # it is kept out of ``file_counts`` via --name-status.
            counts[tokens[i + 2]] = None if added == "-" and deleted == "-" else int(added) + int(deleted)
            i += 3
            continue
        counts[path] = None if added == "-" and deleted == "-" else int(added) + int(deleted)
        i += 1
    return counts


def evaluate_size(base_branch: str, wdir: str, env: dict, config: AutoApproveConfig) -> SizeEvaluation:
    """Evaluate the checked-out branch against ``origin/<base_branch>``.

    A file is small when its added+removed count is strictly below ``viewed_max_lines``; the
    PR is small when the sum over all text files (including deleted and renamed ones) is strictly
    below ``approve_max_lines``. Binary, deleted and renamed files are never marked small; binary
    files have no line count and contribute 0. Lockfiles and generated text files are counted.
    """
    numstat = _numstat_counts(base_branch, wdir, env)
    excluded = _excluded_files(base_branch, wdir, env)

    file_counts = {p: n for p, n in numstat.items() if n is not None and p not in excluded}

    total = sum(n for n in numstat.values() if n is not None)
    small_files = [p for p, n in file_counts.items() if n < config.viewed_max_lines]
    return SizeEvaluation(
        file_counts=file_counts,
        total_lines=total,
        small_files=small_files,
        pr_is_small=total < config.approve_max_lines,
    )
