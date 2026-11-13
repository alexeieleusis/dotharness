# Prompt: Refactor the code one build added, for one stack group

> Every value in angle brackets below is a parameter. The caller substitutes all of them
> before you see this text. This prompt names the files you must follow. It does not copy them.

You orchestrate the refactor of one stack group for a single work item. Run only this stage.

- Stage to run now: `<stage>`
- Work item: <work-item reference>
- Brief: <brief path>
- Handoff directory: <handoff dir>
- Target repository checkout: <target repo dir>
- Target branch (already checked out in that checkout): <target branch>
- Maximum retry cycles: <max retry cycles>
- Stop report file: <stop report path>
- Pattern set for this group: `<refactor_set>`
- Pattern set guide: <patterns dir>/<refactor_set>/QUICK_START_MIGRATION.md
- Input file: <input_file>
- Files in this group (one repo-relative path per line):

<group_files>

## First

Read the pattern set guide. Read `<input_file>` and use only the entry whose `set` equals
`<refactor_set>`, for context; never edit that file. Do not read any other handoff.

## What to do

Apply the group's pattern set incrementally and behavior-preserving, following the guide's
migration steps. The code must work after every step. Commit each step to the target branch. Use
one sub-agent per file in the group, one at a time, strictly one after the other and never in
parallel. Give each sub-agent the guide path, the file path and the rules in this section. Then:

- Touch only the files listed above (`<group_files>`). New files you add (extracted modules) are allowed.
- Edit an existing file that is not in the list only on lines that reference an added file (an
  import or a call site), and list each such file in the handoff.
- Do not refactor tests beyond keeping them passing. Do not change a test's assertions.
- Do not change behavior. Do not fix a bug, add a feature or rename a public interface. If you
  find a defect, report it as a deviation in the handoff and keep the behavior as it is. If the
  defect makes a behavior-preserving refactor impossible, stop with checkpoint `C3`.
- A refactor that changes nothing is valid. It still needs the handoff.

## Rules

- Run every sub-agent in the foreground and wait for its report. Never use `run_in_background`, and never
  end your turn while a sub-agent is running: when your session ends, the sub-agent is killed mid-edit
  and its work is lost.
- The stage is complete only when every change is committed, the handoff is saved and
  `git status` is clean. The orchestrator checks this and stops the item otherwise.
- Commit only to the target branch. Never do any of the following:
  - push
  - open or edit a pull request
  - approve or merge
  - rewrite history
  - change the base branch

## When to stop

When any of these happens, stop immediately. Do not guess. Write the stop report, then return:

- A sub-agent reports an open question: checkpoint `C3`.
- A deviation that may change the group's other files, or you cannot tell whether it does:
  checkpoint `C4`.
- A file still fails after the maximum retry cycles, or a change is needed outside the allowed
  scope: checkpoint `C5`.

The stop report is a JSON object with the keys `checkpoint` (one of the codes above), `leaf`
(the group's set name, or an empty string), and `detail` (what a person needs to decide, in plain
words), written to `<stop report path>`. When the stage finishes without a stop, do not write the
stop report. Finish by listing every commit, every deviation, and every edit to an existing file.

## Handoff

Before you finish, save the handoff for this group as `refactor-<refactor_set>.md`, directly in `<handoff dir>`, not in a subdirectory of it. Write it even when the refactor changed nothing: the orchestrator checks that it exists. The name starts with `refactor-`, never with two digits and a hyphen, so it cannot be mistaken for a file another stage wrote.

The file holds these two bullets. Each one starts at the beginning of a line. Write them exactly as shown, with this text before the colon, because the orchestrator finds them by that text. Put nothing between the two bullets, and put the second one after the first. Any further notes go below both bullets, after a blank line.

- `- Deviations from the plan:` followed, on the same line, by every deviation: a guide step you skipped or changed, a defect you found and left as it is, anything you did differently from this prompt. Separate several deviations with `; `. Write `none` when there are none.
- `- Edits to existing files:` followed, on the same line, by one item for each existing file you edited that is not listed under Files in this group. Write each item as `path (reason)`: the path relative to the repository root, then the reason in parentheses, with no `;` inside the reason. Separate items with `; `. Write `none` when there are none. List a file even when the edit is one import line. Do not list files you only added.

Example of a handoff for a refactor that changed nothing outside the group:

```
- Deviations from the plan: none
- Edits to existing files: none
```

Example of a handoff with entries:

```
- Deviations from the plan: skipped the guide's step 3, the group has no I/O to extract; found an unchecked None in the parser and left it as it is
- Edits to existing files: src/app/main.py (import of the extracted module); src/app/routes.py (call site of the moved handler)
```
