# Prompt: Drive spec-prism-flow's Build Stage (Manual, Per-Phase)

> Point a coding-agent session at this file. No parameters need pre-filling: the agent
> resolves the phase corpus and target repo itself (see below). It asks the user only
> when it cannot resolve something on its own. It then implements an already-generated
> phase corpus one phase at a time, instead of using `spec-prism-flow build run`.
>
> This pairs with `tools/spec-prism-flow/docs/lifecycle-cheat-sheet.md`. That guide's
> "Recommended workflow" section explains why: `build run`'s own orchestration is the
> least mature part of the tool. This prompt is a deliberately thin, honest manual
> stand-in for it. The steps are: implement, check against acceptance criteria, commit,
> move on.

## Resolve the parameters yourself — don't ask to have them pre-filled

Whoever invokes you will point you at a project: a phase dir, or its
`.spec-prism-flow.toml` file. They will not hand you a filled-in list of values. Work
these out yourself. Ask the user only when you genuinely cannot resolve a value on your
own:

- **Phase dir**: Read `plan.phase_dir` from the project's `.spec-prism-flow.toml`, the
  same config used to generate this corpus. Or use the phase dir you were given directly.
- **Leaf phase files / dependency graph**: `<phase dir>/NN-name-leaf.md` and
  `<phase dir>/graph.json`. Both files should already exist once `plan draft-phases` has
  run. If either is missing, stop and say so. Do not try to build without them.
- **Target repo** (where the actual code changes and commits happen): Check
  `init_manifest.json` in the project's workspace dir for a `"code"` field.
  - If `plan init` ran with `--code`, that path is the target repo.
  - If the `"code"` field is null or missing, **ask the user** for the git checkout to
    build in. Never guess this value.
  - Never use the spec-prism-flow tool repo as the target repo. Never use the project's
    own planning directory either — that directory holds planning files only, and git
    does not track it.

## Reading the dependency graph

`graph.json` has `"nodes"` (leaf stems) and `"edges"`. Every edge is
`[dependent, dependency]`. **The arrow points from the second element to the first**: the
second element (the dependency) must be implemented and committed before the first
element (the dependent) can start. Read edges in that direction only.

Before implementing anything:

1. Load `graph.json`.
2. Compute one valid topological order over its nodes. Every node must appear only after
   all of its dependencies, per the edges above.
3. If `graph.json` disagrees with a leaf file's own `Depends on` section, stop. Flag the
   conflict. Do not guess which one is right.
4. Show the resulting sequence before you start implementing. This lets the user check it
   first.

## Implementing each phase

Go through that sequence **one phase at a time, strictly in order — never in parallel**.
This applies even when the graph shows no dependency between two phases. Running several
implementation sub-agents at the same time overloads the AI harness tool: too many
sessions running together can hit rate limits or compete for resources.

For each phase, in order:

1. Read that phase's leaf file in full.
2. Spawn a fresh sub-agent scoped to just that phase. Give it the leaf file's `Scope`,
   `Requirements`, and `Acceptance criteria`, word for word. Tell it to check its own work
   against the `Manual test checklist` before it reports that it is done. It must not
   touch files outside the declared `Scope`.
3. Once the sub-agent reports that the implementation is done, **commit the result** in
   the target repo. Use a message that names the phase, for example
   `Phase 04: <leaf name>`. Do this yourself — do not assume a commit happened on its own.
   This step matters most when the sub-agent's coding backend is `opencode` instead of
   Claude Code: an opencode session does not always commit on its own. Treat the commit
   step as your job, not something the sub-agent does automatically.
4. Confirm the commit exists (`git log -1`) before moving to the next phase. If a phase's
   sub-agent gets stuck, or produces work that does not meet its acceptance criteria,
   stop. Escalate the problem instead of moving on to the next phase. There is no retry
   budget and no PR-lifecycle process here. This build method is a deliberately thin,
   honest stand-in for `build run`, which is not yet reliable.

## Done when

Every node in the topological order has a matching commit in the target repo, in the
same order. Report the result: for each phase, name its commit hash.
