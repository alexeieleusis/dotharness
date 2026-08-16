"""Defense against a backend/git operation landing in the wrong repo.

Added after an incident where `harness run --config <other-repo>.toml`,
invoked from `~/.harness/tools/pr-review`, ended up fetching a branch and
committing into `~/.harness` (dotharness's own repo) instead of the repo the
config pointed at. Root cause wasn't fully pinned down (see the incident
writeup), but nothing in this codebase's own git/subprocess calls explains
it — every one of them already uses `cwd=`/`-C` derived from
`config.repo.working_dir`. The leading hypothesis is a backend (opencode)
process that outlived its intended invocation and kept running with a stale
cwd. Either way, `assert_repo_identity` is the belt-and-suspenders backstop: if
the directory a commit/push is about to happen in isn't actually the
configured repo, abort loudly before anything happens, rather than silently
committing into whatever repo happens to be sitting there.

That check only inspects the directory the backend was *told* to operate in,
which in the incident above was never the directory actually mutated — the
rogue instance landed in the harness's own repo regardless of `--dir`/`cwd`.
`assert_repo_unchanged` (with `discover_repo_root`/`head_sha`) covers that
other side: snapshot the harness's own repo before a run and confirm its HEAD
hasn't moved afterward, since a run targeting some other repo has no
legitimate reason to commit into this one.
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

_GIT_TIMEOUT_SECONDS = 30


class RepoIdentityError(RuntimeError):
    """`working_dir` is not the git repo `repo.name` says it should be."""


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        list(args),
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _run_git(working_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return _run("git", "-C", str(working_dir), *args)


def assert_repo_identity(working_dir: Path, expected_repo_name: str) -> None:
    """Raise RepoIdentityError unless `working_dir` is the toplevel of a git
    worktree whose `origin` remote resolves to `expected_repo_name` (an
    'owner/repo' slug, as configured in `repo.name`)."""
    working_dir = working_dir.resolve()

    toplevel = _run_git(working_dir, "rev-parse", "--show-toplevel")
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

    origin = _run_git(working_dir, "remote", "get-url", "origin")
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


def discover_repo_root(start: Path) -> Path | None:
    """Best-effort toplevel of the git repo containing `start`, or None if `start`
    isn't inside one (e.g. an unusual install layout). Used to locate this harness's
    own repo checkout so it can be watched for the rogue-backend-writes-elsewhere
    scenario described in this module's docstring: `expected_repo_name` covers the
    repo a backend run is *supposed* to touch, but says nothing about a different
    directory — such as the harness's own repo — that a runaway backend process
    might write into instead."""
    result = _run_git(start, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def head_sha(working_dir: Path) -> str:
    """Current HEAD commit sha for `working_dir`'s repo."""
    result = _run_git(working_dir, "rev-parse", "HEAD")
    if result.returncode != 0:
        raise RepoIdentityError(f"could not read HEAD for {working_dir}: {result.stderr.strip()}")  # noqa: TRY003
    return result.stdout.strip()


def assert_repo_unchanged(working_dir: Path, expected_head: str) -> None:
    """Raise RepoIdentityError if `working_dir`'s HEAD has moved since `expected_head`
    was captured. A backend run that isn't supposed to touch `working_dir` at all
    (e.g. this harness's own repo, while it's meant to be operating on some other
    configured repo) has no legitimate reason to move its HEAD."""
    current = head_sha(working_dir)
    if current != expected_head:
        raise RepoIdentityError(  # noqa: TRY003
            f"{working_dir}'s HEAD moved from {expected_head} to {current} during a backend run that "
            "wasn't supposed to touch it — looks like the rogue-backend-writes-elsewhere scenario this "
            "guard exists to catch"
        )


def _split_host_and_path(url: str) -> tuple[str, str] | None:
    """Split a git remote URL into (host-or-alias, 'owner/repo' path), or
    None if it doesn't look like an scp-style/ssh/https git URL."""
    normalized = url.strip().removesuffix(".git").rstrip("/")

    if normalized.startswith(("https://", "ssh://")):
        normalized = normalized.split("://", 1)[1]
        authority, sep, path = normalized.partition("/")
    else:
        # scp-style: [user@]host:path
        authority, sep, path = normalized.partition(":")

    if not sep or not path:
        return None
    return authority.rpartition("@")[2] or authority, path


@functools.cache
def _resolve_ssh_host(host: str) -> str:
    """Resolve an SSH config host alias (e.g. 'github-personal', mapped via
    `Host github-personal` / `HostName github.com`) to its real HostName.
    Falls back to the alias itself if `ssh -G` is unavailable or fails, which
    keeps this check fail-closed rather than silently permissive. Cached
    since the SSH config backing a given alias doesn't change mid-run, and
    this is checked on every backend invocation."""
    if host.lower() == "github.com":
        return "github.com"  # common case: skip the subprocess
    try:
        result = _run("ssh", "-G", host)
    except (OSError, subprocess.SubprocessError):
        return host
    if result.returncode != 0:
        return host
    for line in result.stdout.splitlines():
        if line.startswith("hostname "):
            return line.split(maxsplit=1)[1].strip()
    return host


def _origin_matches_repo(url: str, repo_name: str) -> bool:
    """`repo_name` is an 'owner/repo' slug; `url` is origin's fetch URL in
    whatever form git reports it (SSH, HTTPS, with or without '.git'),
    possibly through an SSH config host alias like `github-personal`."""
    split = _split_host_and_path(url)
    if split is None:
        return False
    host, path = split
    return _resolve_ssh_host(host).lower() == "github.com" and path == repo_name
