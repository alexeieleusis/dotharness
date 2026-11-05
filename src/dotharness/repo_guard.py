"""Repo-identity and harness-repo guards for backend runs.

``assert_repo_identity`` checks that a directory is the git toplevel of the expected GitHub repo;
``snapshot_harness_repo`` / ``assert_harness_unchanged`` check this checkout's HEAD did not move during a run.
Independent of any launcher, so other callers can reuse them.
"""

import logging
from pathlib import Path

from dotharness.subprocess_runner import RunResult, run

logger = logging.getLogger(__name__)

_HELPER_TIMEOUT_SECONDS = 30.0


class RepoIdentityError(RuntimeError):
    """``cwd`` is not the git repo ``expected_repo_name`` says it is, or the harness repo's HEAD moved."""


async def _git(working_dir: Path, *args: str) -> RunResult:
    return await run(["git", "-C", str(working_dir), *args], timeout=_HELPER_TIMEOUT_SECONDS)


_ssh_hosts: dict[str, str] = {}


async def _resolve_ssh_host(host: str) -> str:
    """Resolve an SSH config host alias to its real HostName; fall back to the alias (fail closed)."""
    if host.lower() == "github.com":
        return "github.com"
    if host in _ssh_hosts:
        return _ssh_hosts[host]
    if host.startswith("-"):
        # An untrusted remote host must never be parsed by ssh as an option; fail closed.
        return host
    resolved = host
    result = await run(["ssh", "-G", "--", host], timeout=_HELPER_TIMEOUT_SECONDS)
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("hostname "):
                resolved = line.split(maxsplit=1)[1].strip()
                break
    _ssh_hosts[host] = resolved
    return resolved


def _split_host_and_path(url: str) -> tuple[str, str] | None:
    normalized = url.strip().removesuffix(".git").rstrip("/")
    if normalized.startswith(("https://", "ssh://")):
        authority, sep, path = normalized.split("://", 1)[1].partition("/")
    else:
        authority, sep, path = normalized.partition(":")  # scp-style [user@]host:path
    if not sep or not path:
        return None
    return authority.rpartition("@")[2] or authority, path


async def _origin_matches_repo(url: str, repo_name: str) -> bool:
    split = _split_host_and_path(url)
    if split is None:
        return False
    host, path = split
    return (await _resolve_ssh_host(host)).lower() == "github.com" and path == repo_name


async def assert_repo_identity(working_dir: str | Path, expected_repo_name: str) -> None:
    """Raise ``RepoIdentityError`` unless ``working_dir`` is a git toplevel whose ``origin`` is ``expected_repo_name``.

    ``expected_repo_name`` is an ``owner/repo`` GitHub slug.
    """
    directory = Path(working_dir).resolve()
    toplevel = await _git(directory, "rev-parse", "--show-toplevel")
    if toplevel.returncode != 0:
        msg = f"{directory} is not inside a git repository ({toplevel.stderr.strip()})"
        raise RepoIdentityError(msg)
    actual = Path(toplevel.stdout.strip()).resolve()
    if actual != directory:
        msg = f"{directory} is not itself the toplevel of its git repo (toplevel is {actual})"
        raise RepoIdentityError(msg)
    origin = await _git(directory, "remote", "get-url", "origin")
    if origin.returncode != 0:
        msg = f"{directory} has no 'origin' remote ({origin.stderr.strip()})"
        raise RepoIdentityError(msg)
    if not await _origin_matches_repo(origin.stdout.strip(), expected_repo_name):
        msg = f"{directory}'s origin does not match the expected repo {expected_repo_name!r}"
        raise RepoIdentityError(msg)


async def _head_sha(working_dir: Path) -> str:
    result = await _git(working_dir, "rev-parse", "HEAD")
    if result.returncode != 0:
        msg = f"could not read HEAD for {working_dir}: {result.stderr.strip()}"
        raise RepoIdentityError(msg)
    return result.stdout.strip()


async def _discover_repo_root(start: Path) -> Path | None:
    result = await _git(start, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


async def snapshot_harness_repo(cwd: Path, root: Path | None) -> tuple[Path, str] | None:
    """(root, HEAD) to watch, or None when there is nothing to watch (no repo found, or ``cwd`` is that repo)."""
    if root is None:
        root = await _discover_repo_root(Path(__file__).resolve().parent)
    if root is None or cwd.resolve() == root.resolve():
        return None
    return root, await _head_sha(root)


async def assert_harness_unchanged(snapshot: tuple[Path, str] | None) -> None:
    if snapshot is None:
        return
    root, head = snapshot
    current = await _head_sha(root)
    if current != head:
        msg = f"{root}'s HEAD moved from {head} to {current} during a backend run that was not supposed to touch it"
        raise RepoIdentityError(msg)
