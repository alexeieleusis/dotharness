# Lifecycle cheat sheet: idea → merged code

One page, two halves. `plan` turns a rough idea into an agent-sized phase corpus.
`build` drives that corpus to merged PRs. Everything below assumes a
`.spec-prism-flow.toml` already exists for the target project. `plan.workspace_dir`
and `plan.phase_dir` are required. See [requirements.md §10](requirements.md#10-tool-shape).

## Step 0: Get a brief (before `plan init`)

`plan init` needs a `BRIEF.md`. Work usually starts from a ticket, so the brief comes first.
Ask a coding agent to do these steps:

1. Fetch the ticket (for example, from Linear).
2. Draft the brief with
   [`requirements-doc-drafting-prompt.md`](https://github.com/alexeieleusis/dotharness/blob/main/knowledge/spec-prism-flow/requirements-doc-drafting-prompt.md).
   Tell the agent to check the code. The brief must record what the code already implements,
   its shortcomings, and its constraints. The agent must resolve open questions from the
   repository where it can. It must leave only the real questions for you.
3. Save the brief as `BRIEF.md` in the project's workspace directory.

You answer the remaining open questions. The agent adds each answer to the brief as a
numbered decision. If the agent cannot find the code or a referenced document, it asks you
once, in one message, and carries on with what it has if you do not answer. Run `plan init`
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
Spawn a sub-agent to draft `requirements.md`. `draft-phases` just needs to be run and
its output reviewed — it writes the phase files itself from the leaf docs, so there is
nothing to draft. Spawn a sub-agent to run `plan review` and fix what it flags. Each
sub-agent reasons over one file only. It does not carry the whole plan's context forward
from stage to stage.

Use [`plan-generation-prompt.md`](https://github.com/alexeieleusis/dotharness/blob/main/knowledge/spec-prism-flow/plan-generation-prompt.md)
to run this without pre-filled parameters. Point a coding-agent session at that file
directly — it resolves the project, its config, and its brief on its own, and asks the
human only when it genuinely cannot.

You can drive `build` too, but the build phase is the least mature part of this tool. Do
not let one agent instance drive `build run` across the whole phase corpus in a single
session. Delegate a fresh sub-agent per phase instead. Each phase then gets a clean
context, and a stuck or misbehaving phase does not drag down the rest of the run.
Supervise `build` more closely than `plan`.

Use [`build-execution-prompt.md`](https://github.com/alexeieleusis/dotharness/blob/main/knowledge/spec-prism-flow/build-execution-prompt.md)
to run the build phase this way — implementing the phase corpus directly from the leaf
files and `graph.json`, one phase at a time, instead of using `spec-prism-flow build run`.

### Delegating plan and build to sub-agents

You can run this with one sub-agent for the plan phase and one for the build phase. Give each
sub-agent its prompt file and the project's config or `BRIEF.md`.

- **Plan sub-agent.** It drives `plan` with `plan-generation-prompt.md`. It returns to you at
  the three checkpoints, and only there: the brief, the review of `00-overview.md`, and
  `OPEN_QUESTIONS.md`. (You normally finish the brief in step 0, before it starts.)
- **Build sub-agent.** It drives `build` with `build-execution-prompt.md`. It launches its own
  sub-agents, one per phase. It runs them strictly in sequence, never in parallel.
- **Context between phases.** Each phase sub-agent ends its report with a handoff summary.
  The summary states what the sub-agent did, its deviations from the plan, context for later
  phases, and open questions. The build sub-agent saves each one to
  `handoffs/NN-<leaf name>.md` in the workspace directory and gives the paths of all earlier
  summaries to the next phase sub-agent. The template is in `build-execution-prompt.md`,
  under "Handoff summary". Tell the build sub-agent to require it.
- **Open questions.** A phase can raise an open question. A phase can also raise a deviation
  that changes a later phase. In both cases, the build sub-agent stops and returns to you. It
  does not guess. If it cannot tell whether a deviation affects a later phase, it asks. When
  you answer, it continues.
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
2. It prints the exact expected output path, then blocks on
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
`{node}_forced` retry when a Leaf verdict comes back oversized (over 15 files or over 1500 words;
undersized leaves are accepted, so a small project can end as a single phase) and depth has not hit
`--depth-cap` yet (default 4 — `DEFAULT_DEPTH_CAP` in `decompose.py`). For any
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
for them. `build run` and `build status` are different: both treat `Path.cwd()` as *the
clone* — the actual git checkout you are building. The clone's `origin` remote supplies
the `owner/repo` slug for every `gh pr` call and resume-state key, and
`config.build.commands` runs there as merge gates.

`uv --directory <spec-prism-flow-repo> run ...` changes the subprocess's directory to
`<spec-prism-flow-repo>` first. If you invoke a `build` command that way, it treats the
tool's own repo as the clone. That is wrong, and it is the likely cause if `build run`
complains about running from the wrong root. Use `--directory` for `plan` commands. For
`build` commands, `cd` into the actual target clone yourself, then use `--project`
instead — it points `uv` at spec-prism-flow's venv and dependencies without changing your
cwd.

`<spec-prism-flow-repo>` is not one fixed path — more than one checkout can exist on
disk. Check `~/.harness/tools/spec-prism-flow` first: that is the working install used
to run this tool day to day. Fall back to the dev checkout (wherever this repo's clone
lives, e.g. `~/development/dotharness/tools/spec-prism-flow`) only if the first path
does not exist.

```
# plan stage: cwd doesn't matter
uv --directory <spec-prism-flow-repo> run spec-prism-flow \
  plan draft-overview --config /abs/path/to/.spec-prism-flow.toml

# build stage: cwd must be the target project's own git checkout
cd /path/to/actual/project/checkout
uv run --project <spec-prism-flow-repo> spec-prism-flow \
  build run --config /abs/path/to/.spec-prism-flow.toml
```

## At a glance

| # | Command | Produces | Human checkpoint |
|---|---|---|---|
| 1 | `spec-prism-flow plan init BRIEF.md [--code ...] [--conventions ...]` | `init_manifest.json` | — |
| 2 | `spec-prism-flow plan draft-overview` | `00-overview.md`, `OPEN_QUESTIONS.md` | **Yes** — answer open questions / edit overview |
| 3 | `spec-prism-flow plan draft-requirements` | `requirements.md` | **Yes** — review/edit |
| 4 | `spec-prism-flow plan decompose [--depth-cap N]` | decomposition tree + `graph.json` | **Yes** — review the whole tree before phase files are drafted. Each leaf's mini-requirements doc is already written by this point |
| 5 | `spec-prism-flow plan draft-phases` | `{config.plan.phase_dir}/NN-name-leaf.md` per leaf | **Yes** — review phase files |
| 6 | `spec-prism-flow plan review` | pass/fail report (4 consistency checks) | Fix and re-run until it passes |
| 7 | `spec-prism-flow build run [--dry-run] [--resume] [--strict]` | merged PRs, one per phase, plus a completion log | Manual-test prompt per phase (advisory unless `--strict`) |
| 8 | `spec-prism-flow build status` | one row per phase: `pending`/`in progress`/`escalated`/`merged` | — |

Each `plan` stage is a single agent run against durable files on disk. Nothing
auto-advances past a stage without an explicit go-ahead. Every `plan` and `build`
command takes `--config PATH` (it defaults to `./.spec-prism-flow.toml`). See §0 above
for where the config file can live, and where cwd matters.

## 1. Plan — brief → phase corpus

```
spec-prism-flow plan init brief.md --conventions conventions.md
# → edit OPEN_QUESTIONS.md / 00-overview.md, then:
spec-prism-flow plan draft-overview
# → edit OPEN_QUESTIONS.md / 00-overview.md, then:
spec-prism-flow plan draft-requirements
# → review/edit requirements.md, then:
spec-prism-flow plan decompose
# → review the decomposition tree + graph.json, then:
spec-prism-flow plan draft-phases
# → review the generated {config.plan.phase_dir}/NN-name-leaf.md files, then:
spec-prism-flow plan review
```

`decompose` recursively splits `requirements.md` into a tree of chunks. It is an
`unfoldr` corecursion: each branch stops independently once it is small enough to be
one agent-sized leaf, or when a depth cap forces human review instead. `draft-phases`
then turns each **leaf only** into a five-section phase file (`Scope`, `Requirements`,
`Acceptance criteria`, `Manual test checklist`, `Depends on`). It flags any leaf
outside the sizing bands (~5–10 files, ~150k–200k token session budget) and never
silently ships it. `plan review` is the exit gate. It checks:

- the dependency chain is valid
- `graph.json` agrees with the dependency chain
- every section of `requirements.md` maps to a phase
- no open question is stale

## 2. Build — phase corpus → merged code

```
spec-prism-flow build run            # sequential (config.build.workers == 1, default)
spec-prism-flow build run --dry-run  # exercise the whole control flow, zero live subprocess calls
spec-prism-flow build status         # where does every phase currently stand?
```

Setting `config.build.workers > 1` switches `build run` to **parallel mode**. It reads
`graph.json` instead of the linear `Depends on` chain. It runs up to `workers`
eligible leaves concurrently. A leaf is eligible once everything it depends on has
merged. An escalation on one leaf blocks only its dependents, not the whole run.
You decide how to organize branches and stacks across concurrently-running leaves.

### Per-phase loop (`run_phase`, either mode)

```
implement (agent run) → commit
        │
        ▼
push branch → gh pr create
        │
        ▼
┌─── iterate cycle (capped at config.build.max_retry_cycles) ───┐
│  fetch/rebase → re-check file scope                           │
│  static analysis  (vibe-heal, if vibe_heal.enabled)            │
│  self-review + address-comments  (harness, if review.enabled)  │
│  unresolved review threads > 0?  ──────────────► loop          │
│  manual test checklist (human)                                 │
│    pass            → exit loop, go to merge                    │
│    fail + retry     → loop                                     │
│    fail + no retry  → --strict: raise, block merge              │
│                        no --strict (default): log it, proceed   │
└─────────────────────────────────────────────────────────────┘
        │
        ▼
merge gates (config.build.commands: build/lint/test) → gh pr merge --squash
        │
        ▼
append completion log record (phase, PR, cycles, escalations, diff stats)
```

A crash mid-loop is safe to resume with `--resume`. The resume-state file tracks the
open PR and the iterate cycle count. A restart continues the retry budget instead of
resetting it.

`vibe_heal.enabled` and `review.enabled` gate the two external-tool steps above. Both
are no-ops when off. Off is the default for any project without those tools configured.

## 3. At work: the always-on review cycle

If this project has `review.enabled = true` (backed by `pr-review`, i.e. `harness`),
that review is not the only one reviewing a PR `build run` opens. In this repo's owner's
actual working setup, `harness run all` already runs on a repeating cycle against
**every** open PR in the repo. This cycle runs independently of `spec-prism-flow`. It
does not know whether a given PR came from `build run` or from a human. In practice,
this cycle sweeps a PR `spec-prism-flow build run` opens the same way it would sweep
any hand-opened PR.

See `pr-review`'s command docs for what each `harness run all` step does and the
known vibe-heal/`review-requested` requested-reviewer race:
`tools/pr-review/docs/commands/index.md` and
`tools/pr-review/docs/commands/review-prs.md`.

Turning on `review.enabled`/`vibe_heal.enabled` in `.spec-prism-flow.toml` is
redundant for a repo already covered by the always-on cycle. You'd get two
independent review passes racing each other on the same PR. Leave them off unless
you're driving `build run` against a repo that **isn't** already swept by `harness run
all`.

## 4. Escalations — where to look

| Error | Raised when | Where it surfaces |
|---|---|---|
| `EmptyImplementationError` | agent run produced no commit | `build run` output, before any push |
| `RetryBudgetExhausted` | iterate cycle exceeded `config.build.max_retry_cycles` | `build run` output + completion log (`escalation_reason`) |
| `ManualTestFailed` | manual test failed and declined retry, under `--strict` only | `build run` output + completion log |
| Scope-guard violation | diff touched a file outside the phase's `Scope` | `build run` output |
| `PRNotMergeableError` | `gh pr merge` non-zero exit | `build run` output, with a `next_command` hint (e.g. `gh pr view N`) |

`build status` classifies every phase as `pending` / `in progress` / `escalated` /
`merged` from the completion log and the presence of the resume-state file. Run it
any time to see where a `build run` stopped without re-reading logs.
