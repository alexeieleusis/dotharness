"""Defense against a backend/git operation landing in the wrong repo.

Added after an incident where `harness run --config <other-repo>.toml`,
invoked from `~/.harness/tools/pr-review`, ended up fetching a branch and
committing into `~/.harness` (dotharness's own repo) instead of the repo the
config pointed at. Root cause wasn't fully pinned down (see the incident
writeup), but nothing in this codebase's own git/subprocess calls explains
it — every one of them already uses `cwd=`/`-C` derived from
`config.repo.working_dir`. The leading hypothesis is a backend (opencode)
process that outlived its intended invocation and kept running with a stale
cwd. Either way, this check is the belt-and-suspenders backstop: if the
directory a commit/push is about to happen in isn't actually the configured
repo, abort loudly before anything happens, rather than silently committing
into whatever repo happens to be sitting there.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_GITHUB_URL_PREFIXES = (
    "git@github.com:",
    "https://github.com/",
    "ssh://git@github.com/",
)


class RepoIdentityError(RuntimeError):
    """`working_dir` is not the git repo `repo.name` says it should be."""


def assert_repo_identity(working_dir: Path, expected_repo_name: str) -> None:
    """Raise RepoIdentityError unless `working_dir` is the toplevel of a git
    worktree whose `origin` remote resolves to `expected_repo_name` (an
    'owner/repo' slug, as configured in `repo.name`)."""
    working_dir = working_dir.resolve()

    toplevel = subprocess.run(  # noqa: S603
        ["git", "-C", str(working_dir), "rev-parse", "--show-toplevel"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if toplevel.returncode != 0:
        raise RepoIdentityError(  # noqa: TRY003
            f"{working_dir} is not inside a git repository "
            f"(git rev-parse --show-toplevel failed: {toplevel.stderr.strip()})"
        )
    actual_toplevel = Path(toplevel.stdout.strip()).resolve()
    if actual_toplevel != working_dir:
        raise RepoIdentityError(  # noqa: TRY003
            f"repo.working_dir {working_dir} is not itself the toplevel of its git "
            f"repo (toplevel is {actual_toplevel}) — refusing to operate on what "
            "looks like a subdirectory of some other checkout"
        )

    origin = subprocess.run(  # noqa: S603
        ["git", "-C", str(working_dir), "remote", "get-url", "origin"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if origin.returncode != 0:
        raise RepoIdentityError(  # noqa: TRY003
            f"{working_dir} has no 'origin' remote (git remote get-url origin failed: {origin.stderr.strip()})"
        )
    origin_url = origin.stdout.strip()
    if not _origin_matches_repo(origin_url, expected_repo_name):
        raise RepoIdentityError(  # noqa: TRY003
            f"{working_dir}'s origin ({origin_url!r}) does not match the configured "
            f"repo.name {expected_repo_name!r} — refusing to run a backend/git "
            "operation against what looks like the wrong repo"
        )


def _origin_matches_repo(url: str, repo_name: str) -> bool:
    """`repo_name` is an 'owner/repo' slug; `url` is origin's fetch URL in
    whatever form git reports it (SSH, HTTPS, with or without '.git')."""
    normalized = url.strip()
    for prefix in _GITHUB_URL_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    normalized = normalized.removesuffix(".git").rstrip("/")
    return normalized == repo_name
