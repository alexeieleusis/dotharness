# Lifecycle cheat sheet: idea → phase corpus

`plan` turns a rough idea into an agent-sized phase corpus. The build side
(`build run`, `build status`) is not migrated into `dotharness`. sisyphus builds the
corpus, or a coding agent that follows `build-execution-prompt.md` builds it. Everything below
assumes a `.spec-prism-flow.toml` already exists for the target project. `plan.workspace_dir`
and `plan.phase_dir` are required. See [requirements.md §10](requirements.md#10-tool-shape).

## Step 0: Get a brief (before `plan init`)

`plan init` needs a `BRIEF.md`. Work usually starts from a ticket, so the brief comes first.
Ask a coding agent to do these steps:

1. Fetch the ticket (for example, from Linear).
2. Draft the brief with
   [`requirements-doc-drafting-prompt.md`].
   Tell the agent to check the code. The brief must record what the code already implements,
   its shortcomings, and its constraints. The agent must resolve open questions from the
   repository where it can. It must leave only the real questions for you.
3. Save the brief as `BRIEF.md` in the project's workspace directory.

You answer the remaining open questions. The agent adds each answer to the brief as a
numbered decision. If the agent cannot find the code or a referenced document, it asks you
once, in one message. If you do not answer, it carries on with what it has. Run `plan init`
when you confirm that no open question blocks the requirements document.

## Drive this yourself

You are the agent that drives `plan`, end to end. Do not read the commands below as a
manual checklist for a human to type. The human in the loop does three things:

- Writes the brief.
- Reviews `00-overview.md` once `draft-overview` produces it.
- Answers or resolves `OPEN_QUESTIONS.md`.

You run everything else in `plan` yourself: `draft-requirements`, `decompose`,
`draft-phases`, `review`, and any fix that `plan review` flags. Return to the human only
at those three checkpoints.

Delegate each step to its own sub-agent. Do not run the whole pipeline in one context.
Spawn a sub-agent to draft `requirements.md`. Run `draft-phases` and review its
output. It writes the phase files itself from the leaf docs, so there is nothing to
draft. Spawn a sub-agent to run `plan review` and fix what it flags. Each
sub-agent reasons over one file only. It does not carry the whole plan's context
from stage to stage.

Use `plan-generation-prompt.md` (packaged at `src/dotharness/knowledge/spec-prism-flow/`)
to run this without pre-filled parameters. Point a coding-agent session at that file
directly. It resolves the project, its config, and its brief on its own. It asks the
human only when it genuinely cannot.

The build side is not part of `spec_prism_flow`. To implement the phase corpus, use
`build-execution-prompt.md` (packaged at `src/dotharness/knowledge/spec-prism-flow/`).
It implements the corpus directly from the leaf files and `graph.json`, one phase at a
time, with a fresh sub-agent per phase.

### Delegating plan and build to sub-agents

You can run this with one sub-agent for the plan phase and one for the build phase. Give each
sub-agent its prompt file and the project's config or `BRIEF.md`.

- **Plan sub-agent.** It drives `plan` with `plan-generation-prompt.md`. It returns to you at
  the three checkpoints, and only there: the brief, the review of `00-overview.md`, and
  `OPEN_QUESTIONS.md`. (You normally finish the brief in step 0, before it starts.)
- **Build sub-agent.** It drives the build with `build-execution-prompt.md`. It launches its own
  sub-agents, one per phase. It runs them strictly in sequence, never in parallel.
- **Context between phases.** Each phase sub-agent ends its report with a handoff summary.
  The summary states what the sub-agent did, its deviations from the plan, context for later
  phases, and open questions. The build sub-agent saves each one to
  `handoffs/NN-<leaf name>.md` (one per item) in the workspace directory
  (`plan.workspace_dir`). It gives the paths of all earlier summaries to the next
  phase sub-agent. The template is in `build-execution-prompt.md`,
  under "Handoff summary". Tell the build sub-agent to require it.
- **Open questions.** A phase can raise an open question. A phase can also raise a deviation
  that changes a later phase. In both cases, the build sub-agent stops and returns to you. It
  does not guess. If it cannot tell whether a deviation affects a later phase, it asks. When
  you answer, it continues.
  A deviation made only of tests for the phase's own behaviour does not stop the build. The
  sub-agent records those tests in `Context for later phases`. The definition is in
  `build-execution-prompt.md`.
- **Done.** Each sub-agent reports when it finishes. The build report lists every commit,
  every deviation, and every question raised.

Example instruction to the build sub-agent: "Follow `build-execution-prompt.md` for the
project at `<path>`. Launch one sub-agent per phase, in order. Require each sub-agent to
return a handoff summary, and save it under `handoffs/`. Give the earlier summary paths to the next sub-agent. Report when you
finish. Return to me when an open question arises."

### How the handoff actually works

You can only delegate to a sub-agent once you know what each stage is actually waiting
for. Every drafting step — `draft-overview`, `draft-requirements`, and
each tree node `decompose` visits — blocks on the same protocol
(`spec_prism_flow/handoff.py`):

1. It writes `{stage}_prompt.md` into `workspace_dir` and prints its path. It also copies
   the path, best-effort, to the clipboard (`pbcopy`) and, inside tmux, to the paste
   buffer. It skips this silently if neither is available.
2. It prints the exact expected output path. Then it blocks on
   `Agent finished writing output? [y/N]`.
3. You must read that prompt, do the drafting, and write the file at the exact expected
   path before you answer "y".

If you answer "y" before the output file exists, the command does not just re-prompt. It
raises `HandoffError: Expected output file not found` and stops the whole command
outright. Watch for each `Prompt written to:` / `Expected output at:` pair. Read the
prompt **file** itself, not the clipboard — that is a manual-mode convenience for a
human, and you cannot see it. Write the output file. Only then send "y" to the
subprocess's stdin.

`decompose` runs this protocol once per tree node, not once for the whole command. Each
oversized node can cost two handoffs: the natural split/leaf attempt, plus a
`{node}_forced` retry. The retry happens when a Leaf verdict comes back oversized and
depth has not hit `--depth-cap` yet. Oversized means over 15 files or over 1500 words.
Undersized leaves are accepted, so a small project can end as a single phase.
`--depth-cap` defaults to 4 (`DEFAULT_DEPTH_CAP` in `decompose.py`). For any
requirements doc that is not tiny, expect a couple dozen prompt/output round trips before
the tree is done, not one. Budget for that. Consider passing `--depth-cap` on purpose
instead of accepting the default: it bounds how deep the recursion goes, and so how many
handoffs it costs.

## 0. Config location and cwd

The config file does not have to sit at the invoking directory's root. `--config PATH`
works on any command and can point at the file wherever it lives. `./.spec-prism-flow.toml`
is only the default when you omit `--config`. The tool resolves `plan.workspace_dir` and
`plan.phase_dir` inside that file against the invoking process's **cwd**, not against the
config file's own location. Write them as absolute paths if you want one config to work
no matter where you invoke it from.

`plan` commands do not depend on a target repository, so cwd does not otherwise matter
for them.

`uv --directory <dotharness-repo> run ...` changes the subprocess's directory to
`<dotharness-repo>` first. That is fine for `plan` commands.

```
# plan stage: cwd doesn't matter
uv --directory <dotharness-repo> run spec_prism_flow \
  plan draft-overview --config /abs/path/to/.spec-prism-flow.toml
```

## At a glance

| # | Command | Produces | Human checkpoint |
|---|---|---|---|
| 1 | `spec_prism_flow plan init BRIEF.md [--code ...] [--conventions ...]` | `init_manifest.json` | — |
| 2 | `spec_prism_flow plan draft-overview` | `00-overview.md`, `OPEN_QUESTIONS.md` | **Yes** — answer open questions / edit overview |
| 3 | `spec_prism_flow plan draft-requirements` | `requirements.md` | **Yes** — review/edit |
| 4 | `spec_prism_flow plan decompose [--depth-cap N]` | decomposition tree + `graph.json` | **Yes** — review the whole tree before phase files are drafted. Each leaf's mini-requirements doc is already written by this point |
| 5 | `spec_prism_flow plan draft-phases` | `{config.plan.phase_dir}/NN-name-leaf.md` per leaf | **Yes** — review phase files |
| 6 | `spec_prism_flow plan review` | pass/fail report (4 consistency checks) | Fix and re-run until it passes |

Each `plan` stage is a single agent run against durable files on disk. Nothing
auto-advances past a stage without an explicit go-ahead. Every `plan`
command takes `--config PATH` (it defaults to `./.spec-prism-flow.toml`). See §0 above
for where the config file can live, and where cwd matters.

## 1. Plan — brief → phase corpus

```
spec_prism_flow plan init brief.md --conventions conventions.md
# → edit OPEN_QUESTIONS.md / 00-overview.md, then:
spec_prism_flow plan draft-overview
# → edit OPEN_QUESTIONS.md / 00-overview.md, then:
spec_prism_flow plan draft-requirements
# → review/edit requirements.md, then:
spec_prism_flow plan decompose
# → review the decomposition tree + graph.json, then:
spec_prism_flow plan draft-phases
# → review the generated {config.plan.phase_dir}/NN-name-leaf.md files, then:
spec_prism_flow plan review
```

`decompose` recursively splits `requirements.md` into a tree of chunks. It is an
`unfoldr` corecursion. Each branch stops independently once it is small enough to be
one agent-sized leaf, or when a depth cap forces human review instead. `draft-phases`
then turns each **leaf only** into a five-section phase file (`Scope`, `Requirements`,
`Acceptance criteria`, `Manual test checklist`, `Depends on`). It flags any leaf
outside the sizing bands (~5–10 files, ~150k–200k token session budget) and never
silently ships it. `plan review` is the exit gate. It checks:

- the dependency chain is valid
- `graph.json` agrees with the dependency chain
- every section of `requirements.md` maps to a phase
- no open question is stale
