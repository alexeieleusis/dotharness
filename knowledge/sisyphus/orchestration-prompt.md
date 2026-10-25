# Prompt: Orchestrate one stage for one work item

> Every value in angle brackets below is a parameter. The caller substitutes all of them
> before you see this text. This prompt names the files you must follow. It does not copy them.

You orchestrate one stage for a single work item. Run only the stage named here.

- Stage to run now: `<stage>` (either `plan` or `build`)
- Work item: <work-item reference>
- Brief: <brief path>
- Handoff directory: <handoff dir>
- Target repository checkout: <target repo dir>
- Target branch (already checked out in that checkout): <target branch>
- Phase configuration file: <phase config path>
- Lifecycle cheat sheet: <cheat sheet path>
- Plan prompt: <plan prompt path>
- Build prompt: <build prompt path>
- Maximum retry cycles per leaf: <max retry cycles>
- Stop report file: <stop report path>

## First

Read the lifecycle cheat sheet, especially the section "Delegating plan and build to
sub-agents". Follow it to split the work among sub-agents.

## If the stage is `plan`

Run exactly one plan sub-agent. Give it the plan prompt, the phase configuration file, and the brief. Have it
follow the plan prompt end to end. Do not ask a person to review the overview
alone. An unreviewed overview does not stop the stage. Stop only when the open-questions file
that the planning step writes contains entries that cannot be resolved from the repository. In
that case, write the stop report with checkpoint `C2` and return. Do not build, push or open a
pull request.

## If the stage is `build`

Run exactly one build sub-agent, working inside the target repository checkout on the target
branch. Give it the build prompt and the phase configuration file. Have it follow the
build prompt. Do not use any automated end-to-end build command. The build prompt describes
the per-leaf procedure to use instead.

The build sub-agent launches one sub-agent per leaf, strictly one after the other and never in
parallel. For each leaf:

- require a handoff summary in the template the build prompt gives.
- save it to `handoffs/NN-<leaf name>.md` inside the handoff directory.
- give the paths of every earlier summary to the next leaf's sub-agent.

Commit only to the target branch. Never do any of the following:

- push
- open or edit a pull request
- approve or merge
- rewrite history
- change the base branch

## When to stop

When any of these happens, stop immediately. Do not guess. Do not start a later
leaf. Write
the stop report, then return:

- A leaf reports an open question: checkpoint `C3`.
- A leaf reports a deviation that may change a later leaf, or you cannot tell whether it does:
  checkpoint `C4`.
- A leaf still fails after the maximum retry cycles, or changes files outside its declared
  scope: checkpoint `C5`.

The stop report is a JSON object with the keys `checkpoint` (one of the codes above), `leaf`
(the leaf name, or an empty string), and `detail` (what a person needs to decide, in plain
words). When the stage finishes without a stop, do not write the stop report. Finish by listing
every commit, every deviation, and every question that was raised.
