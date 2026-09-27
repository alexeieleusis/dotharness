## Scope
- harness/runners/regret_review.py
- ../../knowledge/pr-review/review-regret.md
- tests/runners/test_regret_review.py
- harness/runners/common.py
- docs/commands/self-review.md

## Requirements
# Requirements: Backend Judgment Prompt and `review-regret.md`

### 1. Purpose / origin

This leaf belongs to `regret-review`'s decomposition tree (`requirements.md`
§7.2 steps 3–4, §10). It covers the pass's one backend invocation per PR and
the prose instructions file it sends to that backend. Its structure models
the existing `review-design.md`
(`~/.harness/knowledge/pr-review/review-design.md`), the closest existing
PR-level-pass instructions file in tone and output-contract shape.

### 2. Problem statement

The previous leaf produces a set of candidate `(comment, blamed_change)`
pairs. None of them are judged yet. This leaf must convert "an old comment
existed near a line this fix changes" into "addressing that comment back
then would have prevented this bug". That is a judgment call. Pure code
cannot make it. Doing so requires reading the fix's intent against the old
comment's content. That is why `requirements.md` G3 requires the AI backend
invocation.

### 3. Goals

- **G1.** This leaf makes one backend invocation per current-PR run, never
  one per candidate. It receives the current PR's fix diff (via
  `build_file_review_section`, `common.py:1101-1133`). Per candidate, it
  also receives the original comment's body/author and the diff of the
  change that comment was left on.
- **G2.** The prompt demands a structured answer that parses
  deterministically (e.g. one line per candidate: candidate index + yes/no +
  one-sentence rationale). It never demands free-form prose this pass would
  have to re-parse heuristically.
- **G3.** This leaf adds a new `knowledge/pr-review/review-regret.md`
  instructions file at the repo-root path. The running pass receives it via
  `config.harness.knowledge_dir / "pr-review" / "review-regret.md"` (i.e.
  `~/.harness/knowledge/pr-review/review-regret.md` on this machine). The
  file covers the role/perspective: a senior reviewer judging *retrospective*
  relevance, not re-reviewing the current fix. It also covers the judgment
  question verbatim (`requirements.md` G3) and the exact structured-output
  contract G2 requires. It models `review-design.md`'s
  Tone/Perspective/Role/Output section structure.
- **G4.** This leaf produces zero or more `RegretFinding` instances from the
  parsed backend response. The marker/poster leaf already defined that
  shape. This leaf sends them to the wiring leaves for posting.

### 4. Non-goals

- This leaf does not define `RegretFinding`'s shape. The marker/poster leaf
  already fixed it (§7.6). This leaf only populates instances of it.
- This leaf does not retry a malformed backend response. This leaf treats a
  response that does not match the structured contract as "no confirmed
  findings this run". It fails toward silence. That matches `requirements.md`
  §7.2 step 4's stance that "nothing to judge" produces no comment. The
  response does not start an escalation or retry loop.
  `common.run_pr_level_pass`'s own retry semantics (via the calling runner)
  are the only retry mechanism. This leaf does not change them.
- This leaf does not write `review-regret.md`'s final prose as a verbatim
  copy of `review-design.md`. It rewrites the content for this pass's own
  judgment question and output contract. It borrows only the section
  *structure*.

### 5. Glossary

See `requirements.md` §5 for `Regret finding`. This leaf produces the first
real instances of it.

### 6. Feature/component breakdown

| Component | Question it answers | Input | Output |
|---|---|---|---|
| Prompt builder | What does the backend see? | Current PR diff, candidate pairs | Prompt string |
| `review-regret.md` | What is the backend told to do with it? | — | Instructions text |
| Response parser | Which candidates were confirmed? | Backend's structured response | `list[RegretFinding]` |

### 7. Detailed functional requirements

- Add `build_regret_judgment_prompt(instructions, candidates, pr, ...)` to
  `harness/runners/regret_review.py`. Follow the same trailer-building
  convention every other pass's prompt builder uses for the
  PR URL/number/repo/commit line (`_build_pr_metadata_trailer`,
  `common.py:836-851`).
- The per-candidate section must give the backend the blamed file/region,
  the original comment's full body/author, and the diff of the change that
  comment was left on. The blamed file/region comes from the current PR's
  own diff, so the backend can judge "would fixing X have prevented this".
  The diff of the old change shows what the reviewer reacted to, not just
  the comment text in isolation.
- `review-regret.md`'s output contract: one line per candidate, in a fixed,
  greppable format. The implementer chooses the exact format, but it must
  parse unambiguously (e.g. `CANDIDATE <n>: YES|NO — <one-sentence reason>`).
  The instructions file itself must never call `gh pr comment`. That differs
  from `review-design.md`, which invokes `gh` directly. This pass's posting
  happens in code: the poster leaf posts from the parsed response, not the
  backend.
- The response parser lives in `regret_review.py`, next to the prompt
  builder. It creates one `RegretFinding` per `YES` line. Each finding
  carries that candidate's file/region/introducing-PR/comment-id/rationale.
  It discards `NO` lines and anything unparseable.

### 8. Non-functional requirements

- This leaf makes one backend invocation per PR run.
  `harness.backend_timeout_seconds` bounds that call. It is the existing
  generic backend timeout. This leaf introduces no separate backend timeout.
  `regret_review_timeout` covers this pass's git/`gh` calls, per the config
  leaf's field description.
- G1 of the blame/PR-resolver leaf bounds the prompt size (`max_diff_lines`,
  default 50). The candidate count is small by construction, not by anything
  this leaf enforces.

### 9. Out-of-scope / explicit exclusions

This leaf implements no UI or formatting beyond the plain structured-line
contract in G2. No JSON. No markdown table. The implementer may deviate if
they judge a structured line format insufficient. They must then flag the
deviation explicitly in the phase's own PR description.

### 10. System/tool shape

- **Files touched:** `harness/runners/regret_review.py` (edit — prompt
  builder + response parser). `knowledge/pr-review/review-regret.md` (new).
  Note: this path is outside `tools/pr-review/`, at the repo root's
  `knowledge/` directory, where `review-design.md`/`review-traceability.md`
  already live. A build phase whose working tree is scoped to
  `tools/pr-review/` needs to reach one level up to touch it. Flag this
  explicitly in the phase file's own Scope section. Do not silently assume it
  is in-tree. `tests/runners/test_regret_review.py` (edit — prompt-building
  and response-parsing tests, including malformed-response handling).

### 11. Open decisions log

This log carries none from `requirements.md`. The exact prose of
`review-regret.md` is a drafting task for this leaf's implementation. It is
not a decision that needs further human sign-off, beyond matching G3's
structural requirements.

### 12. Next step

Once merged, both runner-wiring leaves (§7.3, §7.4) can call this leaf's
prompt builder + response parser as part of their `common.run_pr_level_pass`
invocation.

## Acceptance criteria
- This leaf makes one backend invocation per current-PR run, never one per candidate. It receives the current PR's fix diff (via `build_file_review_section`, `common.py:1101-1133`). Per candidate, it also receives the original comment's body/author and the diff of the change that comment was left on.
- The prompt demands a structured answer that parses deterministically (e.g. one line per candidate: candidate index + yes/no + one-sentence rationale). It never demands free-form prose this pass would have to re-parse heuristically.
- This leaf adds a new `knowledge/pr-review/review-regret.md` instructions file at the repo-root path. The running pass receives it via `config.harness.knowledge_dir / "pr-review" / "review-regret.md"` (i.e. `~/.harness/knowledge/pr-review/review-regret.md` on this machine). The file covers the role/perspective: a senior reviewer judging *retrospective* relevance, not re-reviewing the current fix. It also covers the judgment question verbatim (`requirements.md` G3) and the exact structured-output contract G2 requires. It models `review-design.md`'s Tone/Perspective/Role/Output section structure.
- This leaf produces zero or more `RegretFinding` instances from the parsed backend response. The marker/poster leaf already defined that shape. This leaf sends them to the wiring leaves for posting.

## Manual test checklist
- Manually verify: this leaf makes one backend invocation per current-PR run, never one per candidate. It receives the current PR's fix diff (via `build_file_review_section`, `common.py:1101-1133`). Per candidate, it also receives the original comment's body/author and the diff of the change that comment was left on.
- Manually verify: the prompt demands a structured answer that parses deterministically (e.g. one line per candidate: candidate index + yes/no + one-sentence rationale). It never demands free-form prose this pass would have to re-parse heuristically.
- Manually verify: this leaf adds a new `knowledge/pr-review/review-regret.md` instructions file at the repo-root path. The running pass receives it via `config.harness.knowledge_dir / "pr-review" / "review-regret.md"` (i.e. `~/.harness/knowledge/pr-review/review-regret.md` on this machine). The file covers the role/perspective: a senior reviewer judging *retrospective* relevance, not re-reviewing the current fix. It also covers the judgment question verbatim (`requirements.md` G3) and the exact structured-output contract G2 requires. It models `review-design.md`'s Tone/Perspective/Role/Output section structure.
- Manually verify: this leaf produces zero or more `RegretFinding` instances from the parsed backend response. The marker/poster leaf already defined that shape. This leaf sends them to the wiring leaves for posting.

## Depends on
- Phase 4 merged.
