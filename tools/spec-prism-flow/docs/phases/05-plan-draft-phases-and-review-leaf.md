# Phase 05 — `plan draft-phases` & `plan review`

*Purpose: turn the approved leaf tree into phase files, then let a human verify the whole corpus is internally consistent before build starts.*

## Scope
- spec_prism_flow/cli.py (additions: `plan draft-phases`, `plan review` subcommands)
- spec_prism_flow/draft_phases.py
- spec_prism_flow/review.py
- tests/test_draft_phases.py
- tests/test_review.py

## Requirements

Excerpt, chunk A-2-4's mini-requirements doc §7 (Detailed functional requirements) and §7.3 (What "done" looks like), quoted verbatim:

### 7.1 `spec-prism-flow plan draft-phases`
Reads the approved tree (persisted by Phase 04's `decompose`) and its DFS visit order. For each **leaf only**: builds a `PhaseFile` (Phase 01) with `number` = DFS visit index, `name` = a slug derived from the leaf's chunk name, `requirements` = the leaf's mini-doc content verbatim, `scope` = the leaf's file-scope estimate, `acceptance_criteria`/`manual_test_checklist` derived from the mini-doc's testable requirements, `depends_on` = `"Phase {N-1} merged."` (or `"None (first phase)."` for phase 1) per the linear DFS chain. Writes each via Phase 01's `render_phase_file` to `{config.plan.phase_dir}/{phase_file_name(number, name)}`. Internal tree nodes are skipped entirely — no file, no log entry beyond an internal "N leaves drafted, M internal nodes skipped" summary. For each leaf drafted, runs Phase 01's `check_file_scope`/`check_word_count`; any result outside its `in_band` range is collected into a flagged-outliers list printed at the end of the run (not raised as an error — flagging, not blocking).

### 7.2 `spec-prism-flow plan review`
Reads every `{config.plan.phase_dir}/*-leaf.md` file plus `{config.plan.phase_dir}/graph.json`, plus the approved `requirements.md` and `OPEN_QUESTIONS.md`. Runs, in order, and reports all four independently (a failure on one does not skip the others):
1. **Dependency-order validity** — parses each `Depends on` field; confirms phase 1 is the only one with `"None (first phase)."` and every other phase *N* names exactly phase *N−1*.
2. **Graph agreement** — calls Phase 01's `validate_graph` with the loaded `Graph` and parsed `PhaseFile` list; surfaces every returned violation string as-is (missing linear edge, cycle, disjoint-scope breach).
3. **Section coverage** — for each numbered `requirements.md` section, checks at least one phase file's `Requirements` body contains a quoted excerpt referencing it (matched by the section's header text or number, e.g. `"§7.2"` appearing in a phase's Requirements); unmatched sections are listed by number and title.
4. **Stale open questions** — parses `OPEN_QUESTIONS.md`'s entries; any entry with no recorded answer and no explicit "deferred" marker is listed.

**Open decision (flagged, not yet resolved — implement as the full `validate_graph` output below until resolved otherwise):** `requirements.md` §7.2 step 8 defines "graph agreement" narrowly — only that every linear-chain edge appears in the graph and the graph is acyclic. Item 2 above instead surfaces `validate_graph`'s entire return value, including the orphan check and the disjoint-scope-breach check — both of which Phase 04's `decompose` already enforces by construction at tree-build time (see Phase 04 §7's `DecomposeError` on a disjoint-scope violation). Whether `plan review`'s Graph-agreement check should stay scoped to just the two spec'd conditions, or whether re-surfacing the construction-time invariants here as a review-time safety net is worth the redundancy, isn't dictated by the spec — flagged here for a human to confirm or override before implementation.

Output is a single structured report (pass/fail per check, with the offending items) printed to console and written to a review-log file in the plan workspace; `plan review` never auto-fixes — a human acts on the report and either re-runs `draft-phases`/`decompose` or explicitly overrides.

### 7.3 What "done" looks like
The elicitation loop is done when `plan review` reports all four checks passing **and** a human has explicitly approved the corpus — not merely when the agent stops producing output. A flagged, unresolved item (a stale open question, a section coverage gap) is a valid terminal state for a given run if a human explicitly acknowledges and defers it; it must never silently disappear from the review report on a re-run.

## Acceptance criteria
- `plan draft-phases` writes exactly one `docs/phases/NN-name-leaf.md` per leaf, in DFS order, and nothing for internal tree nodes.
- Each written phase file's `Requirements` section is the leaf's mini-doc content verbatim (no summarization or truncation); `Scope` matches the leaf's file-scope estimate; `Depends on` follows the fixed linear chain.
- Sizing outliers (file-scope or word-count out of band) are collected and printed at the end of the run without raising or blocking the write.
- Re-running `plan draft-phases` against an unchanged tree produces byte-identical output files.
- `plan review`'s four checks (dependency order, graph agreement, section coverage, stale open questions) each run and report independently — one check's failure never skips or short-circuits the others.
- `plan review` writes a structured report to both console and a review-log file, and never modifies any phase file, `graph.json`, or `requirements.md` itself.
- `uv run pytest` passes for both test files; `ruff check`/`ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/test_draft_phases.py tests/test_review.py -v` and confirm all cases above pass.
- Against a small fixture tree (2–3 leaves, one deliberately oversized), run `plan draft-phases` and confirm the correct files are written, sizing outliers are printed, and internal nodes produce no files.
- Re-run `plan draft-phases` unchanged and diff the output files to confirm byte-identical results.
- Against a fixture corpus with one deliberately broken `Depends on` chain, one `graph.json` missing a required edge, one `requirements.md` section with no phase referencing it, and one unanswered `OPEN_QUESTIONS.md` entry, run `plan review` and confirm all four issues are reported in a single run (not just the first one found).
- Confirm no unhandled exceptions appear in any of the above, and that `plan review` never edits any of the files it inspects.

## Depends on
- Phase 04 merged.
