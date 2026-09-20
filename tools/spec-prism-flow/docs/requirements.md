# Spec Prism Flow — Vision & Requirements (draft)

> The tool was drafted under the working name "phaseforge", briefly considered "Spec Prism," and is
> now named **Spec Prism Flow** (§11's open decisions #1 and #8 are resolved — see §0).

## 0. Purpose / origin

**Spec Prism Flow** is an engine for Specification-Driven Development for autonomous agents, run
end to end: from a high-level idea to merged code. Its `plan` half (Feature A, §7) turns a
high-level, singular system prompt or product goal into exhaustive, deeply validated, deterministic
technical requirements — framing requirement generation as an algebraic data synthesis task, the
`unfoldr`-style corecursion that drives `decompose` (§7.2 step 6) — so a feature space gets explored
and edge cases get mapped *before* a single line of application code is generated. That phrasing
names what `plan`'s rigor is trying to prevent — rework, or an agent stalling on ambiguity in the
middle of an implementation — not a claim that ambiguity is eliminated (impossible; G3 already
commits to surfacing what can't be resolved rather than pretending otherwise) or that the tool's
scope stops short of execution. This is risk mitigation, not risk removal. Its `build` half
(Feature B, §8) is what actually executes the resulting phase corpus — the same tool, carried
through to the code it planned for (§11 #8).

This tool generalizes a pattern first proven in the [`agentic-neighboku-lensflow`](https://github.com/alexeieleusis/agentic-neighboku-lensflow)
repo's [`docs/neighboku-ai-rebuild/`](https://github.com/alexeieleusis/agentic-neighboku-lensflow/tree/main/docs/neighboku-ai-rebuild/)
planning docs and [`docs/phases/`](https://github.com/alexeieleusis/agentic-neighboku-lensflow/tree/main/docs/phases/)
corpus (20 files) plus the `scripts/orchestrate/` engine that drives them. That experiment rebuilt
an existing game from scratch via ~20 small, independently agent-implementable phases, and it
worked — each phase was self-contained enough that a coding agent could implement it, get
reviewed, and merge it in one session. But it only got there by reverse-engineering requirements
from a **finished reference implementation**: full source, full git history, in-app docs, and a
narrated walkthrough video. Spec Prism Flow is what's left when that reference doesn't exist — the
same two capabilities (turn requirements into agent-sized phases; execute those phases safely),
built for the normal case of starting from a rough idea instead of a finished product.

## 1. Problem statement

A coding agent (Claude Code, opencode) does its best work against a small, unambiguous,
self-contained work order: a fixed file scope, requirements quoted in full rather than referenced,
one architectural convention stated explicitly, and testable acceptance criteria. Handing an agent
a full PRD, or a vague one-line feature request, produces worse results than handing it a phase
file with these properties — scope creep, architectural drift across features, and requirements
the agent has to guess at.

The Neighboku rebuild got such phase files "for free": every ambiguity had a ground-truth answer
sitting in the original code, and the requirements doc's job was just to transcribe and organize
what was already there, flagging the handful of real discrepancies for human judgment. Most real
projects — including whatever the user builds next — start with no such reference. The requirements
exist only as an idea, a rough brief, maybe a competitor to point at. Getting from that starting
point to a phase corpus with the same tight, low-ambiguity properties needs a *process*, not just a
transcription pass — and that process needs to be repeatable, because this tool is meant to be
reached for on every new greenfield (or well-scoped brownfield) effort going forward, not built
once for a single project.

## 2. Goals

- **G1.** Produce phase files that satisfy the Phase-File Spec (§6), derived empirically from the
  Neighboku corpus, for any new project — not just React/TS ones.
- **G2.** Size every phase for one bounded coding-agent session: roughly 150k–200k tokens of total
  session budget (spec + touched files + review/fix cycles) and a file-scope of about 5–10 files
  (hard ceiling ~15, per the empirical range). This is the canonical sizing criterion — see §9 for
  how it's actually checked in practice, since token cost can't be measured up front.
- **G3.** Support iterative refinement rather than demanding a perfect brief up front — resolve
  ambiguities through a structured elicitation loop, and where the tool genuinely can't resolve
  one, surface it as an explicit open decision (Neighboku requirements.md §8's pattern) instead of
  guessing silently.
- **G4.** Execute a finalized phase sequence against a pluggable coding-agent backend (Claude Code
  by default; opencode also supported, since `scripts/orchestrate` already proved that backend
  works) with the same safety rails that experiment validated: a hard file-scope guard, bounded
  retries with escalation to a human, and merge gates that block on failing build/lint/test.
- **G5.** Stay stack-agnostic. The phase-file format, the elicitation flow, and the execution engine
  must not assume any particular language, framework, or architectural convention (e.g. the
  fractal-component pattern) — those are supplied per target project as an input document, the way
  `requirements.md` §7.2 was specific to Neighboku, not to the process that produced it.
- **G6.** Reuse the user's existing personal tooling instead of reimplementing it: `gh` for PRs,
  `harness` (dotharness `pr-review`) for AI code review and comment-addressing, `vibe-heal` for
  SonarQube-style static analysis — Spec Prism Flow orchestrates calls to these, the way
  `scripts/orchestrate` already does for Neighboku, rather than rebuilding review/analysis logic.

## 3. Non-goals

- Not a general-purpose PRD or spec-writing tool for human readers. Its only output contract is
  agent-executable phase files; a human reads them too, but that's secondary.
- Not a static-analysis or code-review engine. It calls `harness`/`vibe-heal` as steps; it does not
  reimplement what they do.
- Not a multi-track comparison tool. Neighboku's baseline-vs-LensFlow lockstep design
  (`implementation-plan.md` §1) was specific to that experiment's purpose; Spec Prism Flow runs a
  single track against a single target repo.
- Not split into separate `plan`/`build` tools behind a shared library. That split was considered
  (§11 #8) and rejected: with the user as the only current consumer of either half, maintaining a
  shared abstraction library across two repos is overkill this tool doesn't need yet.
- Does not hardcode any UI/architecture convention. A target project's own conventions (fractal
  components, hexagonal architecture, whatever) are an input the elicitation flow reads and quotes
  into phase files — never a built-in assumption of the tool itself.
- Not a replacement for human review. Manual-test checklists and merge decisions still involve a
  human checkpoint — advisory by default, blocking only with `--strict` (§8, §11 #2).

## 4. Glossary

- **Chunk.** A named sub-scope of `requirements.md` — the seed `decompose` (§7.2 step 6) operates
  on. Every chunk carries a rough file-scope estimate and a parent path (e.g. `A`, then `A-1`,
  `A-1-1`, ...); the root seed is the whole document. A chunk is either a leaf, or it gets split
  into 2–4 named child chunks that become the next seeds in the recursion.
- **Leaf.** A chunk for which running `requirements-doc-drafting-prompt.md`'s elicitation process
  would already produce a requirements doc for an already-correctly-scoped task — its own
  Feature/component-breakdown section comes back trivial (one feature, nothing left to split) and
  it fits the sizing bands (§9, derived from G2). Reaching a leaf is the unfold's termination signal
  (§7.2 step 6); a leaf becomes exactly one phase file (§6), drafted by `draft-phases` (§7.2 step 7).
  A leaf is always a terminal node of the phase tree, never an internal one.

## 5. The two features, at a glance

| | Feature A — `plan` | Feature B — `build` |
|---|---|---|
| Question it answers | What are we building, broken into agent-sized pieces? | Get the agent to actually build each piece, safely. |
| Shape | Autonomous CLI/agent loop, staged with human checkpoint gates between stages | Generalized port of `scripts/orchestrate`'s engine |
| Input | A rough brief, optional existing docs/code, optional conventions doc | The phase corpus `plan` produced (or any corpus meeting the Phase-File Spec) |
| Output | `overview.md`, `requirements.md`, `docs/phases/NN-name-leaf.md` files, an open-decisions log | Merged PRs (or local commits), one per phase, plus a per-phase completion log |

Both live under one CLI, one repo (§10) — `spec-prism-flow plan ...` / `spec-prism-flow build ...`
(§11 #8, resolved).

## 6. Phase-File Spec (the shared contract between A and B)

Empirically distilled from all 20 `docs/phases/*.md` files in the Neighboku corpus — every one of
them follows this exactly, with zero structural deviation:

- **Exactly five sections, in this order:** `Scope`, `Requirements`, `Acceptance criteria`,
  `Manual test checklist`, `Depends on`.
- **Filename:** `docs/phases/NN-name-leaf.md`, where `NN` is the leaf's flat DFS visit index
  (§7.2's linearization) and `-leaf` is a fixed suffix. Every file `draft-phases` (§7.2 step 7) ever
  writes is definitionally a leaf (§4) — the suffix exists to disambiguate a finished phase file
  from any intermediate per-chunk drafts `decompose`'s generator (§7.2 step 6) may persist for tree
  debugging, not to distinguish leaves from each other. A chunk's hierarchical path (e.g. `A-1-2`)
  is not encoded in the filename; it can appear in the phase file's own body for traceability.
- **`Scope`** is a literal file allowlist (explicit paths or narrow globs like
  `src/components/Foo/__tests__/*.ts`) — the hard boundary a diff may not cross. This is what a
  scope guard enforces mechanically at review time, not a suggestion.
- **`Requirements`** quotes the relevant excerpt(s) of the master `requirements.md` **verbatim**,
  not by reference — the agent should never need to open a second document to know what to build.
  Where a phase's boundary cuts across a requirements section (some of it belongs to an earlier or
  later phase), that carve-out is stated explicitly ("this phase's scope is limited to X; Y belongs
  to Phase N").
- Known ambiguities, discrepancies, or "don't fix this" traps relevant to the phase are quoted in
  directly, with an explicit instruction not to unilaterally resolve them.
- **`Acceptance criteria`** is an exhaustive, testable bullet list — including, where relevant, the
  exact file-layout/architecture convention the phase must follow, named precisely (not "follow
  conventions" but the literal hook/file names expected).
- **`Manual test checklist`** maps close to 1:1 onto the acceptance criteria: concrete steps a
  human (or a browser-automation agent) can execute to confirm each one, plus a standing "no console
  errors" check.
- **`Depends on`** is a single linear predecessor ("Phase N−1 merged") — the simplest possible
  contract for a human or a single agent working through the corpus in order, and the default
  rendering of the phase tree (§7.2's DFS order). This stays the phase file's own declared
  dependency even when a richer, real dependency graph exists (see next bullet) — a phase file
  should never need to consult a second document to know what unblocks it.
- **A companion dependency graph, `docs/phases/graph.json`**, one node per leaf, captures the *real*
  edges `decompose` derives from the tree: a leaf truly depends only on its ancestors' shared setup
  and any leaf whose Requirements/Scope excerpt cross-references it — not necessarily on the sibling
  immediately before it in DFS order. This is what a multi-agent `build` run consults to parallelize
  (§8); a single-agent run can ignore it entirely and just walk the linear `Depends on` chain.
  Graph construction enforces one hard invariant: two leaves with no edge between them must have
  disjoint file scopes — if they'd overlap, they're linked (never left silently racing).
- **Sizing** is governed entirely by §9 (Non-functional requirements, derived from G2) — not
  restated independently here, so the two can never drift out of sync.

## 7. Feature A — `plan` (requirements refinement)

### 7.1 Why an autonomous loop, not a live chat skill

The user chose this deliberately: `plan` should behave like `scripts/orchestrate` does for
Feature B — a program with defined stages, state, and checkpoints, runnable unattended between
checkpoints — rather than a slash-command that only works inside a live, synchronous Claude Code
conversation. This makes it resumable, scriptable, and consistent with how the user already drives
`orchestrate` and `harness`.

### 7.2 v1 elicitation protocol (expected to iterate — this is a first design, not a locked spec)

A sequence of CLI subcommands, each an agent run against durable files on disk, with a human
checkpoint between every stage (nothing auto-advances past a stage without an explicit go-ahead).

**Human ↔ agent-harness hand-off (§11 #7, resolved for v1):** every stage below that needs an
actual agent run follows the same protocol, since the coding-agent harness (Claude Code or
opencode) is an interactive CLI the tool can't call as a library. `spec-prism-flow` prints the
prompt file's path and the expected output file's path to the console, copies the prompt file's
path to both the system clipboard and the current tmux paste buffer (covering local/GUI clipboard
use and SSH/tmux-only sessions in one shot), then waits. The human starts the coding-agent harness
themselves, pastes in the prompt file path to have it read the prompt and begin, and the agent
writes its result to the specified output file; `spec-prism-flow` picks it up there once the human
signals the stage is done. Expected to iterate if this proves too manual in practice.

1. **`spec-prism-flow plan init`** — takes a rough brief (a text/markdown file the human wrote), and
   optionally: a path to existing code (brownfield context), a path to a conventions document
   (architecture/stack rules to carry into every phase, Neighboku's `fractal_component.md`
   equivalent), and reference links. Sets up the project's planning workspace at whatever location
   the project's config (§10) declares — never hardcoded to a `docs/` tree it assumes it can create
   (§11 #4, resolved): matches `pr-review`'s `.harness.toml`-style per-project config, since a
   project the user contributes to but doesn't control the layout of may not tolerate an
   auto-scaffolded directory.
2. **`spec-prism-flow plan draft-overview`** — an agent run that reads the brief (+ optional
   context) and drafts `00-overview.md` (problem statement, goals, non-goals, glossary, key
   decisions already implied by the brief) modeled on Neighboku's own `00-overview.md`. Anything it
   can't resolve from the brief alone is written to `OPEN_QUESTIONS.md` as a concrete, answerable
   question — not left implicit in the draft.
3. **Human checkpoint** — edit `OPEN_QUESTIONS.md` with answers (or edit the draft directly), then
   signal ready.
4. **`spec-prism-flow plan draft-requirements`** — expands the approved overview + answered
   questions into a full `requirements.md`: functional behavior section by section, the glossary,
   any architecture/convention constraints pulled in from the supplied conventions doc,
   non-functional requirements, and an explicit out-of-scope section. Same verbatim-quoting
   discipline the phase files will later need — this is the document later phases excerpt from.
   Concretely, this step is one full run of `requirements-doc-drafting-prompt.md`'s elicitation
   process (its own required structure and properties: verbatim-quotable, ID'd, ambiguity surfaced
   never guessed, open-decisions log) against the approved overview — the same process `decompose`
   (step 6) reapplies recursively, one chunk at a time.
5. **Human checkpoint** — review/edit `requirements.md`.
6. **`spec-prism-flow plan decompose`** — recursively unfolds the requirements doc into a phase
   tree, then linearizes that tree via depth-first traversal into the ordered phase list. This
   replaces a single flat splitting pass with an `unfoldr`-style corecursion: `unfoldr :: (chunk ->
   Leaf | [chunk]) -> chunk -> Tree`, driven depth-first.
   - **Seed:** a chunk (§4) — a named sub-scope of `requirements.md` plus a rough file estimate and
     its parent path (e.g. `A`, then `A-1`, `A-1-1`, ...). The root seed is the whole document.
   - **Generator (one agent call per chunk):** runs `requirements-doc-drafting-prompt.md`'s full
     process (§9 — deliberately not an abridged version) against the chunk, exactly as
     `draft-requirements` (step 4) ran it against the whole brief. Only the chunk's own slice of the
     parent `requirements.md` is passed as the "rough brief" input — deliberately not the rest of
     the parent document. This favors a self-contained sub-draft over one padded with unrelated
     context, even at the cost of an extra iteration when the sub-draft surfaces a gap that fuller
     context would have already answered (§9). A chunk is a **leaf** (§4) exactly when this run
     produces a requirements doc for an already-correctly-scoped task: its own Feature/component-
     breakdown section (that prompt's §6) comes back empty or trivial — one feature, nothing left to
     split — and it fits the sizing bands (§9). That's the **termination signal**; the mini-doc this
     run produced is kept and feeds directly into `draft-phases` (step 7) — no requirements are
     re-derived at leaf time. If instead the run's own Feature/component breakdown is non-trivial
     (more than one feature), the generator returns one child chunk per breakdown entry, each
     becoming a new seed the traversal recurses into next.
   - **Trivial-breakdown deadlock:** the generator can judge a chunk "one feature" — nothing to
     split into — while that chunk still fails the sizing bands (§9). This is an accepted risk, not
     a solved problem (§9): when it happens, `decompose` re-runs the generator on that same chunk
     with an amended prompt — appending an explicit "you already judged this trivial once; force a
     split anyway" instruction, since re-running the unaltered prompt against unchanged input would
     likely reproduce the same verdict — to force a new leaf, splicing it into the DFS order
     immediately before whatever chunk would otherwise follow, rather than halting the run. This
     forced-split retry is capped at one attempt per chunk, matching the **Termination safeguard**
     two bullets below: if the retry still comes back trivial, the chunk is flagged for human review
     rather than retried indefinitely. Each occurrence (retry or escalation) is logged (§9, §11 #6) —
     no automatic tuning of the generator prompt or sizing bands in v1.
   - **Depth-first, asymmetric by design:** each branch bottoms out independently — `A-2` can
     terminate as a single leaf while `A-1` recurses three levels deep (`A-1-1-1`, `A-1-1-2`, ...)
     and `A-3` bottoms out at two. The stopping condition is purely "does this chunk satisfy the
     sizing bands," evaluated per branch, never a fixed depth for the whole tree.
   - **Termination safeguard:** a hard recursion-depth cap (default 4; configurable) — a chunk still
     over-scoped at the cap is flagged for human review rather than split indefinitely, matching G4's
     bounded-retry/escalation philosophy rather than trusting the generator to always converge.
   - **Linearization:** DFS visit order fixes the phase corpus's sequential numbering and supplies
     the linear `Depends on` chain from §6 — a leaf's predecessor is whichever leaf's DFS visit
     immediately preceded it, regardless of which branch either came from. This is the easy,
     always-available answer to "what do I implement next" for a single agent.
   - **Graph derivation:** alongside the linear order, `decompose` derives `graph.json` (§6) — for
     each pair of leaves, an edge if one's Requirements/Scope excerpt references the other or they
     share a mutable file, no edge otherwise. Leaves with no path between them in this graph are
     safe to implement concurrently; this is what lets `build` fan out across multiple agents
     without abandoning the simple linear contract single-agent runs rely on.
   - **Human checkpoint** reviews the whole tree — not a flat list — before any leaf's full phase
     file is drafted. This is still the cheapest point to fix a bad decomposition (reorder, force a
     merge, force a further split), before any prose exists around it. Edits made here are trusted
     as-is (§11's entry on the human-agent interaction mechanism covers how this checkpoint is
     actually operated).
7. **`spec-prism-flow plan draft-phases`** — walks the approved tree in DFS order and, for each
   **leaf only**, repackages the mini-requirements doc `decompose`'s generator already produced for
   it (step 6) into the Phase-File Spec's five-section format (§6): that doc's detailed functional
   requirements become the `Requirements` section verbatim, its file-scope estimate becomes `Scope`,
   its testable requirements drive `Acceptance criteria`/`Manual test checklist`. Internal tree nodes
   (`A`, `A-1`, `A-3` in the example above) are decomposition scaffolding, not phase files
   themselves — their sub-scope names may seed a leaf's title, but nothing is drafted for them.
   Continues self-checking each leaf's word count and file-scope count against the sizing bands
   (§9), flagging (not silently shipping) any outlier for human review.
8. **`spec-prism-flow plan review`** — a final consistency pass over the whole corpus: does the
   dependency chain form a valid order, does `graph.json` agree with it (every linear-chain edge
   appears in the graph, and the graph is acyclic), does every `requirements.md` section map to at
   least one phase, are there stale entries in `OPEN_QUESTIONS.md` that never got resolved.

### 7.3 What "done" looks like

The elicitation loop is done when `plan review` passes and a human has approved the phase corpus —
not when the agent stops asking questions. An unresolved, flagged question is a valid terminal
state for a given run (recorded in the open-decisions log), never silently dropped.

## 8. Feature B — `build` (phase execution)

Generalizes `scripts/orchestrate`, keeping its proven core and dropping what was specific to the
Neighboku two-track experiment.

**Keep, largely as-is:**
- `phase_file.py`'s parser for the Phase-File Spec (§6) format.
- `scope_guard.py` — a diff touching a file outside the phase's declared `Scope` is a blocking
  finding.
- `merge_gates.py`'s hard gate (no merge on a failing build/lint/test) — generalized to run
  whatever command list the target project's config declares, instead of a hardcoded `pnpm
  build`/`pnpm lint`/`pnpm test`.
- The bounded-retry/escalation state machine (cap `address-comments`-style cycles per phase; stop
  and escalate to a human rather than loop indefinitely).
- `track_runner.py`'s "loop phases in declared order, skip already-merged ones, halt on the first
  escalation" behavior — kept as-is as `build`'s **sequential mode** (default, one agent worker):
  walk the DFS-linearized `Depends on` chain from §6/§7.2, one phase at a time.

**Drop:**
- `lockstep_runner.py` and every two-track/baseline-vs-variant comparison concept — Neighboku-only.
- `metrics.py`'s Sonar-issue-delta-between-tracks comparison fields (a per-phase completion log is
  still useful; the cross-track comparison isn't).

**Generalize:**
- `opencode_runner.py` becomes one implementation behind a small "agent runner" interface — an
  abstraction over "run the coding agent against this phase file, in this branch, and commit the
  result" — with Claude Code as the default implementation (opencode stays supported, since it's
  already proven to work here).
- The static-analysis and review steps (Neighboku's `vibe_heal_runner.py`/`harness_runner.py`)
  become optional, config-driven calls into the user's existing `harness` (pr-review) and
  `vibe-heal` CLIs — present and wired in when a target project has them configured, a clean no-op
  when it doesn't (most brand-new projects won't, on day one).
- New: `track_runner.py` gains a **parallel mode**, consulted only when the target project's config
  sets a worker count > 1 (default 1, which degenerates exactly to sequential mode). It reads
  `graph.json` (§6/§7.2) instead of the linear chain: a leaf becomes eligible once every leaf with
  an edge into it has merged, and up to N eligible leaves run concurrently, each against its own
  agent-runner instance, branch, and PR. `scope_guard`'s disjoint-file-scope invariant (enforced at
  graph-construction time in `decompose`) is what makes running them concurrently safe; merge gates
  and bounded-retry/escalation apply per-leaf exactly as in sequential mode, and an escalation on one
  leaf halts only the leaves that depend on it, not the whole run. **Branch/stack organization and
  merge order are left to the human (§11 #5, resolved)** — expected to be rare enough to not warrant
  automation, and better served by dedicated stacked-branch tooling (e.g. git-spice) than by
  `spec-prism-flow` reinventing it. The tool's own responsibility ends at surfacing the order
  (the linear DFS chain and `graph.json`, §6) a human uses to organize that stack.

**The workflow per phase (either mode):** implement → push + open PR (mandatory for v1 — a
local-only, no-push/PR mode was considered and deferred to a future phase, §11 #3) → static
analysis (optional) → AI review (optional) → address comments (bounded retries) → manual test
checklist (human-executed; tool displays the phase file's checklist and records pass/fail;
**advisory by default — logged but non-blocking — with a `--strict` flag making it a hard merge
gate, §11 #2**) → merge. In parallel mode this pipeline runs once per concurrently-eligible leaf.

## 9. Non-functional requirements

- **Sizing bands (single source of truth; derived from G2).** A leaf, and the phase file drafted
  from it, must fit: **file-scope 5–10 files** (hard ceiling ~15) and **roughly 150k–200k tokens**
  of total coding-agent session budget (spec + touched files + review/fix cycles) — both per G2. The
  Neighboku corpus additionally observed a **500–1500 word** doc-length band (full range 532–2100)
  for the phase files these numbers were derived from; that band is a proxy for the token-budget
  figure, not an independent target. G1's Phase-File Spec (§6) and every sizing check in §7.2 cite
  this section rather than restate the numbers.
- **Sizing checks are best-effort, not measured.** There is no way to count the tokens a future
  coding-agent session will spend before that session runs. `decompose`'s generator (§7.2 step 6)
  and `draft-phases`'s self-check (§7.2 step 7) evaluate a chunk/leaf against the *proxies*
  available at draft time — file-scope count and doc length — as a best-effort stand-in for G2's
  real token-budget criterion. This is a deliberate, accepted approximation.
- **Trivial-breakdown deadlock is an accepted risk, not a solved problem.** The generator's
  Feature/component-breakdown judgment (one feature vs. several) and the sizing bands above are
  different axes and can disagree — a chunk can be genuinely "one feature" and still oversized.
  §7.2 step 6 describes the mitigation: one prompt-amended retry to force a split, escalating to
  human review on a second trivial verdict rather than retrying indefinitely. Each occurrence
  (retry or escalation) is logged (§11 #6, resolved) — no automatic feedback loop into the generator
  prompt or the sizing bands for v1; that log is for a human to skim later if the pattern turns out
  to be common enough to warrant tuning either one.
- **Recursive elicitation favors self-containment over inherited context.** Every `decompose`
  generator call (§7.2 step 6) is deliberately scoped to only the chunk's own requirements slice,
  never the full parent document — trading a possible extra clarification iteration for a leaf
  whose `Requirements` section never needs a second document to make sense of it, matching the
  Phase-File Spec's (§6) verbatim-quoting discipline.
- **No abridged elicitation pass at chunk level.** Every `decompose` generator call runs
  `requirements-doc-drafting-prompt.md`'s full structure. An abridged variant was considered and
  rejected: producing an abridged version would itself cost an LLM pass, cancelling out whatever
  the shorter pass would have saved.
- **Scope-guard invariant (§6, §8).** A diff may never touch a file outside its phase's declared
  `Scope`; enforced mechanically, not advisory. In parallel mode (§8) this extends to the dependency
  graph itself: any two leaves without an edge between them must have disjoint file scopes.
- **Merge gate (G4, §8).** No merge on a failing build/lint/test command, whatever command list the
  target project's config (§10) declares.
- **Bounded retries (G4, §8).** `address-comments`-style cycles per phase are capped; on
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
  `click`-based, matching `pr-review`/`refactor-hotspots`'s house style (`scripts/orchestrate` used
  `typer`+`rich`; house style since then has settled on `click`).
- **Config:** a per-target-project TOML (analogous to `.harness.toml`) declaring: the agent backend
  (Claude Code default / opencode), whether `harness`/`vibe-heal` integration is active and how to
  reach it, the path to the project's conventions document, the planning workspace location (where
  `plan init` writes `00-overview.md`/`requirements.md`/`OPEN_QUESTIONS.md`, distinct from the phase
  directory location below), the phase directory location, the build/lint/test command list
  `merge_gates` should run, and `build`'s worker count (default 1 — sequential; >1 switches
  `track_runner` into parallel mode, §8).

## 11. Open decisions log

Flagged explicitly, per the same discipline Neighboku's `requirements.md` §8 used — these should be
resolved by a human before or during Spec Prism Flow's own implementation, not decided unilaterally
by an agent building it:

1. ~~**Tool name.**~~ **Resolved:** the tool is named **Spec Prism Flow** (§0), package/CLI name
   `spec-prism-flow`. Earlier working names "phaseforge" and "Spec Prism" are retained only in this
   document's own history/staging notes.
2. ~~**Manual-test-checklist gate strength.**~~ **Resolved:** advisory by default (agent proceeds,
   checklist is logged but not enforced), with a `--strict` flag switching it to a hard, blocking
   merge gate (§3, §8).
3. ~~**Git/PR operations: mandatory or optional?**~~ **Resolved:** mandatory for v1 — every phase
   goes through `gh pr create`, matching `scripts/orchestrate`'s assumption, so the review/
   static-analysis steps always have something to attach to (§8). A "local-only" mode for solo work
   without a remote was considered and deliberately deferred to a future phase, not built now — the
   v1 scope is already large enough.
4. ~~**Scaffolding a target project's `docs/` tree.**~~ **Resolved:** config-driven, not
   auto-scaffolded (§7.2 step 1, §10) — mirrors `pr-review`'s `.harness.toml` pattern. `plan init`
   never assumes it can create a `docs/` tree, since the user may be contributing to a project whose
   layout they don't control.
5. ~~**Parallel-mode git/branching strategy.**~~ **Resolved:** left entirely to the human (§8).
   `spec-prism-flow` limits itself to surfacing task order (the linear DFS chain and `graph.json`,
   §6); branch stacking and merge-order decisions across concurrently-eligible leaves are expected
   to be rare enough to hand off to dedicated tooling (e.g. git-spice) rather than build and
   maintain that logic in-house.
6. ~~**Feedback loop for trivial-breakdown deadlocks (§9).**~~ **Resolved:** just log it (§9) — no
   automatic adjustment of the generator prompt or sizing bands for v1.
7. ~~**Human-agent-harness interaction mechanism.**~~ **Resolved for v1** (§7.2): the tool prints
   the prompt/output file paths to the console and copies the prompt file's path to both the system
   clipboard and the current tmux paste buffer, then waits for the human to start the coding-agent
   harness and paste it in. Explicitly expected to iterate if this proves too manual in practice.
8. ~~**Does `build` (Feature B) stay part of the tool?**~~ **Resolved: yes, unified.** Both features
   ship as one tool, one CLI, one repo — `spec-prism-flow plan` and `spec-prism-flow build` (§5,
   §10). The alternative (split into a `spec-prism` frontend and a `spec-flow` backend behind a
   shared library, so either half could be swapped independently) was considered and rejected: with
   the user as the only current consumer of either half, the shared-library overhead a clean split
   would need is overkill (§3). The name's "before a single line of application code is generated"
   framing (§0) describes `plan`'s rigor and its motivation, not a scope boundary excluding `build`.

## 12. Next step

Once this draft is reviewed: with all of §11's open decisions now resolved, scaffold
`~/.harness/tools/spec-prism-flow/` via the cookiecutter-uv recipe in §10, move this document to
`spec-prism-flow/docs/requirements.md`, and then — dogfooding the very spec this document defines —
write Spec Prism Flow's own `docs/phases/NN-name-leaf.md` files for building Feature A and
Feature B, and run them through `scripts/orchestrate`'s existing engine (or an early hand-run of
`build` itself, once it exists) to implement Spec Prism Flow.
