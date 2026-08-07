# focused-review instructions

You are elaborating a single SonarQube-posted review comment that cites a knowledge
file from the `jpablo/vibe-types` catalog. The flagged comment and its full knowledge
file content are appended below (under **Flagged comment** and **Knowledge file**).
The PR number and repo name follow at the end — use them wherever `<NUMBER>` and
`<REPO>` appear. A literal marker string is also given at the end (**Marker**) — your
posted reply MUST include it verbatim, or future runs will treat this comment as
unhandled and repost.

**Do not edit any file, run `git add`, commit, or push.** This step only posts a
comment — it never changes code.

---

## Step 1 — Understand the finding

Read the SonarQube comment body and diff context below. Then read the flagged file at
the given path/line directly from the working directory (already checked out at the
PR's head) to see the surrounding code in full — the diff hunk alone is usually too
narrow.

## Step 2 — Read the knowledge file

The full content of the cited knowledge-base file is included below under
**Knowledge file**. It documents the general principle the SonarQube rule is checking
for — what it is, when to use it, common antipatterns, and worked examples.

## Step 3 — Write the refactor description

Using the knowledge file's guidance, write a concrete, specific description of the
refactor to apply to *this* flagged code — not a restatement of the knowledge file.
Include:

- A one-sentence summary of what's wrong with the current code, in this specific case.
- The concrete refactor: what to change, named identifiers from the actual code (not
  placeholders), and a short before/after code snippet if it clarifies the change.
- Why this shape is better, in one or two sentences — tie it back to the specific
  antipattern or principle from the knowledge file, don't just repeat the file's prose.

Keep it the length of a normal, focused PR review comment — a few short paragraphs and
at most one code snippet. Do not paste large sections of the knowledge file verbatim.

## Step 4 — Post the reply

```bash
gh api "repos/<REPO>/pulls/<NUMBER>/comments/<COMMENT_ID>/replies" \
  -X POST -f body="<your refactor description>

<Marker>"
```

Replace `<COMMENT_ID>` with the **Comment ID** given below, and `<Marker>` with the
exact marker string given below, on its own line at the end of the body.

---

## Notes

- If, after reading the full file, the flagged code has already been refactored or the
  finding no longer applies, post a brief reply saying so instead of a refactor
  description — still include the marker.
- Never request or re-request a review.
- Never push or commit — the runner does not expect any git changes from this step.
