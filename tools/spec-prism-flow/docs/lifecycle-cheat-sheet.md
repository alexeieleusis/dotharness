# Lifecycle cheat sheet: idea → merged code

One page, two halves: `plan` turns a rough idea into an agent-sized phase corpus;
`build` drives that corpus to merged PRs. Everything below assumes a
`.spec-prism-flow.toml` already exists for the target project (`plan.workspace_dir`,
`plan.phase_dir` are required — see [requirements.md §10](requirements.md#10-tool-shape)).

## At a glance

| # | Command | Produces | Human checkpoint |
|---|---|---|---|
| 1 | `spec-prism-flow plan init BRIEF.md [--code ...] [--conventions ...]` | `init_manifest.json` | — |
| 2 | `spec-prism-flow plan draft-overview` | `00-overview.md`, `OPEN_QUESTIONS.md` | **Yes** — answer open questions / edit overview |
| 3 | `spec-prism-flow plan draft-requirements` | `requirements.md` | **Yes** — review/edit |
| 4 | `spec-prism-flow plan decompose [--depth-cap N]` | decomposition tree + `graph.json` | **Yes** — review the whole tree before phase files are drafted (each leaf's mini-requirements doc is already written by this point) |
| 5 | `spec-prism-flow plan draft-phases` | `{config.plan.phase_dir}/NN-name-leaf.md` per leaf | **Yes** — review phase files |
| 6 | `spec-prism-flow plan review` | pass/fail report (4 consistency checks) | Fix and re-run until it passes |
| 7 | `spec-prism-flow build run [--dry-run] [--resume] [--strict]` | merged PRs, one per phase, plus a completion log | Manual-test prompt per phase (advisory unless `--strict`) |
| 8 | `spec-prism-flow build status` | one row per phase: `pending`/`in progress`/`escalated`/`merged` | — |

Each `plan` stage is a single agent run against durable files on disk — nothing
auto-advances past a stage without an explicit go-ahead. Every `build`/`plan` command
takes `--config PATH` (defaults to `./.spec-prism-flow.toml`).

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

`decompose` recursively splits `requirements.md` into a tree of chunks (an `unfoldr`
corecursion — each branch bottoms out independently once it's small enough to be one
agent-sized leaf, or a depth cap forces human review instead). `draft-phases` then
turns each **leaf only** into a five-section phase file (`Scope`, `Requirements`,
`Acceptance criteria`, `Manual test checklist`, `Depends on`) and flags — never
silently ships — any leaf outside the sizing bands (~5–10 files, ~150k–200k token
session budget). `plan review` is the exit gate: dependency chain valid, `graph.json`
agrees with it, every requirements section maps to a phase, no stale open questions.

## 2. Build — phase corpus → merged code

```
spec-prism-flow build run            # sequential (config.build.workers == 1, default)
spec-prism-flow build run --dry-run  # exercise the whole control flow, zero live subprocess calls
spec-prism-flow build status         # where does every phase currently stand?
```

Setting `config.build.workers > 1` switches `build run` to **parallel mode**: it reads
`graph.json` instead of the linear `Depends on` chain and runs up to `workers`
eligible leaves concurrently (a leaf is eligible once everything it depends on has
merged). An escalation on one leaf blocks only its dependents, not the whole run.
Branch/stack organization across concurrently-running leaves is left to you.

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

A crash mid-loop is safe to resume (`--resume`): the resume-state file tracks the open
PR and the iterate cycle count, so a restart picks the retry budget back up instead of
resetting it.

The `vibe_heal.enabled`/`review.enabled` config flags are what gate the two
external-tool steps above — both are no-ops when off, which is the default for any
project without those tools configured.

## 3. At work: the always-on review cycle

Where this project has `review.enabled = true` (backed by `pr-review`, i.e. `harness`),
that's not the only thing reviewing a PR `build run` opens. In this user's actual
working setup, `harness run all` is already running on a repeating cycle against
**every** open PR in the repo — independent of `spec-prism-flow` and unaware that a
particular PR came from `build run` rather than a human. `harness run all` runs, in
order, continuing past a failing step and exiting non-zero only if one failed:

| Step | Scope | What it does |
|---|---|---|
| `review-prs` | every open, non-draft PR | vibe_heal/SonarQube-style static-analysis sweep, posted as PR comments — this is the "vibe-heal/sonarqube" step |
| `focused-review` | PRs where you're author/assignee/requested reviewer | elaborates SonarQube findings that cite a refactor-knowledge file into a detailed comment |
| `self-review` | your own open PRs (`--author @me`) | AI backend does an automated first-pass review — file-by-file, summary, design, requirement-traceability — before a human looks at it |
| `review-requested` | PRs where GitHub review was explicitly requested from you | AI backend produces inline + summary review comments, reacting to review-request state |
| `address-comments` | your open PRs, or ones you're assigned to, with pending feedback | AI backend reads unresolved comments, makes the smallest fix or replies, commits, pushes |

Practically, this means a PR `spec-prism-flow build run` opens gets picked up by this
cycle the same way any hand-opened PR would — you don't need `.spec-prism-flow.toml`'s
`review.enabled`/`vibe_heal.enabled` turned on just to get static analysis or an AI
review; the always-on cycle already covers it on its own cadence.

**Known interaction (now fixed):** `review-prs`' vibe-heal step posts a real GitHub PR
*review* (not just a comment), which clears you from GitHub's requested-reviewers list
as a side effect — this used to knock a PR off `review_requested`'s radar for one `all`
cycle. `review_prs.py`'s `_process_pr` now re-adds the reviewer synchronously right
after posting (it checks `get_requested_reviewers` up front, then calls `add_reviewer`
once any subdir was processed), so `review_requested` sees you as requested again
within the same cycle — no reliance on GitHub's own ~2-minute re-add.

Turning on `review.enabled`/`vibe_heal.enabled` in `.spec-prism-flow.toml` on top of
this is redundant for a repo already covered by the always-on cycle: you'd get two
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
`merged` from the completion log plus resume-state-file presence — run it any time to
see where a `build run` left off without re-reading logs.
