# Spec Prism Flow — Vision & Requirements (draft)

> This document uses the tool's resolved name, **Spec Prism Flow**, throughout (see §11 item 1 for
> the naming history).

## 0. Purpose / origin

**Spec Prism Flow** is an engine for Specification-Driven Development for autonomous agents. It runs
end to end, from a high-level idea to merged code. It has two halves. `plan` (Feature A, §7) turns a
single high-level system prompt or product goal into exhaustive, validated, deterministic
technical requirements. It then recursively splits those requirements into chunks (§4). Formally,
this is an `unfoldr`-style corecursion: a generate-and-split process that keeps recursing until each
piece is small enough to stop on, or a depth cap is hit and the oversized chunk is flagged for human
review instead (§7.2 step 6 gives it a precise definition). The goal is to explore a feature space
and map edge cases *before* a single line of application code is generated. `build` (Feature B, §8)
then executes the resulting phase corpus. It is the same tool, carried through to the code it
planned for (§11 #8).

This is risk mitigation, not risk removal. `plan`'s rigor is meant to prevent rework and to keep an
agent from stalling on ambiguity mid-implementation. It is not meant to eliminate ambiguity
outright, or to imply that `build` sits outside the tool's scope. Where ambiguity genuinely can't be
resolved, G3 has the tool surface it as an explicit open decision instead of guessing.

This tool generalizes a pattern first proven in the [`agentic-neighboku-lensflow`](https://github.com/alexeieleusis/agentic-neighboku-lensflow)
repo: its [`docs/neighboku-ai-rebuild/`](https://github.com/alexeieleusis/agentic-neighboku-lensflow/tree/main/docs/neighboku-ai-rebuild/)
planning docs, its [`docs/phases/`](https://github.com/alexeieleusis/agentic-neighboku-lensflow/tree/main/docs/phases/)
corpus (20 files), and the `scripts/orchestrate/` engine that drives them. That experiment rebuilt
an existing game from scratch via about 20 small phases, each independently implementable by a
coding agent. It worked: each phase was self-contained enough that a coding agent could implement
it, get it reviewed, and merge it in one session. But it succeeded only by reverse-engineering
requirements from a **finished reference implementation**: full source, full git history, in-app
docs, and a narrated walkthrough video. Spec Prism Flow is what's left when that reference doesn't
exist. It keeps the same two capabilities — turning requirements into agent-sized phases and
executing those phases safely — but is built for the normal case of starting from a rough idea
instead of a finished product.

## 1. Problem statement

A coding agent (Claude Code, opencode) does its best work against a small, unambiguous,
self-contained work order. That work order has these properties: a fixed file scope, requirements
quoted in full rather than referenced, one architectural convention stated explicitly, and testable
acceptance criteria. Handing an agent a full PRD, or a vague one-line feature request, produces
worse results than handing it a phase file with these properties. The worse results are: scope
creep, architectural drift across features, and requirements the agent has to guess at.

The Neighboku rebuild got such phase files "for free": every ambiguity had a ground-truth answer
in the original code. The requirements doc's job was just to transcribe and organize what was
already there, flagging the handful of real discrepancies for human judgment. Most real projects —
including whatever the user builds next — start with no such reference. The requirements exist only
as an idea, a rough brief, maybe a competitor to point at. Getting from that starting point to a
phase corpus with the same tight, low-ambiguity properties needs a *process*, not just a
transcription pass. The process needs to be repeatable, because this tool is meant to be used on
every new greenfield (or well-scoped brownfield) effort going forward, not built once for a single
project.

## 2. Goals

- **G1.** Produce phase files that satisfy the Phase-File Spec (§6), which is derived empirically
  from the Neighboku corpus, for any new project — not just React/TS ones.
- **G2.** Size every phase for one bounded coding-agent session: roughly 150k–200k tokens of total
  session budget (spec + touched files + review/fix cycles) and a file-scope of about 5–10 files
  (hard ceiling ~15, per the empirical range). This is the canonical sizing criterion — see §9 for
  how it's checked in practice, since token cost can't be measured up front.
- **G3.** Support iterative refinement rather than demanding a perfect brief up front. Resolve
  ambiguities through a structured elicitation loop. Where the tool genuinely can't resolve one,
  surface it as an explicit open decision (Neighboku `requirements.md` §8's pattern) instead of
  guessing silently.
- **G4.** Execute a finalized phase sequence against a pluggable coding-agent backend, with the
  same safety rails that experiment validated:
  - a hard file-scope guard
  - bounded retries with escalation to a human
  - merge gates that block on failing build/lint/test

  Backend choice: Claude Code by default, with opencode also supported, since `scripts/orchestrate`
  already proved that backend works.
- **G5.** Stay stack-agnostic. The phase-file format, the elicitation flow, and the execution
  engine must not assume any particular language, framework, or architectural convention (e.g. the
  fractal-component pattern). Those are supplied per target project as an input document.
  Neighboku's own `requirements.md` §7.2 was specific to Neighboku, not to the process that
  produced it.
- **G6.** Reuse the user's existing personal tooling instead of reimplementing it: `gh` for PRs,
  `harness` (dotharness `pr-review`) for AI code review and comment-addressing, and `vibe-heal` for
  SonarQube-style static analysis. Spec Prism Flow orchestrates calls to these, the way
  `scripts/orchestrate` already does for Neighboku, rather than rebuilding review/analysis logic.

## 3. Non-goals

- Not a general-purpose PRD or spec-writing tool for human readers. Its only output contract is
  agent-executable phase files. A human reads them too, but that's secondary.
- Not a static-analysis or code-review engine. It calls `harness`/`vibe-heal` as steps. It does not
  reimplement what they do.
- Not a multi-track comparison tool. Neighboku's baseline-vs-LensFlow lockstep design
  (`implementation-plan.md` §1) was specific to that experiment's purpose. Spec Prism Flow runs a
  single track against a single target repo.
- Not split into separate `plan`/`build` tools behind a shared library. That split was considered
  (§11 #8) and rejected: with the user as the only current consumer of either half, maintaining a
  shared abstraction library across two repos is overkill. This tool doesn't need it yet.
- Does not hardcode any UI/architecture convention. A target project's own conventions (fractal
  components, hexagonal architecture, whatever) are an input the elicitation flow reads and quotes
  into phase files — never a built-in assumption of the tool itself.
- Not a replacement for human review. Manual-test checklists and merge decisions still involve a
  human checkpoint — advisory by default, blocking only with `--strict` (§8, §11 #2).

## 4. Glossary

This glossary covers only terms whose plain-language meaning could be mistaken for something else
in context. Most process-specific vocabulary is defined once, at the point in §7.2 where it's
introduced.

- **Chunk.** A named sub-scope of `requirements.md` that `decompose` (§7.2 step 6) operates on.
  When passed as input to the generator, the same chunk is also called a **seed**. Every chunk
  carries a rough file-scope estimate and a parent path (e.g. `A`, then `A-1`, `A-1-1`, ...). The
  root seed is the whole document. A chunk is either a leaf, or it is split into 2–4 named child
  chunks that become the next seeds in the recursion.
- **Leaf.** A chunk for which running `requirements-doc-drafting-prompt.md`'s elicitation process
  would already produce a requirements doc for a task that is already correctly scoped. Its own
  Feature/component-breakdown section comes back trivial (one feature, nothing left to split), and
  it fits the sizing bands (§9, derived from G2). Reaching a leaf is the unfold's termination
  signal (§7.2 step 6). A leaf becomes exactly one phase file (§6), drafted by `draft-phases`
  (§7.2 step 7). A leaf is always a terminal node of the phase tree, never an internal one.
- **Generator.** The per-chunk agent call made by `decompose` (§7.2 step 6) — not a code generator
  or a language-level generator function — maps a chunk to either a leaf (termination) or a set of
  child chunks (recursion continues). It is defined formally, with its type signature, in §7.2
  step 6.

## 5. The two features, at a glance

| | Feature A — `plan` | Feature B — `build` |
|---|---|---|
| Question it answers | What are we building, broken into agent-sized pieces? | Get the agent to build each piece, safely. |
| Shape | Autonomous CLI/agent loop, with human checkpoint gates between stages | Generalized port of `scripts/orchestrate`'s engine |
| Input | A rough brief, optional existing docs/code, optional conventions doc | The phase corpus `plan` produced (or any corpus meeting the Phase-File Spec) |
| Output | `overview.md`, `requirements.md`, `docs/phases/NN-name-leaf.md` files, an open-decisions log | Merged PRs (or local commits), one per phase, plus a per-phase completion log |

Both live under one CLI, one repo (§10) — `spec-prism-flow plan ...` / `spec-prism-flow build ...`
(§11 #8, resolved).

## 6. Phase-File Spec (the shared contract between A and B)

This spec is distilled empirically from all 20 `docs/phases/*.md` files in the Neighboku corpus.
Every one of them follows it exactly, with zero structural deviation:

- **Exactly five sections, in this order:** `Scope`, `Requirements`, `Acceptance criteria`,
  `Manual test checklist`, `Depends on`.
- **Filename:** `docs/phases/NN-name-leaf.md`, where `NN` is the leaf's flat DFS visit index
  (§7.2's linearization) and `-leaf` is a fixed suffix. Every file `draft-phases` (§7.2 step 7)
  ever writes is definitionally a leaf (§4). The suffix exists to disambiguate a finished phase
  file from any intermediate per-chunk drafts that `decompose`'s generator (§7.2 step 6) may
  persist for tree debugging. It does not distinguish leaves from each other. A chunk's
  hierarchical path (e.g. `A-1-2`) is not encoded in the filename. It can appear in the phase
  file's own body for traceability.
- **`Scope`** is a literal file allowlist (explicit paths or narrow globs like
  `src/components/Foo/__tests__/*.ts`) — the hard boundary a diff may not cross. This is what a
  scope guard enforces mechanically at review time, not a suggestion.
- **`Requirements`** quotes the relevant excerpt(s) of the master `requirements.md` **verbatim**,
  not by reference — the agent should never need to open a second document to know what to build.
  Where a phase's boundary cuts across a requirements section (some of it belongs to an earlier or
  later phase), that carve-out is stated explicitly ("this phase's scope is limited to X. Y belongs
  to Phase N").
- Known ambiguities, discrepancies, or "don't fix this" traps relevant to the phase are quoted
  directly, with an explicit instruction not to resolve them unilaterally.
- **`Acceptance criteria`** is an exhaustive, testable bullet list — including, where relevant,
  the exact file-layout/architecture convention the phase must follow, named precisely (not "follow
  conventions" but the literal hook/file names expected).
- **`Manual test checklist`** maps close to 1:1 onto the acceptance criteria: concrete steps a
  human (or a browser-automation agent) can execute to confirm each one, plus a standing "no console
  errors" check.
- **`Depends on`** is a single linear predecessor ("Phase N−1 merged") — the simplest possible
  contract for a human or a single agent working through the corpus in order, and the default
  rendering of the phase tree (§7.2's DFS order). This stays the phase file's own declared
  dependency even when a richer, real dependency graph exists (see next bullet). A phase file
  should never need to consult a second document to know what unblocks it.
- **A companion dependency graph, `docs/phases/graph.json`,** has one node per leaf and captures
  the *real* edges `decompose` derives from the tree. A leaf depends only on its ancestors' shared
  setup and any leaf whose Requirements/Scope excerpt cross-references it — not necessarily on the
  sibling immediately before it in DFS order. This is what a multi-agent `build` run consults to
  parallelize (§8). A single-agent run can ignore it entirely and just walk the linear `Depends on`
  chain. Graph construction enforces one hard invariant: two leaves with no edge between them must
  have disjoint file scopes. If they'd overlap, they're linked (never left silently racing).
- **Sizing** is governed entirely by §9 (Non-functional requirements, derived from G2). It is not
  restated independently here, so the two cannot diverge.

## 7. Feature A — `plan` (requirements refinement)

### 7.1 Why an autonomous loop, not a live chat skill

The user chose this deliberately. `plan` should behave like `scripts/orchestrate` does for
Feature B: a program with defined stages, state, and checkpoints, runnable unattended between
checkpoints. It should not be a slash-command that only works inside a live, synchronous Claude
Code conversation. This makes it resumable, scriptable, and consistent with how the user already
drives `orchestrate` and `harness`.

### 7.2 v1 elicitation protocol (expected to iterate — this is a first design, not a locked spec)

The protocol is a sequence of CLI subcommands, each an agent run against durable files on disk,
with a human checkpoint between every stage. Nothing auto-advances past a stage without an explicit
go-ahead.

**Human ↔ agent-harness hand-off (§11 #7, resolved for v1):** every stage below that needs an
actual agent run follows the same protocol. The coding-agent harness (Claude Code or opencode) is
an interactive CLI that the tool can't call as a library. `spec-prism-flow` prints the prompt
file's path and the expected output file's path to the console. It copies the prompt file's path
to both the system clipboard and the current tmux paste buffer, covering both local/GUI clipboard
use and SSH/tmux-only sessions. Then it waits. The human starts the coding-agent harness and
pastes the prompt file path into it. The harness reads the prompt and begins. The agent writes its
result to the specified output file. `spec-prism-flow` reads it from there once the human signals
the stage is done. This protocol is expected to iterate if it proves too manual in practice.

1. **`spec-prism-flow plan init`** — it takes a rough brief (a text/markdown file the human wrote)
   and optionally any of these:
   - a path to existing code (brownfield context)
   - a path to a conventions document (architecture/stack rules to carry into every phase —
     Neighboku's `fractal_component.md` equivalent)
   - reference links

   It sets up the project's planning workspace at the location the project's config (§10)
   declares. It never hardcodes a `docs/` tree it assumes it can create (§11 #4, resolved). It
   matches `pr-review`'s `.harness.toml`-style per-project config, since a project the user
   contributes to but doesn't control the layout of may not tolerate an auto-scaffolded directory.
2. **`spec-prism-flow plan draft-overview`** — it is an agent run. It reads the brief (+ optional
   context) and drafts `00-overview.md` (problem statement, goals, non-goals, glossary, key
   decisions already implied by the brief), modeled on Neighboku's own `00-overview.md`. Anything
   it can't resolve from the brief alone is written to `OPEN_QUESTIONS.md` as a concrete,
   answerable question — not left implicit in the draft.
3. **Human checkpoint** — edit `OPEN_QUESTIONS.md` with answers (or edit the draft directly), then
   signal ready.
4. **`spec-prism-flow plan draft-requirements`** — it expands the approved overview and answered
   questions into a full `requirements.md`. The sections:
   - functional behavior, section by section
   - the glossary
   - any architecture/convention constraints pulled in from the supplied conventions doc
   - non-functional requirements
   - an explicit out-of-scope section

   It uses the same verbatim-quoting discipline the phase files will later need: this is the
   document later phases excerpt from. This step is one full run of
   `requirements-doc-drafting-prompt.md`'s elicitation process against the approved overview. That
   process has its own required structure and properties: verbatim-quotable, ID'd, ambiguity
   surfaced never guessed, open-decisions log. `decompose` (step 6) reapplies the same process
   recursively, one chunk at a time.
5. **Human checkpoint** — review/edit `requirements.md`.
6. **`spec-prism-flow plan decompose`** — it recursively unfolds the requirements doc into a phase
   tree. It then linearizes the tree via depth-first traversal into the ordered phase list. The
   unfolding replaces a single flat splitting pass with an `unfoldr`-style corecursion: `unfoldr ::
   (chunk -> Leaf | [chunk]) -> chunk -> Tree`, driven depth-first.
   - **Seed:** a chunk (§4) — a named sub-scope of `requirements.md` plus a rough file-scope
     estimate and its parent path (e.g. `A`, then `A-1`, `A-1-1`, ...). The root seed is the whole
     document.
   - **Generator (one agent call per chunk):** it runs `requirements-doc-drafting-prompt.md`'s
     full process against the chunk, exactly as `draft-requirements` (step 4) ran it against the
     whole brief. The process is the full version (§9), deliberately not an abridged one. Only the
     chunk's own slice of the parent `requirements.md` is passed as the "rough brief" input —
     deliberately not the rest of the parent document. This choice favors a self-contained
     sub-draft over one padded with unrelated context. The cost: an extra iteration when the
     sub-draft surfaces a gap that fuller context would have already answered (§9). A chunk is a
     **leaf** (§4) exactly when this run produces a requirements doc for a task that is already
     correctly scoped. Then its own Feature/component-breakdown section (that prompt's §6) comes
     back empty or trivial — one feature, nothing left to split — and it fits the sizing bands
     (§9). That's the **termination signal**. The mini-doc this run produced is kept and feeds
     directly into `draft-phases` (step 7). No requirements are re-derived at leaf time. If
     instead the run's own Feature/component breakdown is non-trivial (more than one feature), the
     generator returns one child chunk per breakdown entry. Each becomes a new seed the traversal
     recurses into next.
   - **Trivial-breakdown deadlock:** the generator can judge a chunk "one feature" — nothing to
     split into — while that chunk still fails the sizing bands (§9). This is an accepted risk,
     not a solved problem (§9). When it happens, `decompose` re-runs the generator on that same
     chunk with an amended prompt. That prompt appends an explicit instruction: "you already
     judged this trivial once; force a split anyway." Re-running the unaltered prompt against
     unchanged input would likely reproduce the same verdict. The goal is to force a new leaf:
     `decompose` splices it into the DFS order immediately before whatever chunk would otherwise
     follow, rather than halting the run. This forced-split retry is capped at one attempt per
     chunk, matching the **Termination safeguard** two bullets below. If the retry still comes
     back trivial, the chunk is flagged for human review rather than retried indefinitely. Each
     occurrence (retry or escalation) is logged (§9, §11 #6). v1 does no automatic tuning of the
     generator prompt or sizing bands.
   - **Depth-first, asymmetric by design:** each branch terminates independently — `A-2` can end
     as a single leaf while `A-1` recurses three levels deep (`A-1-1-1`, `A-1-1-2`, ...) and
     `A-3` ends with two. The stopping condition is "does this chunk satisfy the sizing bands,"
     evaluated per branch. It is never a fixed depth for the whole tree.
   - **Termination safeguard:** the safeguard is a hard recursion-depth cap (default 4,
     configurable). A chunk still over-scoped at the cap is flagged for human review rather than
     split indefinitely. This matches G4's bounded-retry/escalation philosophy rather than
     trusting the generator to always converge.
   - **Linearization:** DFS visit order fixes the phase corpus's sequential numbering and supplies
     the linear `Depends on` chain from §6. A leaf's predecessor is whichever leaf's DFS visit
     immediately preceded it, regardless of which branch either came from. This is the easy,
     always-available answer to "what do I implement next" for a single agent.
   - **Graph derivation:** alongside the linear order, `decompose` derives `graph.json` (§6). For
     each pair of leaves, an edge exists if one's Requirements/Scope excerpt references the other,
     or they share a mutable file. Otherwise, there is no edge. Leaves with no path between them
     in this graph are safe to implement concurrently. This is what lets `build` spread work
     across multiple agents without abandoning the simple linear contract that single-agent runs
     rely on.
   - **Human checkpoint:** the human reviews the whole tree — not a flat list — before any leaf's
     full phase file is drafted. This is still the cheapest point to fix a bad decomposition
     (reorder, force a merge, force a further split), before any prose exists around it. Edits
     made here are trusted as-is (§11's entry on the human-agent interaction mechanism covers how
     this checkpoint is operated).
7. **`spec-prism-flow plan draft-phases`** — it walks the approved tree in DFS order. For each
   **leaf only**, it repackages the mini-requirements doc that `decompose`'s generator already
   produced for it (step 6) into the Phase-File Spec's five-section format (§6). The mapping:
   - the doc's detailed functional requirements become the `Requirements` section verbatim
   - its file-scope estimate becomes `Scope`
   - its testable requirements drive `Acceptance criteria`/`Manual test checklist`

   Internal tree nodes (`A`, `A-1`, `A-3` in the example above) are decomposition scaffolding,
   not phase files themselves. Their sub-scope names may seed a leaf's title, but nothing is
   drafted for them. It continues self-checking each leaf's word count and file-scope count
   against the sizing bands (§9). It flags any outlier for human review instead of shipping it
   silently.
8. **`spec-prism-flow plan review`** — it runs a final consistency pass over the whole corpus. It
   checks:
   - Does the dependency chain form a valid order?
   - Does `graph.json` agree with it (every linear-chain edge appears in the graph, and the graph
     is acyclic)?
   - Does every `requirements.md` section map to at least one phase?
   - Are there stale entries in `OPEN_QUESTIONS.md` that were never resolved?

### 7.3 What "done" looks like

The elicitation loop is done when `plan review` passes and a human has approved the phase corpus —
not when the agent stops asking questions. An unresolved, flagged question is a valid terminal
state for a given run (recorded in the open-decisions log), and it is never silently dropped.

## 8. Feature B — `build` (phase execution)

This feature generalizes `scripts/orchestrate`. It keeps its proven core and drops what was
specific to the Neighboku two-track experiment.

**Keep, largely as-is:**
- `phase_file.py`'s parser for the Phase-File Spec (§6) format.
- `scope_guard.py` — a diff touching a file outside the phase's declared `Scope` is a blocking
  finding.
- `merge_gates.py`'s hard gate (no merge on a failing build/lint/test) — generalized to run
  whatever command list the target project's config declares, instead of a hardcoded `pnpm
  build`/`pnpm lint`/`pnpm test`.
- The bounded-retry/escalation state machine (cap `address-comments`-style cycles per phase, stop
  and escalate to a human rather than loop indefinitely).
- `track_runner.py`'s "loop phases in declared order, skip already-merged ones, halt on the first
  escalation" behavior — kept as-is as `build`'s **sequential mode** (default, one agent worker).
  It walks the DFS-linearized `Depends on` chain from §6/§7.2, one phase at a time.

**Drop:**
- `lockstep_runner.py` and every two-track/baseline-vs-variant comparison concept — Neighboku-only.
- `metrics.py`'s Sonar-issue-delta-between-tracks comparison fields (a per-phase completion log is
  still useful. The cross-track comparison isn't.)

**Generalize:**
- `opencode_runner.py` becomes one implementation behind a small "agent runner" interface. The
  interface is an abstraction over one operation: "run the coding agent against this phase file, in
  this branch, and commit the result." Claude Code is the default implementation. opencode stays
  supported, since it's already proven to work here.
- The static-analysis and review steps (Neighboku's `vibe_heal_runner.py`/`harness_runner.py`)
  become optional, config-driven calls into the user's existing `harness` (pr-review) and
  `vibe-heal` CLIs. They are present and active when a target project has them configured. They
  are a clean no-op when it doesn't (most brand-new projects won't, on day one).
- New: `track_runner.py` gains a **parallel mode**, consulted only when the target project's config
  sets a worker count > 1 (default 1, which degenerates to sequential mode). It reads `graph.json`
  (§6/§7.2) instead of the linear chain. A leaf becomes eligible once every leaf with an edge into
  it has merged. Up to N eligible leaves run concurrently, each against its own agent-runner
  instance, branch, and PR. `scope_guard`'s disjoint-file-scope invariant (enforced at
  graph-construction time in `decompose`) makes running them concurrently safe. Merge gates and
  bounded-retry/escalation apply per-leaf exactly as in sequential mode. An escalation on one leaf
  halts only the leaves that depend on it, not the whole run. **Branch/stack organization and
  merge order are left to the human (§11 #5, resolved).** This is expected to be rare enough to
  not warrant automation. Dedicated stacked-branch tooling (e.g. git-spice) serves it better than
  `spec-prism-flow` reinventing the logic. The tool's own responsibility ends at surfacing the
  order (the linear DFS chain and `graph.json`, §6) a human uses to organize that stack.

**The workflow per phase (either mode):**
1. Implement.
2. Push + open PR (mandatory for v1 — a local-only, no-push/PR mode was considered and deferred to
   a future phase, §11 #3).
3. Static analysis (optional).
4. AI review (optional).
5. Address comments (bounded retries).
6. Manual test checklist (human-executed. The tool displays the phase file's checklist and records
   pass/fail. **Advisory by default — logged but non-blocking — with a `--strict` flag making it a
   hard merge gate, §11 #2**).
7. Merge.

In parallel mode this pipeline runs once per concurrently-eligible leaf.

## 9. Non-functional requirements

- **Sizing bands (single source of truth, derived from G2).** A leaf, and the phase file drafted
  from it, must fit: **file-scope 5–10 files** (hard ceiling ~15) and **roughly 150k–200k tokens**
  of total coding-agent session budget (spec + touched files + review/fix cycles) — both per G2.
  The Neighboku corpus additionally observed a **500–1500 word** doc-length band (full range
  532–2100) for the phase files these numbers were derived from. That band is a proxy for the
  token-budget figure, not an independent target. G1's Phase-File Spec (§6) and every sizing check
  in §7.2 cite this section rather than restate the numbers.
- **Sizing checks are best-effort, not measured.** There is no way to count the tokens a future
  coding-agent session will spend before that session runs. `decompose`'s generator (§7.2 step 6)
  and `draft-phases`'s self-check (§7.2 step 7) evaluate a chunk or leaf against the *proxies*
  available at draft time: file-scope count and doc length. These act as a best-effort stand-in
  for G2's real token-budget criterion. This is a deliberate, accepted approximation.
- **Trivial-breakdown deadlock is an accepted risk, not a solved problem.** The generator's
  Feature/component-breakdown judgment (one feature vs. several) and the sizing bands above are
  different axes and can disagree — a chunk can be genuinely "one feature" and still oversized.
  §7.2 step 6 describes the mitigation: one prompt-amended retry to force a split, escalating to
  human review on a second trivial verdict rather than retrying indefinitely. Each occurrence
  (retry or escalation) is logged (§11 #6, resolved). v1 has no automatic feedback loop into the
  generator prompt or the sizing bands. The log is for a human to skim later, if the pattern
  proves common enough to warrant tuning either one.
- **Recursive elicitation favors self-containment over inherited context.** Every `decompose`
  generator call (§7.2 step 6) is deliberately scoped to only the chunk's own requirements slice,
  never the full parent document. The trade: a possible extra clarification iteration, in exchange
  for a leaf whose `Requirements` section never needs a second document to make sense of it. This
  matches the Phase-File Spec's (§6) verbatim-quoting discipline.
- **No abridged elicitation pass at chunk level.** Every `decompose` generator call runs
  `requirements-doc-drafting-prompt.md`'s full structure. An abridged variant was considered and
  rejected: producing an abridged version would itself cost an LLM pass, cancelling out whatever
  the shorter pass would have saved.
- **Scope-guard invariant (§6, §8).** A diff may never touch a file outside its phase's declared
  `Scope`. This is enforced mechanically, not advisory. In parallel mode (§8) this extends to the
  dependency graph itself: any two leaves without an edge between them must have disjoint file
  scopes.
- **Merge gate (G4, §8).** No merge on a failing build/lint/test command, whatever command list
  the target project's config (§10) declares.
- **Bounded retries (G4, §8).** `address-comments`-style cycles per phase are capped. On
  exhaustion, escalate to a human rather than loop indefinitely.

## 10. Tool shape

- **Location:** `~/.harness/tools/spec-prism-flow/`, a new `uv`-managed Python CLI.
- **Bootstrap:** `~/.cookiecutters/cookiecutter-uv`, replaying the same option set already used for
  `pr-review` and `refactor-hotspots`: `layout=flat`, `include_github_actions=y`,
  `publish_to_pypi=n`, `deptry=y`, `docs_tool=zensical`, `codecov=y`, `dockerfile=n`,
  `devcontainer=n`, `type_checker=ty`, `open_source_license=Apache Software License 2.0`,
  `author=Alexei Diaz`, `email=alexei.eleusis@gmail.com`, `author_github_handle=alexeieleusis`,
  output dir `~/.harness/tools`.
- **CLI shape:** one CLI, two command groups mirroring the two features (§11 #8, resolved as one
  unified tool rather than a split) — `spec-prism-flow plan <subcommand>` (§7.2's stages) and
  `spec-prism-flow build <subcommand>` (mirroring `orchestrate run-phase`/`run-track`/`status`).
  The CLI is `click`-based, matching `pr-review`/`refactor-hotspots`'s house style.
  `scripts/orchestrate` used `typer`+`rich`. House style has since settled on `click`.
- **Config:** a per-target-project TOML (analogous to `.harness.toml`) declaring:
  - the agent backend (Claude Code default, or opencode)
  - whether `harness`/`vibe-heal` integration is active, and how to reach it
  - the path to the project's conventions document
  - the planning workspace location (where `plan init` writes `00-overview.md`/`requirements.md`/
    `OPEN_QUESTIONS.md`. Distinct from the phase directory location below)
  - the phase directory location
  - the build/lint/test command list `merge_gates` should run
  - `build`'s worker count (default 1, which is sequential. A count above 1 switches `track_runner`
    into parallel mode, §8)

## 11. Decisions log

These decisions are flagged explicitly, per the same discipline Neighboku's `requirements.md` §8
used. They were resolved by a human before or during Spec Prism Flow's own implementation, rather
than decided unilaterally by an agent building it:

1. ~~**Tool name.**~~ **Resolved:** the tool is named **Spec Prism Flow**. The package/CLI name is
   `spec-prism-flow`. The earlier working names — "phaseforge," then briefly "Spec Prism" — were
   discarded during drafting. They are recorded here only as history.
2. ~~**Manual-test-checklist gate strength.**~~ **Resolved:** the checklist gate is advisory by
   default (the agent proceeds, and the checklist is logged but not enforced), with a `--strict`
   flag switching it to a hard, blocking merge gate (§3, §8).
3. ~~**Git/PR operations: mandatory or optional?**~~ **Resolved:** mandatory for v1. Every phase
   opens a PR via `gh pr create`, matching `scripts/orchestrate`'s assumption, so the
   review/static-analysis steps always have something to attach to (§8). A "local-only" mode for
   solo work without a remote was considered and deliberately deferred to a future phase, not
   built now — the v1 scope is already large enough.
4. ~~**Scaffolding a target project's `docs/` tree.**~~ **Resolved:** the layout is
   config-driven, not auto-scaffolded (§7.2 step 1, §10). This mirrors `pr-review`'s
   `.harness.toml` pattern. `plan init` never assumes it can create a `docs/` tree, since the user
   may be contributing to a project whose layout they don't control.
5. ~~**Parallel-mode git/branching strategy.**~~ **Resolved:** left entirely to the human (§8).
   `spec-prism-flow` limits itself to surfacing task order (the linear DFS chain and `graph.json`,
   §6). Branch stacking and merge-order decisions across concurrently-eligible leaves are
   expected to be rare enough to hand off to dedicated tooling (e.g. git-spice), rather than
   building and maintaining that logic in-house.
6. ~~**Feedback loop for trivial-breakdown deadlocks (§9).**~~ **Resolved:** just log it (§9). v1
   has no automatic adjustment of the generator prompt or sizing bands.
7. ~~**Human-agent-harness interaction mechanism.**~~ **Resolved for v1** (§7.2): the tool prints
   the prompt/output file paths to the console. It copies the prompt file's path to both the
   system clipboard and the current tmux paste buffer. Then it waits for the human to start the
   coding-agent harness and paste the path in. This design is explicitly expected to iterate if it
   proves too manual in practice.
8. ~~**Does `build` (Feature B) stay part of the tool?**~~ **Resolved: yes, unified.** Both
   features ship as one tool, one CLI, one repo — `spec-prism-flow plan` and `spec-prism-flow
   build` (§5, §10). The alternative was considered and rejected: split into a `spec-prism`
   frontend and a `spec-flow` backend behind a shared library, so either half could be swapped
   independently. With the user as the only current consumer of either half, the shared-library
   overhead a clean split would need is overkill (§3). The name's "before a single line of
   application code is generated" framing (§0) describes `plan`'s rigor and its motivation, not a
   scope boundary excluding `build`.

## 12. Next step

Once this draft is reviewed, with all of §11's open decisions now resolved:
1. Scaffold `~/.harness/tools/spec-prism-flow/` via the cookiecutter-uv recipe in §10.
2. Move this document to `spec-prism-flow/docs/requirements.md`.
3. Write Spec Prism Flow's own `docs/phases/NN-name-leaf.md` files for building Feature A and
   Feature B — dogfooding the very spec this document defines.
4. Run them through `scripts/orchestrate`'s existing engine (or an early hand-run of `build`
   itself, once it exists) to implement Spec Prism Flow.
