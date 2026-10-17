"""Delete an item's local Sisyphus branch after a clean finish, with strict verification.

The only module that deletes a branch. Standard library only; git is injected as a callable
(``git(args) -> result`` with ``returncode``/``stdout``/``stderr``, run in the dedicated clone) and so is the
pull-request lookup (``pr_lookup(ref) -> PrInfo | None``, raising on a gh failure), so the orchestrator's
runners (and tests' fakes) are used unchanged.

Nothing is deleted on trust. Preconditions, each of which keeps the branch and returns a reason for the digest:
the local branch exists (if not: nothing to do, silently; that makes a rerun idempotent); the clone is clean
(never stash, reset or clean). Then ONE of three proofs, checked in this order, cheapest first (a later proof is
not evaluated once an earlier one holds, so ``gh`` is only asked when git alone cannot prove it):

1. the branch head is an ancestor of ``origin/<branch>`` (``ls-remote`` + fetch + ``merge-base --is-ancestor``);
2. the branch head is an ancestor of ``origin/<base>`` (base fetched first; a merge-commit or rebase merge, also
   once the remote branch is gone);
3. ``gh pr view`` reports the PR MERGED with ``headRefOid`` equal to the local head (squash merges).

A detached HEAD (left by ``harness run ci-fix``) must itself be on the branch, ``origin/<branch>`` or
``origin/<base>`` so leaving it loses nothing. The clone is then switched to the base branch and fast-forwarded
from ``origin/<base>`` (``--ff-only`` only) and ``git branch -d`` is run (never ``-D``/``--force``): after proof
(1) the verified remote branch is first made the upstream (``--set-upstream-to``) because an unmerged PR branch
would otherwise be refused; after proof (2) the head is an ancestor of the checked-out base, which ``-d``
accepts as is. Proof (3) cannot satisfy ``-d`` (a squash-merged head is in neither HEAD nor an upstream), so
:func:`_delete_squash_merged` runs ``git branch -D`` there and ONLY there (proofs 1 and 2 never use ``-D``;
``--force`` is never used). No recovery ref is made: the reflog is the last resort, and the digest carries the
full head SHA, the PR number and ``git fetch origin pull/<N>/head``. No remote branch, PR or other ref is touched.
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
class PrInfo:
    """What ``gh pr view --json number,state,headRefOid`` says."""

    number: int | None
    state: str
    head_sha: str


PrLookup = Callable[[str], PrInfo | None]


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


def _prove_on_origin_branch(git: GitFn, branch: str, sha: str) -> str | None:
    """Proof (1). ``None`` when ``sha`` is on ``origin/<branch>``, else the reason it is not proven."""
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
    return None


def _fetch_base(git: GitFn, base: str) -> str | None:
    if git(["fetch", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"]).returncode != 0:
        return f"origin/{base} could not be fetched"
    return None


def _prove_on_base(git: GitFn, base: str, sha: str) -> str | None:
    """Proof (2). ``None`` when ``sha`` is an ancestor of the freshly fetched ``origin/<base>``."""
    why = _fetch_base(git, base)
    if why:
        return why
    ancestor = git(["merge-base", "--is-ancestor", sha, f"refs/remotes/origin/{base}"])
    if ancestor.returncode == 0:
        return None
    if ancestor.returncode == 1:
        return f"head is not an ancestor of origin/{base}"
    return f"could not be compared with origin/{base}: {_err(ancestor)}"


def _prove_pr_merged(pr_lookup: PrLookup | None, ref: str, sha: str) -> tuple[str | None, str]:
    """Proof (3). ``(None, detail)`` when the PR is MERGED with ``headRefOid == sha``, else ``(reason, "")``."""
    if pr_lookup is None:
        return "no GitHub client available", ""
    try:
        info = pr_lookup(ref)
    except Exception as exc:  # a gh failure must keep the branch, never crash the run
        return f"gh lookup of {ref} failed: {str(exc)[-200:]}", ""
    if info is None:
        return f"no PR found for {ref}", ""
    label = f"PR #{info.number}" if info.number is not None else f"the PR of {ref}"
    if info.state != "MERGED":
        return f"{label} is {info.state or 'in an unknown state'}, not MERGED", ""
    if info.head_sha != sha:
        return f"{label} is MERGED but its head {info.head_sha[:12]} differs from the local head {sha[:12]}", ""
    number: int | str | None = info.number
    if number is None and ref.isdigit():
        number = ref
    recover = f"; recover with: git fetch origin pull/{number}/head" if number is not None else ""
    return None, f"{label} is MERGED with head {sha}{recover}"


def _detached_head_unsafe(git: GitFn, branch: str, base: str, sha: str) -> str | None:
    """A detached HEAD must already be on the branch or origin, so leaving it loses nothing."""
    if _out(git(["rev-parse", "--abbrev-ref", "HEAD"])) != "HEAD":
        return None
    for ref in (sha, f"refs/remotes/origin/{branch}", f"refs/remotes/origin/{base}"):
        if git(["merge-base", "--is-ancestor", "HEAD", ref]).returncode == 0:
            return None
    return f"the clone is on a detached HEAD that is not on {branch} or origin; left alone"


def _switch_to_base(git: GitFn, base: str, *, fetch: bool) -> str | None:
    """(Fetch,) switch to ``base`` and fast-forward it (``--ff-only``); ``None`` on success, else the reason."""
    if fetch and _fetch_base(git, base):
        return f"proven, but origin/{base} could not be fetched"
    if git(["checkout", base]).returncode != 0:
        return f"proven, but the clone could not be switched to {base}"
    if git(["merge", "--ff-only", f"refs/remotes/origin/{base}"]).returncode != 0:
        return f"proven, but {base} cannot be fast-forwarded from origin/{base}"
    return None


def _delete_squash_merged(git: GitFn, item: str, branch: str, detail: str, base: str) -> CleanupOutcome:
    """Proof (3) holds (PR MERGED, ``headRefOid`` == local head) and the clone is on base and clean: ``-D``.

    A squash-merged head is neither reachable from the checked-out base nor from any upstream, so plain ``-d``
    refuses; the proof is what makes ``-D`` safe. This is the ONLY place ``-D`` is issued. No recovery ref and no
    helper refs: the reflog is the last-resort recovery, and ``detail`` (full head SHA, PR number and the
    ``git fetch origin pull/<N>/head`` hint) goes to the digest so the commit can be fetched from GitHub.
    """
    deleted = git(["branch", "-D", branch])
    if deleted.returncode == 0:
        return CleanupOutcome(DELETED, f"local branch {branch} deleted (proof 3: {detail}); clone is on {base}")
    return _kept(item, branch, f"proof (3) holds ({detail}) but git branch -D refused: {_err(deleted)}")


def _first_proof(
    git: GitFn, branch: str, base: str, sha: str, pr_lookup: PrLookup | None, pr_ref: str
) -> tuple[int, str] | str:
    """The first proof that holds, in order, as ``(number, detail)``; else the "none held" reason."""
    failure = _prove_on_origin_branch(git, branch, sha)
    if failure is None:
        return 1, f"head {sha[:12]} is an ancestor of origin/{branch}"
    failures = [f"(1) {failure}"]
    failure = _prove_on_base(git, base, sha)
    if failure is None:
        return 2, f"head {sha[:12]} is an ancestor of origin/{base}"
    failures.append(f"(2) {failure}")
    failure, detail = _prove_pr_merged(pr_lookup, pr_ref, sha)
    if failure is None:
        return 3, detail
    failures.append(f"(3) {failure}")
    return "no deletion proof held: " + "; ".join(failures)


def cleanup_branch(
    git: GitFn,
    *,
    item: str,
    branch: str,
    base: str,
    pr_lookup: PrLookup | None = None,
    pr_ref: str | None = None,
) -> CleanupOutcome:
    """Prove, switch the clone to ``base`` and delete local ``branch``; see the module docstring.

    ``pr_ref`` is the PR number (as text) from the run record when stored, else the lookup is by ``branch``.
    """
    if git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode != 0:
        return CleanupOutcome(ABSENT)
    dirty = git(["status", "--porcelain", "--untracked-files=all"])
    if dirty.returncode != 0 or _out(dirty):
        return _kept(item, branch, "the clone is dirty (or its status is unreadable); nothing was touched")
    head = git(["rev-parse", f"refs/heads/{branch}"])
    if head.returncode != 0 or not _out(head):
        return _kept(item, branch, "head could not be read")
    sha = _out(head)

    proven = _first_proof(git, branch, base, sha, pr_lookup, pr_ref or branch)
    if isinstance(proven, str):
        return _kept(item, branch, proven)
    proof, detail = proven

    why = _detached_head_unsafe(git, branch, base, sha) or _switch_to_base(git, base, fetch=proof != 2)
    if why:
        return _kept(item, branch, why)
    if proof == 3:
        return _delete_squash_merged(git, item, branch, detail, base)
    if proof == 1 and git(["branch", f"--set-upstream-to=origin/{branch}", branch]).returncode != 0:
        return _kept(item, branch, "proven, but its upstream could not be set for the safe delete")
    deleted = git(["branch", "-d", branch])
    if deleted.returncode != 0:
        if proof == 1:
            git(["branch", "--unset-upstream", branch])
        return _kept(item, branch, f"git branch -d refused: {_err(deleted)}")
    return CleanupOutcome(DELETED, f"local branch {branch} deleted (proof {proof}: {detail}); clone is on {base}")
