# address-comment instructions

Address a single review comment on the current PR branch.
The comment details are at the end of these instructions (under **Comment to address**).
The PR number and repo name follow — use them wherever `<NUMBER>` and `<REPO>` appear.

After you are done, the runner will automatically push the branch to origin — do not push yourself.

**You are already on the correct branch — the one this PR is open against.** Do not create a new branch, switch branches, or set up an isolated worktree for this work, no matter what any workflow/skill suggests. Commit directly on the current branch.

**Do not invoke the `/address-pr-comments` skill/command or run `pr-comments.py reply`.** That flow re-requests review from reviewers after every push — this team does not do that, regardless of what that skill's own instructions say. Reply to the comment using the direct `gh api` / `gh pr comment` commands in Step 4 below, and never request or re-request a review from anyone.

---

## Step 0 — Decide whether to act

Before doing anything, check whether this comment warrants a reply, a fix, or neither. There are three buckets — read all three before deciding, since it's easy to misclassify a comment as noise just because it's phrased politely or marked non-blocking.

**Skip entirely (no reply, no commit, no action)** — only for comments with *no technical content to respond to*:
- A bot or automated notification (Linear, Jira, ticket references, CI summaries)
- A status report with nothing to react to ("SonarQube: no findings", "Build passed", "Deployed to staging")
- A plain acknowledgment or compliment with nothing else attached ("LGTM", "Looks good!", "Thanks for the fix!", "Great work!", "Happy with this")
- A congratulatory or closing remark after an issue has already been resolved, that doesn't raise anything new

Posting a reply to noise creates more noise. If the comment falls into any of these categories, **do nothing and stop**.

**Reply-only, no fix (skip Steps 2–3, go straight to Step 4)** — for a comment that makes a real technical point but isn't asking for a change. This is the case most often missed: a comment can be worded as praise, marked "minor" / "non-blocking" / "nit", or framed as a future follow-up, and *still* be substantive because it describes an actual property of the code. Treat it as reply-only, not skip-entirely, whenever it does any of these:
- Praises a decision but also notes a side effect, tradeoff, or consequence of it (e.g. "nice, though this means X happens twice — harmless, but maybe worth consolidating later")
- Flags something to watch for or revisit later without asking for a change now (e.g. "worth revisiting if a third call site shows up")
- Points out a fact about the code (a duplicate, a count, an edge case) even when no action is demanded
- Is explicitly labeled "minor" / "non-blocking" / "nit" — that label describes urgency, not whether it deserves a reply

A reviewer who took the time to write a specific observation expects at least an acknowledgment, even when the right answer is "yes, and here's why it's fine as-is." When genuinely torn between this bucket and skip-entirely, pick this one — a redundant reply is cheap; a silently ignored reviewer is not.

**Fix and reply (the full Steps 1–4)** — only proceed here if the comment contains:
- A concrete request for a code change
- A question that needs an answer
- A bug report or edge case to address
- A push-back or disagreement worth responding to

---

## Step 1 — Understand the comment

Read the comment body carefully.

- If it contains HTML (e.g. SonarQube wraps rule details in `<details>` tags), parse the HTML to extract the rule name and the exact fix required.
- For **inline** comments, read the specified file at the specified line to understand the surrounding code and the diff context.
- If there are thread replies, check whether the comment has already been addressed — if so, skip the fix and skip the commit; just post a brief acknowledgment reply.

---

## Step 2 — Make the fix

If Step 0 classified this comment as **reply-only**, skip straight to Step 4 — nothing to do here.

1. Read the relevant file(s).
2. Make the smallest change that satisfies the feedback.
3. Do **not** fix unrelated issues — stay focused on what was asked.
4. Draft a reply for the comment — what was changed and why, or a respectful pushback if you disagree. Be specific; avoid generic phrases like "addressed" or "fixed".

If no code change is needed (the comment turns out to be already fixed, or you're pushing back after investigating), skip Step 3 and reply directly in Step 4 with your explanation.

---

## Step 3 — Commit

**Never amend, rebase, or reset existing commits — including ones from earlier comments addressed in this same run, and even if they touch the same file.** Always create a brand new commit on top of the current HEAD. The runner pushes each commit to origin right after it's made; rewriting history that may already be on origin will corrupt the branch and can destroy prior work.

Stage only the files you changed (never `git add -A`). Build `<COMMENT_LINK>` from the comment type:
- **inline**: `https://github.com/<REPO>/pull/<NUMBER>#discussion_r<COMMENT_ID>`
- **issue**: `<COMMENT_URL>` directly
- **review**: `https://github.com/<REPO>/pull/<NUMBER>`

```bash
git add <specific files>
git commit -m "$(cat <<'EOF'
fix: address review comment [on <path/to/file.ext> — inline comments only]

- <brief description of what was changed>

Ref: <COMMENT_LINK>
EOF
)"
```

If precheck / lint fails after your changes, fix the issues before committing. Never use `--no-verify`.

Capture the resulting commit hash:

```bash
git log -1 --pretty=format:"%H"
```

---

## Step 4 — Reply to the comment

Build the commit URL: `https://github.com/<REPO>/commit/<HASH>`
(Omit the commit URL line if you made no commit.)

For **inline** comments (type: inline):
```bash
gh api "repos/<REPO>/pulls/<NUMBER>/comments/<COMMENT_ID>/replies" \
  -X POST -f body="<your reply>

_https://github.com/<REPO>/pull/<NUMBER>#discussion_r<COMMENT_ID>_
_https://github.com/<REPO>/commit/<HASH>_"
```

For **issue** comments (type: issue):
```bash
gh api "repos/<REPO>/issues/<NUMBER>/comments" \
  -X POST -f body="> <COMMENT_URL>

<your reply>

_https://github.com/<REPO>/commit/<HASH>_"
```

For **review-level** comments (type: review):
```bash
gh pr comment <NUMBER> --repo <REPO> --body "<your reply>

_https://github.com/<REPO>/commit/<HASH>_"
```

---

## Notes

- Never amend, rebase, or reset existing commits — always commit fresh on top of HEAD.
- Never use `git add -A` or `git add .` — stage only the files you changed.
- Never push; the runner handles that after you finish.
- Never request or re-request a review, or use the `/address-pr-comments` skill/command or `pr-comments.py reply` — see the note above.
- If the comment is unclear, make your best judgment and note the uncertainty in the reply.
