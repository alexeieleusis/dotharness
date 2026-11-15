# local-address-finding instructions

Address a single review finding on the current branch.
The finding details are at the end of these instructions (under **Finding to address**).

The finding comes from an automated reviewer and **may be wrong**. Verify it against the code before changing anything.

This is a local, offline workflow. Do not use `gh`, any GitHub or reply API, `git push`, or request a review from anyone. The runner handles everything after you finish.

**You are already on the correct branch.** Do not create a new branch, switch branches, or set up a worktree. Commit directly on the current branch.

---

## Inputs

At the end of these instructions you are given:

- the finding block (severity, file, line, id, and the description),
- the file and line it refers to,
- the current diff of that file against the merge base,
- static-analysis (vibe-heal) context, if any,
- the absolute path of the resolution file to write: `resolutions/<finding id>.md`.

---

## Step 0 - Decide whether to act

Decide between two outcomes:

- **Fix**: the finding describes a real problem in the current code and a change is warranted.
- **Decline**: the finding is wrong, not applicable, a matter of taste with no technical content to act on, or already addressed (for example "already addressed in `<commit>`"). Check the file: if the described change is not actually there, it is not addressed.

When declining, make no commit, skip Steps 2-3 and go straight to Step 4.

---

## Step 1 - Understand the finding

- Read the finding block carefully, including the reproducing input or state it describes.
- Read the file at the given line and the surrounding code. Use the diff to see what this branch changed.
- Check whether the described problem is real by following the code, not by trusting the finding. The line number may be off.

---

## Step 2 - Make the fix

1. Read the relevant file(s).
2. Make the smallest change that resolves the finding.
3. Do **not** fix unrelated issues. Stay focused on this finding.

**Never claim a fix unless you edited the file(s) and completed Step 3 with a real commit.**

---

## Step 3 - Commit

**Never amend, rebase, or reset existing commits.** Always create a brand new commit on top of the current HEAD.

Stage only the files you changed (never `git add -A` or `git add .`).

```bash
git add <specific files>
git commit -m "$(cat <<'EOF'
fix: address review finding <id> [on <path/to/file.ext>]

- <brief description of what was changed>

Ref: local-review finding <id> (reviewed at <head_sha of the review, 12 chars>)
EOF
)"
```

Use the finding id and the 12-character review head sha given at the end of these instructions. If pre-commit or lint fails after your changes, fix the issues before committing. Never use `--no-verify`.

---

## Step 4 - Write the resolution

Write the resolution file at the absolute path given in the prompt (`resolutions/<finding id>.md`). Create the directory if needed. The first line must be exactly one of:

```
decision: fixed
decision: declined
```

The rest is a specific explanation: what you changed and why, or why you declined (including "already addressed in <commit>"). Avoid generic phrases like "addressed" or "fixed".

You must not write `decision: fixed` unless you made a commit for this finding.

---

## Notes

- Never amend, rebase, or reset existing commits. Always commit fresh on top of HEAD.
- Never use `git add -A` or `git add .`. Stage only the files you changed.
- Never use `--no-verify`.
- Never push, and never use `gh` or any GitHub API.
- If the finding is unclear, make your best judgment and note the uncertainty in the resolution.
