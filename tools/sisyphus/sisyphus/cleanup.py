"""Delete an item's local Sisyphus branch after a clean finish, with strict verification.

The only module that deletes a branch. Standard library only; git is injected as a callable
(``git(args) -> result`` with ``returncode``/``stdout``/``stderr``, run in the dedicated clone), so the
orchestrator's runner (and tests' fakes) are used unchanged.

The branch may exist only locally, so nothing is deleted on trust. In order, every step must pass or the
branch is KEPT and the reason is returned for the digest:

1. the local branch exists (if not: nothing to do, silently; that makes a rerun idempotent);
2. the clone is clean (never stash, reset or clean);
3. ``origin`` has the branch (``ls-remote``) and it is fetched into ``refs/remotes/origin/<branch>``;
4. the branch head is an ancestor of ``origin/<branch>`` (``merge-base --is-ancestor``); upstream
   tracking is not consulted;
5. a detached HEAD (left by ``harness run ci-fix``) is itself on ``origin/<branch>``, so leaving it loses nothing;
6. the clone is switched to the base branch and fast-forwarded from ``origin/<base>`` (``--ff-only`` only).

Then ``git branch -d`` (never ``-D``). Plain ``-d`` refuses a branch that is not merged into ``HEAD`` (the
usual case: an open, unmerged PR), so step 4's verified remote branch is first made the branch's upstream
(``--set-upstream-to``): ``-d`` then checks it against that upstream, git's own second check. No remote
branch, PR or other ref is touched.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

GitFn = Callable[[Sequence[str]], Any]

DELETED = "deleted"
KEPT = "kept"
ABSENT = "absent"


@dataclass(frozen=True)
class CleanupOutcome:
    status: str
    message: str = ""


def _out(res: Any) -> str:
    return str(res.stdout).strip()


def _err(res: Any) -> str:
    return str(res.stderr or res.stdout).strip()[-200:]


def _kept(item: str, branch: str, why: str) -> CleanupOutcome:
    return CleanupOutcome(KEPT, f"Branch kept: {item} {branch} {why}")


def _verify_on_origin(git: GitFn, branch: str, sha: str) -> str | None:
    """``None`` when ``sha`` is verified to be on ``origin/<branch>``, else the reason to keep the branch."""
    remote_ref = f"refs/remotes/origin/{branch}"
    listing = git(["ls-remote", "--heads", "origin", branch])
    if listing.returncode != 0:
        return f"origin could not be reached to verify it: {_err(listing)}"
    if not any(line.split()[-1:] == [f"refs/heads/{branch}"] for line in _out(listing).splitlines()):
        return "not pushed (no such branch on origin)"
    fetched = git(["fetch", "origin", f"+refs/heads/{branch}:{remote_ref}"])
    if fetched.returncode != 0:
        return f"could not be fetched from origin to verify it: {_err(fetched)}"
    ancestor = git(["merge-base", "--is-ancestor", sha, remote_ref])
    if ancestor.returncode == 1:
        count = _out(git(["rev-list", "--count", f"{remote_ref}..{sha}"]))
        return f"has {count + ' commit(s)' if count.isdigit() else 'commit(s)'} not on origin/{branch}"
    if ancestor.returncode != 0:
        return f"could not be compared with origin/{branch}: {_err(ancestor)}"
    current = _out(git(["rev-parse", "--abbrev-ref", "HEAD"]))
    if current == "HEAD" and git(["merge-base", "--is-ancestor", "HEAD", remote_ref]).returncode != 0:
        return f"the clone is on a detached HEAD that is not on origin/{branch}; left alone"
    return None


def _switch_to_base(git: GitFn, base: str) -> str | None:
    """Switch to ``base`` and fast-forward it (``--ff-only``); ``None`` on success, else the reason."""
    base_ref = f"refs/remotes/origin/{base}"
    if git(["fetch", "origin", f"+refs/heads/{base}:{base_ref}"]).returncode != 0:
        return f"verified on origin, but origin/{base} could not be fetched"
    if git(["checkout", base]).returncode != 0:
        return f"verified on origin, but the clone could not be switched to {base}"
    if git(["merge", "--ff-only", base_ref]).returncode != 0:
        return f"verified on origin, but {base} cannot be fast-forwarded from origin/{base}"
    return None


def cleanup_branch(git: GitFn, *, item: str, branch: str, base: str) -> CleanupOutcome:
    """Verify, switch the clone to ``base`` and delete local ``branch``; see the module docstring."""
    if git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode != 0:
        return CleanupOutcome(ABSENT)
    dirty = git(["status", "--porcelain", "--untracked-files=all"])
    if dirty.returncode != 0 or _out(dirty):
        return _kept(item, branch, "the clone is dirty (or its status is unreadable); nothing was touched")
    head = git(["rev-parse", f"refs/heads/{branch}"])
    if head.returncode != 0 or not _out(head):
        return _kept(item, branch, "head could not be read")
    sha = _out(head)
    why = _verify_on_origin(git, branch, sha) or _switch_to_base(git, base)
    if why:
        return _kept(item, branch, why)
    if git(["branch", f"--set-upstream-to=origin/{branch}", branch]).returncode != 0:
        return _kept(item, branch, "verified on origin, but its upstream could not be set for the safe delete")
    deleted = git(["branch", "-d", branch])
    if deleted.returncode != 0:
        git(["branch", "--unset-upstream", branch])
        return _kept(item, branch, f"git branch -d refused: {_err(deleted)}")
    return CleanupOutcome(
        DELETED, f"local branch {branch} deleted (head {sha[:12]} is on origin/{branch}); clone is on {base}"
    )
