# Prompt: Drive spec-prism-flow's Plan Stage

> Point a coding-agent session (e.g. Claude Code) at this file. No parameters need
> pre-filling. The agent resolves the project, its config, and its brief itself (see
> below). It asks the user only when it genuinely cannot resolve something. It then
> drives `spec-prism-flow`'s `plan` stage end-to-end: one sub-agent per step, strictly
> sequential, pausing only at the human checkpoints the cheat sheet names. This prompt
> pairs with `tools/spec-prism-flow/docs/lifecycle-cheat-sheet.md`, which stays the
> source of truth for every command, flag, and gotcha below.

## Resolve the parameters yourself — do not ask to have them pre-filled

Whoever invokes you will point you at a project: a directory, a name, or a path near its
`.spec-prism-flow.toml`. They will not hand you a filled-in list. Work these out
yourself, in this order. Ask the user only when a step genuinely cannot resolve:

- **Tool repo**: more than one copy can exist on disk. Check
  `~/.harness/tools/spec-prism-flow` first — that is the working install used to run
  this tool day to day. Fall back to `~/development/dotharness/tools/spec-prism-flow`
  (the dev checkout) only if the first path does not exist. When *both* exist, that
  order does not disambiguate — it silently favors the install. Before settling, look
  for divergence: is the dev checkout dirty or ahead of the install (`git status`,
  commit, or mtime)? If so, it is unclear which one the invoker means — ask, because
  the likely case is a contributor testing in-progress tool changes and getting the
  stale install instead. Whichever copy you end up using, state out loud which repo
  you picked and why, so a stale install can't be used by accident without the
  invoker knowing.
- **Config file**: the `.spec-prism-flow.toml` you were pointed at, or pointed near (for
  example, under `.spec-prism-flow_projects/<project>/`). If more than one plausible
  match exists, or none does, ask which project or config to use. Do not guess.
- **Workspace dir / phase dir**: read `plan.workspace_dir` / `plan.phase_dir` directly
  from that config file once you find it. Never ask for these separately — the config
  already gives you both.
- **Brief**: check whether `init_manifest.json` already exists in the workspace dir. If
  it does, `plan init` has already run. Read the brief's exact path from that file's
  `"brief"` field. Do not guess a filename. If `init_manifest.json` does not exist yet,
  `plan init` has not run. Ask the user for the brief file's path. Then run `plan init`
  yourself before you continue to `draft-overview`.
- **Invocation pattern** for every `plan` command (the current directory does not matter
  for `plan`):
  `uv --directory <tool repo> run spec-prism-flow plan <subcommand> --config <config file>`

## Your task

Read `<tool repo>/docs/lifecycle-cheat-sheet.md` in full before you do anything else. It
is the source of truth for every command, flag, and gotcha below. If anything here
conflicts with it, the cheat sheet wins. Treat this prompt as an index into the cheat
sheet, not a replacement for it.

Check whether `init_manifest.json` exists in the workspace dir. If it does not, run
`plan init <brief path> --config <config file>` first. Use the brief path you found
above. If it does, skip straight to `draft-overview`. From there, drive each remaining
stage with **one sub-agent per step**: `draft-overview` → `draft-requirements` →
`decompose` → `draft-phases` → `review`.

Do this **strictly sequentially, one file or handoff at a time. Never run steps in
parallel.** Keep only one sub-agent active at a time. Finish and confirm the current
stage — or, inside `decompose`, the current tree node's handoff — before you spawn the
next sub-agent. Do not spawn multiple sub-agents in parallel to save time, even where
the content looks independent. Running several agent sessions in parallel overloads the
AI harness tool.

For each stage:

1. Spawn a fresh sub-agent whose only job is that one stage.
2. Run the CLI command for that stage. Watch its output for `Prompt written to: X` /
   `Expected output at: Y` pairs. This is the handoff protocol. The cheat sheet's "How
   the handoff actually works" section describes it. For each pair, do the following in
   order:
   1. Read `X`.
   2. Do the actual drafting or reviewing work it describes.
   3. Write the result to exactly `Y`.
   4. Confirm the file exists on disk.
   5. Only then, answer the `Agent finished writing output?` prompt with `y`.

   Do not answer `y` before `Y` exists. Answering early crashes the whole command
   (`HandoffError`).

   The text of `X` *is* the sub-agent's prompt. Do not paraphrase it or write your own
   task description instead. Hand `X`'s contents to the sub-agent verbatim as its
   instructions. Tell the sub-agent to write its result to exactly `Y`. For example,
   given:

   ```
   Prompt written to: .../decompose_A-1-1-1-2_prompt.md
   Expected output at: .../decompose_A-1-1-1-2_output.md
   ```

   the sub-agent's entire task is: follow `decompose_A-1-1-1-2_prompt.md` as written,
   and save what it produces to `decompose_A-1-1-1-2_output.md`. Nothing more is needed.
3. `decompose` triggers this loop many times: once per tree node it visits, plus a
   `_forced` retry for any oversized node. A non-trivial requirements doc can easily
   need a dozen or more round trips. Keep cycling read-prompt → write-output → confirm.
   Stop only when the CLI itself stops asking and reaches the final "Tree approved and
   ready to continue?" pause.
4. Stop at every human checkpoint the cheat sheet's table names. Wait there for
   explicit sign-off before you continue:
   - after `draft-overview`: show `00-overview.md` and `OPEN_QUESTIONS.md`
   - after `draft-requirements`: show `requirements.md`
   - after `decompose`: show the decomposition tree before you confirm "Tree approved"
   - after `draft-phases`: show the generated leaf files under the phase dir
   Do not approve any of these on your own. Do not edit `OPEN_QUESTIONS.md`,
   `00-overview.md`, or `requirements.md` ahead of time without explaining what you
   changed and why.
5. Run `plan review` last. If it fails, spawn a sub-agent to fix exactly what it flags,
   then re-run `review` and repeat. But unlike the `decompose` loop, `review` has no
   built-in stop, so bound it: if the same finding has failed review 3 times in a row,
   stop and report it to the human — name the finding and what each attempt tried to
   change — instead of retrying again. A fixer that keeps missing the same spot, or two
   findings that keep trading off against each other every round, will not converge on
   its own; surface it rather than spinning up an unbounded chain of fixer sessions.

## Done when

`plan review` passes with no errors. Report back:

- the phase dir path
- the number of leaf files produced
- a one-line summary of anything still open in `OPEN_QUESTIONS.md`
