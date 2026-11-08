# Prompt: Drive the spec_prism_flow Build Stage (Manual, Per-Phase)

> Point a coding-agent session at this file. No parameters need pre-filling: the agent
> resolves the phase corpus and target repo itself (see below). It asks the user only
> when it cannot resolve something on its own. It then implements an already-generated
> phase corpus one phase at a time. `spec_prism_flow` has no `build` command group, so this
> prompt is the build stage.
>
> This pairs with `lifecycle-cheat-sheet.md` (next to this file). This prompt is a
> deliberately thin, honest manual build: implement, check against acceptance criteria,
> commit, move on.

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
  - The workspace dir is `plan.workspace_dir` in the same `.spec-prism-flow.toml`.
    It is not generally derivable from the phase dir. If you only have a phase dir
    and the `.toml` is not reachable, **ask the user** for the project's workspace
    dir before looking for the manifest.
  - If `plan init` ran with `--code`, that path is the target repo.
  - If the `"code"` field is null or missing, **ask the user** for the git checkout to
    build in. Never guess this value.
  - Never fall back to the dotharness checkout (the `spec_prism_flow` tool) as the target repo
    implicitly. Use it only when `init_manifest.json` explicitly names it in `code`. Never use the project's
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
2. Spawn a fresh sub-agent, **in the foreground** (never `run_in_background`; do not end your turn until it reports) scoped to just that phase. Give it the leaf file's `Scope`,
   `Requirements`, and `Acceptance criteria`, word for word. Also give it the paths of the
   handoff summary files of the phases already done (see "Handoff summary" below). Tell it to check its
   own work against the `Manual test checklist` before it reports that it is done. It must
   not touch files outside the declared `Scope`. The one exception is test files for the
   phase's own new or changed code. Tell it to write tests for the behaviour the phase
   implements, and to create or change those test files even when they are not in `Scope`.
   Adding these tests is not a reason to stop or to raise an open question. It must not
   list "should I write tests?" as an open question. This exception applies only to this
   prompt-driven build; a mechanical scope guard would have no such exception. Tell it to end its report with its own
   handoff summary.
3. Once the sub-agent reports that the implementation is done, **commit the result** in
   the target repo. Use a message that names the phase, for example
   `Phase 04: <leaf name>`. Do this yourself — do not assume a commit happened on its own.
   This step matters most when the sub-agent's coding backend is `opencode` instead of
   Claude Code: an opencode session does not always commit on its own. Treat the commit
   step as your job, not something the sub-agent does automatically.
4. Confirm the commit exists (`git log -1`) before moving to the next phase. If a phase's
   sub-agent gets stuck, or produces work that does not meet its acceptance criteria,
   stop. Escalate the problem instead of moving on to the next phase. There is no retry
   budget and no PR-lifecycle process here. This build method is deliberately thin.

## Handoff summary

Sub-agents run one after another. Each starts with a clean context. The summary is the only
thing that carries context forward. Each sub-agent must end its report with one. Keep it
short and concrete. Save each summary yourself as `handoffs/NN-<leaf name>.md` in the
workspace directory (`plan.workspace_dir`), which is per item, right after the phase's commit. Give the next sub-agent the
paths of all earlier summary files, in phase order, and tell it to read them. If the session
dies, or you stop on an open question, the saved files let a new session resume without
losing context.

```
### Handoff: Phase NN — <leaf name>
- Done: what now exists. Give file paths and the names of new functions, types, or commands.
- Deviations from the plan: each difference from the leaf file (scope, names, shapes,
  skipped work, added work), and why. Write "None" if there are none.
- Context for later phases: facts the next phases must know, such as a helper to reuse, a
  constraint found in the code, or a slow or flaky test. Also list the tests you added or
  changed: file paths, and what each covers.
- Open questions: each question that needs a human decision. Write "None" if there are none.
```

You are the orchestrator. Read every summary before you start the next phase. Then apply
these rules:

- If a deviation changes the `Scope`, `Requirements`, or dependencies of a later phase, stop.
  Tell the user. Do not pass the deviation on silently. If you cannot tell whether a
  deviation affects a later phase, treat it as if it does, and ask the user.
- If a summary lists an open question, stop. Ask the user. Continue after the user answers.
  Do not guess an answer.
- A test-only deviation does not trigger the rules above. That holds even when a later
  phase lists the same test file in its `Scope`: the later phase extends the file. A
  test-only deviation is made only of new or changed test files that exercise only code
  the phase itself added or changed. It leaves the phase's `Requirements` unchanged, and
  changes no later phase's `Scope` except by that same-file extension.
- If a phase's deviations include anything besides such tests, the rules above apply to
  the whole summary unchanged. Examples: a changed `Scope`, a renamed or reshaped
  interface, skipped work, or work that changes a later phase's `Requirements` or
  dependencies. Tests for code the phase did not add or change are not exempt.

Give the orchestrator's handoff paths, as before, to later phases. They carry the test
list from `Context for later phases`.

## Done when

Every node in the topological order has a matching commit in the target repo, in the
same order. Report the result. For each phase, name its commit hash. List every deviation
and every question raised, and state how each was resolved.
