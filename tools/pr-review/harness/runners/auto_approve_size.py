"""Pure size evaluation for the auto-approve command.

Decides, from ``git diff origin/<base>...HEAD`` on a checked-out PR branch, which files
are small enough to mark viewed and whether the whole PR is small enough to approve.
Read-only: no network and no AI/backend call.
"""

from dataclasses import dataclass, field

from harness.config import AutoApproveConfig
from harness.runners.common import TIMEOUT_GIT, count_diff_lines, get_file_diff, run_cmd


@dataclass(frozen=True)
class SizeEvaluation:
    """Result of evaluating a PR's size.

    ``file_counts`` holds only counted files (text, not deleted, not renamed).
    """

    file_counts: dict[str, int] = field(default_factory=dict)
    total_lines: int = 0
    small_files: list[str] = field(default_factory=list)
    pr_is_small: bool = False


def _no_color_env(env: dict) -> dict:
    """Force uncolored git output so a user's ``color.ui = always`` cannot hide +/- markers
    behind ANSI escapes and make a diff under-count (which would wrongly approve)."""
    return {
        **env,
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "color.ui",
        "GIT_CONFIG_VALUE_0": "never",
        "GIT_CONFIG_KEY_1": "color.diff",
        "GIT_CONFIG_VALUE_1": "never",
    }


def _git_diff_z(args: list[str], base_branch: str, wdir: str, env: dict) -> list[str]:
    result = run_cmd(
        ["git", "diff", *args, "-z", f"origin/{base_branch}...HEAD"],
        cwd=wdir,
        env=env,
        timeout=TIMEOUT_GIT,
    )
    return [t for t in result.stdout.decode("utf-8", errors="replace").split("\0") if t]


def _excluded_files(base_branch: str, wdir: str, env: dict) -> set[str]:
    """Paths that are not counted files: binary, deleted, renamed or copied."""
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

    # --numstat -z: "<add>\t<del>\t<path>\0", or for renames "<add>\t<del>\t\0<old>\0<new>\0".
    # Binary files print "-" for both counts.
    tokens = _git_diff_z(["--numstat", "-M"], base_branch, wdir, env)
    i = 0
    while i < len(tokens):
        added, deleted, path = tokens[i].split("\t", 2)
        if path == "":
            i += 3  # rename/copy entry; already excluded above
            continue
        if added == "-" and deleted == "-":
            excluded.add(path)
        i += 1
    return excluded


def evaluate_size(base_branch: str, wdir: str, env: dict, config: AutoApproveConfig) -> SizeEvaluation:
    """Evaluate the checked-out branch against ``origin/<base_branch>``.

    A file is small when its added+removed count is strictly below ``viewed_max_lines``; the
    PR is small when the sum over counted files is strictly below ``approve_max_lines``.
    Binary, deleted and renamed files (even renamed with edits) contribute 0 and are never
    small. Lockfiles and generated text files are counted.
    """
    env = _no_color_env(env)
    name_tokens = _git_diff_z(["--name-only"], base_branch, wdir, env)
    excluded = _excluded_files(base_branch, wdir, env)

    file_counts: dict[str, int] = {}
    for path in name_tokens:
        if path in excluded:
            continue
        diff = get_file_diff(path, base_branch, wdir, env)
        file_counts[path] = count_diff_lines(diff)

    total = sum(file_counts.values())
    small_files = [p for p, n in file_counts.items() if n < config.viewed_max_lines]
    return SizeEvaluation(
        file_counts=file_counts,
        total_lines=total,
        small_files=small_files,
        pr_is_small=total < config.approve_max_lines,
    )
