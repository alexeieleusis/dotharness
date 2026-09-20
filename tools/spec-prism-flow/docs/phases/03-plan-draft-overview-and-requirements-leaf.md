# Phase 03 — `plan draft-overview` & `plan draft-requirements`

*Purpose: drive the human-in-the-loop drafting of the corpus overview and requirements doc, one hand-off cycle at a time.*

## Scope
- spec_prism_flow/overview_stage.py
- spec_prism_flow/requirements_stage.py
- spec_prism_flow/cli.py (additions: `plan draft-overview`, `plan draft-requirements` subcommands)
- tests/test_overview_stage.py
- tests/test_requirements_stage.py

## Requirements

Excerpt, chunk A-2-2's mini-requirements doc §7 (Detailed functional requirements), quoted verbatim:

### 7.1 `plan draft-overview` (`overview_stage.py`)
Reads the brief file path, and optional brownfield-code path / conventions-doc path / reference links, from the workspace config Phase 02's `plan init` wrote (`init_manifest.json`). Builds a prompt file that instructs the agent to draft `00-overview.md` (problem statement, goals, non-goals, glossary, key decisions already implied by the brief) modeled on Neighboku's `00-overview.md`, writing anything unresolved to `OPEN_QUESTIONS.md` as a concrete, answerable question — not left implicit. Invokes Phase 02's `handoff.run_handoff(prompt_text, workspace_dir, stage_name="draft_overview", output_filename="00-overview.md")` so the primitive's expected-output path matches what the prompt instructs the agent to write. After the hand-off returns, verifies `00-overview.md` exists (a `PhaseFileError`-style exception, not a silent no-op, if the agent never wrote it) and reports the also-expected `OPEN_QUESTIONS.md` path — that file may legitimately not exist if the agent found nothing to ask (a fully-resolved brief is valid).

### 7.2 Human checkpoint (overview)
Not a subcommand of its own — the CLI's convention (consistent with Phase 02's hand-off) is that `draft-overview` exits after the agent run and prints instructions: "edit OPEN_QUESTIONS.md (if present) or 00-overview.md directly, then run `plan draft-requirements` when ready." No automatic advance; `draft-requirements` is a separate, human-triggered command.

### 7.3 `plan draft-requirements` (`requirements_stage.py`)
Reads the approved `00-overview.md` (+ `OPEN_QUESTIONS.md` if present, answered or not — an unanswered question is passed through, not blocked on, since a flagged open question is a valid terminal state, not an error). Constructs a prompt file that concatenates: the full text of `requirements-doc-drafting-prompt.md` (read from the config's `knowledge_dir`, matching pr-review's `harness.knowledge_dir` convention — this phase does not hardcode the path), then the approved overview, then any conventions-doc content (from `plan init`'s stored path) as an appended "architecture/convention constraints" input. Invokes Phase 02's `run_handoff(prompt_text, workspace_dir, stage_name="draft_requirements", output_filename="requirements.md")`. Verifies `requirements.md` exists after the hand-off returns.

### 7.4 Human checkpoint (requirements)
Same pattern as 7.2: `draft-requirements` exits after the agent run, printing "review/edit requirements.md, then proceed to `plan decompose` when ready." No automatic advance into decompose.

**Open decision (flagged, not yet resolved — implement with a `click.confirm`-gated overwrite, consistent with Phase 02's `plan init` pattern, until resolved otherwise):**
- The exact confirmation UX for re-running `draft-overview`/`draft-requirements` when it would clobber an in-progress human edit is not yet specified beyond this default.

## Acceptance criteria
- `plan draft-overview` reads `init_manifest.json` (brief/code/conventions/links paths) and builds a prompt file instructing the agent to draft `00-overview.md` and, where needed, `OPEN_QUESTIONS.md`.
- `plan draft-overview` invokes `run_handoff` with `stage_name="draft_overview"` and `output_filename="00-overview.md"` and, after it returns, raises if `00-overview.md` was not written; does not raise if `OPEN_QUESTIONS.md` is absent.
- `plan draft-overview` prints an "edit and run draft-requirements when ready" message on success and does not invoke `draft-requirements` itself.
- `plan draft-requirements` constructs its prompt by concatenating the full, unabridged text of `requirements-doc-drafting-prompt.md` (path resolved from `config.harness.knowledge_dir`-equivalent, never hardcoded), the approved overview, and any conventions-doc content.
- `plan draft-requirements` invokes `run_handoff` with `stage_name="draft_requirements"` and `output_filename="requirements.md"`, and raises if `requirements.md` was not written after it returns.
- Re-running either stage does not silently clobber an in-progress human edit without at least a warning (see the open decision above).
- `uv run pytest` passes for both test files; `ruff check`/`ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/test_overview_stage.py tests/test_requirements_stage.py -v` and confirm all cases above pass.
- From a workspace with a completed `plan init`, run `spec-prism-flow plan draft-overview` (with `run_handoff`'s agent step stubbed/mocked for this manual check) and confirm the prompt file references the brief and any supplied code/conventions/links.
- Confirm `plan draft-overview` fails clearly (no stack trace) when the stubbed hand-off doesn't produce `00-overview.md`.
- Run `spec-prism-flow plan draft-requirements` against a fixture `00-overview.md` and confirm the constructed prompt file contains the full, unabridged `requirements-doc-drafting-prompt.md` text (not a truncated or summarized version) followed by the overview content.
- Confirm no unhandled exceptions appear in any of the above.

## Depends on
- Phase 02 merged.
