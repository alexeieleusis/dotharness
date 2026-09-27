## Scope
- harness/runners/regret_review.py
- ../../knowledge/pr-review/review-regret.md
- tests/runners/test_regret_review.py
- harness/runners/common.py
- docs/commands/self-review.md

## Requirements
# Requirements: Backend Judgment Prompt and `review-regret.md`

## 1. Purpose / origin

A leaf of `regret-review`'s decomposition tree (`requirements.md` §7.2 steps 3–4,
§10), covering the pass's one backend invocation per PR and the prose instructions
file it hands to that backend. Models its structure on the existing
`review-design.md` (`~/.harness/knowledge/pr-review/review-design.md`), the closest
existing PR-level-pass instructions file in tone/output-contract shape.

## 2. Problem statement

The previous leaf produces a set of candidate `(comment, blamed_change)` pairs; none
of them are yet judged. Something has to turn "an old comment existed near a line
this fix changes" into "addressing that comment back then would have prevented this
bug" — a judgment call unsuitable for pure code (it requires reading the fix's intent
against the old comment's content), hence the AI backend invocation `requirements.md`
G3 requires.

## 3. Goals

- **G1.** One backend invocation per current-PR run (never per candidate), given: the
  current PR's fix diff (via `build_file_review_section`, `common.py:1101-1133`) and,
  per candidate, the original comment's body/author and the diff of the change it was
  left on.
- **G2.** The prompt demands a structured, deterministically parseable answer (e.g.
  one line per candidate: candidate index + yes/no + one-sentence rationale) — never
  free-form prose this pass would have to re-parse heuristically.
- **G3.** A new `knowledge/pr-review/review-regret.md` instructions file (repo-root
  path; served to the running pass via `config.harness.knowledge_dir / "pr-review" /
  "review-regret.md"`, i.e. `~/.harness/knowledge/pr-review/review-regret.md` on this
  machine), covering: role/perspective (a senior reviewer judging *retrospective*
  relevance, not re-reviewing the current fix), the judgment question verbatim
  (`requirements.md` G3), and the exact structured-output contract G2 requires —
  modeled on `review-design.md`'s Tone/Perspective/Role/Output section structure.
- **G4.** The parsed backend response is turned into zero or more
  `RegretFinding` instances (the shape the marker/poster leaf already defined),
  handed to the wiring leaves for posting.

## 4. Non-goals

- Not defining `RegretFinding`'s shape here — that's already fixed by the
  marker/poster leaf (§7.6); this leaf only populates instances of it.
- Not retrying a malformed backend response within this leaf — a response that
  doesn't match the structured contract is treated as "no confirmed findings this
  run" (fail toward silence, consistent with `requirements.md` §7.2 step 4's stance
  that "nothing to judge" produces no comment) rather than an escalation or retry
  loop; `common.run_pr_level_pass`'s own retry semantics (via the calling runner) are
  the only retry mechanism in play, unchanged by this leaf.
- Not writing `review-regret.md`'s final prose as a rubber-stamp copy of
  `review-design.md` — the content must be genuinely rewritten for this pass's own
  judgment question and output contract; only the section *structure* is borrowed.

## 5. Glossary

See `requirements.md` §5 for `Regret finding`. This leaf is what produces the first
real instances of it.

## 6. Feature/component breakdown

| Component | Question it answers | Input | Output |
|---|---|---|---|
| Prompt builder | What does the backend see? | Current PR diff, candidate pairs | Prompt string |
| `review-regret.md` | What is the backend told to do with it? | — | Instructions text |
| Response parser | Which candidates were confirmed? | Backend's structured response | `list[RegretFinding]` |

## 7. Detailed functional requirements

- Add `build_regret_judgment_prompt(instructions, candidates, pr, ...)` to
  `harness/runners/regret_review.py`, following the same trailer-building
  convention every other pass's prompt builder uses (`_build_pr_metadata_trailer`,
  `common.py:836-851`) for the PR URL/number/repo/commit line.
- The per-candidate section format must give the backend: the blamed file/region
  (from the current PR's own diff, so it can judge "would fixing X have prevented
  this"), the original comment's full body/author, and the diff of the change that
  comment was left on (so the backend can see what the reviewer was actually
  reacting to, not just the comment text in isolation).
- `review-regret.md`'s output contract: one line per candidate in a fixed, greppable
  format (implementer's exact format choice, but it must be unambiguous to parse —
  e.g. `CANDIDATE <n>: YES|NO — <one-sentence reason>`), never a `gh pr comment`
  invocation from inside the instructions file itself (unlike `review-design.md`,
  which does invoke `gh` directly) — this pass's actual posting happens in code (the
  poster leaf), driven by the parsed response, not by the backend directly.
- The response parser lives alongside the prompt builder in `regret_review.py`,
  turning each `YES` line into a `RegretFinding` (carrying that candidate's file/
  region/introducing-PR/comment-id/rationale) and discarding `NO` lines and anything
  unparseable.

## 8. Non-functional requirements

- One backend invocation per PR run, bounded by `harness.backend_timeout_seconds`
  (the existing generic backend timeout — this leaf introduces no separate backend
  timeout of its own; `regret_review_timeout` covers this pass's git/`gh` calls, per
  the config leaf's field description).
- Prompt size is bounded transitively by G1 of the blame/PR-resolver leaf
  (`max_diff_lines`, default 50) — the candidate count this prompt ever needs to
  batch is small by construction, not by anything this leaf itself enforces.

## 9. Out-of-scope / explicit exclusions

Not implementing any UI/formatting beyond the plain structured-line contract in G2 —
no JSON, no markdown table, unless the implementer judges a structured line format
insufficient (flag any deviation explicitly in the phase's own PR description if
taken).

## 10. System/tool shape

- **Files touched:** `harness/runners/regret_review.py` (edit — prompt builder +
  response parser), `knowledge/pr-review/review-regret.md` (new — note this path is
  outside `tools/pr-review/`, at the repo root's `knowledge/` directory, the same
  place `review-design.md`/`review-traceability.md` already live; a build phase whose
  working tree is scoped to `tools/pr-review/` needs to reach one level up to touch
  it — flag this explicitly in the phase file's own Scope section rather than
  silently assuming it's in-tree), `tests/runners/test_regret_review.py` (edit —
  prompt-building and response-parsing tests, including malformed-response handling).

## 11. Open decisions log

None carried from `requirements.md`. `review-regret.md`'s exact prose is itself a
drafting task for this leaf's own implementation, not a decision requiring further
human sign-off beyond matching G3's structural requirements.

## 12. Next step

Once merged, both runner-wiring leaves (§7.3, §7.4) can call this leaf's prompt
builder + response parser as part of their `common.run_pr_level_pass` invocation.

## Acceptance criteria
- One backend invocation per current-PR run (never per candidate), given: the current PR's fix diff (via `build_file_review_section`, `common.py:1101-1133`) and, per candidate, the original comment's body/author and the diff of the change it was left on.
- The prompt demands a structured, deterministically parseable answer (e.g. one line per candidate: candidate index + yes/no + one-sentence rationale) — never free-form prose this pass would have to re-parse heuristically.
- A new `knowledge/pr-review/review-regret.md` instructions file (repo-root path; served to the running pass via `config.harness.knowledge_dir / "pr-review" / "review-regret.md"`, i.e. `~/.harness/knowledge/pr-review/review-regret.md` on this machine), covering: role/perspective (a senior reviewer judging *retrospective* relevance, not re-reviewing the current fix), the judgment question verbatim (`requirements.md` G3), and the exact structured-output contract G2 requires — modeled on `review-design.md`'s Tone/Perspective/Role/Output section structure.
- The parsed backend response is turned into zero or more `RegretFinding` instances (the shape the marker/poster leaf already defined), handed to the wiring leaves for posting.

## Manual test checklist
- Manually verify: One backend invocation per current-PR run (never per candidate), given: the current PR's fix diff (via `build_file_review_section`, `common.py:1101-1133`) and, per candidate, the original comment's body/author and the diff of the change it was left on.
- Manually verify: The prompt demands a structured, deterministically parseable answer (e.g. one line per candidate: candidate index + yes/no + one-sentence rationale) — never free-form prose this pass would have to re-parse heuristically.
- Manually verify: A new `knowledge/pr-review/review-regret.md` instructions file (repo-root path; served to the running pass via `config.harness.knowledge_dir / "pr-review" / "review-regret.md"`, i.e. `~/.harness/knowledge/pr-review/review-regret.md` on this machine), covering: role/perspective (a senior reviewer judging *retrospective* relevance, not re-reviewing the current fix), the judgment question verbatim (`requirements.md` G3), and the exact structured-output contract G2 requires — modeled on `review-design.md`'s Tone/Perspective/Role/Output section structure.
- Manually verify: The parsed backend response is turned into zero or more `RegretFinding` instances (the shape the marker/poster leaf already defined), handed to the wiring leaves for posting.

## Depends on
- Phase 4 merged.
