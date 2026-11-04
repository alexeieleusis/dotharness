# Review Summary Instructions

You just completed a per-file code review of a local branch. Write the final summary.

Write it to this Markdown file, using plain file tools, and write nothing else
anywhere outside it:

    {OUTPUT_FILE}

Do not run `gh` and do not post anywhere. The summary should be concise (3-6
sentences). Cover:
- A 1-2 sentence executive summary of what the change does.
- Whether any P0/P1 findings were written (or "No blocking issues found").
- Any cross-cutting concern that spans multiple files (optional).

Keep the tone professional. Do not repeat individual findings. Do not write finding
blocks.

If no P0/P1 findings were found in any file, write:

    No blocking issues found.

If a `## Static Analysis` section was present in the input, include one sentence
noting whether any vibe-heal findings were relevant to the changes reviewed (or
"No overlapping static analysis findings").
