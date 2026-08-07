# Review Summary Instructions

You just completed a per-file code review of a pull request. Post the final summary.

Use:
    gh pr comment {PR_NUMBER} --repo {REPO} --body $'# [bot]Review Summary\n...'

The summary should be concise (3-6 sentences). Cover:
- A 1-2 sentence executive summary of what the PR does.
- Whether any P0/P1 findings were posted (or "No blocking issues found").
- Any cross-cutting concern that spans multiple files (optional).

Keep the tone professional. Do not repeat individual findings.

If no P0/P1 findings were found in any file:
    gh pr comment {PR_NUMBER} --repo {REPO} --body $'# [bot]Review Summary\nNo blocking issues found.'

If a `## Static Analysis` section was present in the input, include one sentence
noting whether any vibe-heal findings were relevant to the changes reviewed (or
"No overlapping static analysis findings").
