# address-comments instructions

Address all open review comments on the current PR branch using the steps below.
The PR number and repo name are appended at the end of these instructions — use them wherever `<NUMBER>` and `<REPO>` appear.

After you are done, the runner will automatically push the branch to origin — do not push yourself.

---

## Step 1 — Fetch comments

```bash
python {script_path} fetch --pr <NUMBER>
```

Parse the printed output carefully. Each inline comment includes:

- A numeric **id** (needed for the reply step)
- A **file path and line** to navigate to
- The **comment body** describing what needs to change

Read the saved JSON for full `diff_hunk` context if needed:

```
~/.harness/cache/pr-<NUMBER>-comments.json
```

(The cache dir `~/.harness/cache/` is always on the local machine where the script runs.)

If `fetch` finds **no comments**, stop — nothing to do.

---

## Step 2 — Address each comment

For every inline and review-level comment:

1. Read the relevant file(s).
2. Make the smallest change that satisfies the feedback.
3. Do **not** fix unrelated issues — stay focused on what was asked.
4. Write a tailored reply for each comment — what was changed, why, or a respectful pushback if you disagree. Be specific; avoid generic phrases like "addressed" or "fixed".

---

## Step 3 — Save per-comment replies

Write `~/.harness/cache/pr-<NUMBER>-replies.json` mapping each comment ID to its reply text:

```json
{
  "123456": "Extracted the retry logic into a dedicated `retry_with_backoff` helper so it can be reused by the other callers in the same module.",
  "123457": "Kept the explicit `None` check here rather than a truthiness test — the value can legitimately be `0` or an empty list, so `is None` is the right guard."
}
```

Keys are the numeric comment `id` (as a string) for inline comments, or `review-<id>` for review-level comments.

---

## Step 4 — Atomic commit

Commit **all** addressed changes in a single commit:

```bash
git add <specific files — never -A blindly>
git commit -m "$(cat <<'EOF'
fix: address review comments

- <brief summary per comment>

EOF
)"
```

If precheck / lint fails after your changes, fix the issues before committing. Never use `--no-verify`.

Capture the resulting commit hash:

```bash
git log -1 --pretty=format:"%H"
```

---

## Step 5 — Reply to reviewers and request re-review

```bash
python {script_path} reply --commit <HASH> --pr <NUMBER>
```

The script will post your saved replies to each comment thread (with the commit URL appended) and re-request review from all current reviewers unless the PR is already approved.

---

## Notes

- Never use `git add -A` or `git add .` — stage only the files you changed.
- Never push; the runner handles that after you finish.
- If a comment is unclear, make your best judgment and note the uncertainty in the reply.
