# Phase 03 — `plan draft-overview` & `plan draft-requirements`

*Purpose: drive the human-in-the-loop drafting of the corpus overview and requirements doc, one hand-off cycle at a time.*

## Scope
- spec_prism_flow/overview_stage.py
- spec_prism_flow/requirements_stage.py
- spec_prism_flow/cli.py (additions: `plan draft-overview`, `plan draft-requirements` subcommands)
- tests/test_overview_stage.py
- tests/test_requirements_stage.py

## Requirements

Excerpt from chunk A-2-2's mini-requirements doc §7 (Detailed functional requirements):

### 7.1 `plan draft-overview` (`overview_stage.py`)

The command performs the following steps:

1. It reads these values from `init_manifest.json`, the workspace config that Phase 02's `plan init` wrote: the brief file path, an optional brownfield-code path, an optional conventions-doc path, and optional reference links.
2. It builds a prompt file. The prompt instructs the agent to draft `00-overview.md` (problem statement, goals, non-goals, glossary, key decisions already implied by the brief), modeled on Neighboku's `00-overview.md`. It also instructs the agent to write anything unresolved to `OPEN_QUESTIONS.md` as a concrete, answerable question — not left implicit.
3. It invokes Phase 02's `handoff.run_handoff(prompt_text, workspace_dir, stage_name="draft_overview", output_filename="00-overview.md")`. This keeps the handoff's expected-output path matched to the file the prompt instructs the agent to write.
4. After the hand-off returns, it verifies that `00-overview.md` exists. If the agent never wrote it, it raises a `PhaseFileError`-style exception, not a silent no-op.
5. It also reports the expected `OPEN_QUESTIONS.md` path. That file may legitimately not exist if the agent found nothing to ask (a fully-resolved brief is valid).

### 7.2 Human checkpoint (overview)

This is not a subcommand of its own. Following the CLI convention from Phase 02's hand-off, `draft-overview` exits after the agent run and prints these instructions: "edit OPEN_QUESTIONS.md (if present) or 00-overview.md directly, then run `plan draft-requirements` when ready." There is no automatic advance. `draft-requirements` is a separate, human-triggered command.

### 7.3 `plan draft-requirements` (`requirements_stage.py`)

The command performs the following steps:

1. It reads the approved `00-overview.md`, and `OPEN_QUESTIONS.md` if present (answered or not). It does not block on an unanswered question. It passes it through, because a flagged open question is a valid terminal state, not an error.
2. It constructs a prompt file that concatenates, in order:
   - the full text of `requirements-doc-drafting-prompt.md` (read from the config's `knowledge_dir`, matching pr-review's `harness.knowledge_dir` convention — this phase does not hardcode the path)
   - the approved overview
   - any conventions-doc content (from `plan init`'s stored path), appended as an "architecture/convention constraints" input
3. It invokes Phase 02's `run_handoff(prompt_text, workspace_dir, stage_name="draft_requirements", output_filename="requirements.md")`.
4. After the hand-off returns, it verifies that `requirements.md` exists.

### 7.4 Human checkpoint (requirements)

Same pattern as 7.2. `draft-requirements` exits after the agent run and prints: "review/edit requirements.md, then proceed to `plan decompose` when ready." There is no automatic advance into decompose.

**Open decision (flagged, not yet resolved):** Until resolved otherwise, implement the overwrite with a `click.confirm` gate, consistent with Phase 02's `plan init` pattern.

- The exact confirmation UX for re-running `draft-overview` or `draft-requirements` when the re-run would clobber an in-progress human edit is not yet specified beyond this default.

## Acceptance criteria

- `plan draft-overview` reads `init_manifest.json` (the paths for the brief file, the brownfield code, the conventions doc, and the reference links) and builds a prompt file that instructs the agent to draft `00-overview.md` and, where needed, `OPEN_QUESTIONS.md`.
- `plan draft-overview` invokes `run_handoff` with `stage_name="draft_overview"` and `output_filename="00-overview.md"`. After it returns, it raises if the agent never wrote `00-overview.md`. It does not raise if `OPEN_QUESTIONS.md` is absent.
- On success, `plan draft-overview` prints an "edit and run draft-requirements when ready" message. It does not invoke `draft-requirements` itself.
- `plan draft-requirements` constructs its prompt by concatenating the full, unabridged text of `requirements-doc-drafting-prompt.md` (resolve the path from a config value equivalent to `config.harness.knowledge_dir`, and never hardcode it), the approved overview, and any conventions-doc content.
- `plan draft-requirements` invokes `run_handoff` with `stage_name="draft_requirements"` and `output_filename="requirements.md"`. After it returns, it raises if the agent never wrote `requirements.md`.
- Re-running either stage does not silently clobber an in-progress human edit. At least a warning is required (see the open decision above).
- `uv run pytest` passes for both test files. `ruff check` and `ty` pass with no new violations.

## Manual test checklist

- Run `uv run pytest tests/test_overview_stage.py tests/test_requirements_stage.py -v`. Confirm that all cases above pass.
- From a workspace with a completed `plan init`, run `spec-prism-flow plan draft-overview`. For this manual check, stub or mock `run_handoff`'s agent step. Confirm that the prompt file references the brief and any supplied code, conventions, or links.
- Confirm that `plan draft-overview` fails clearly (no stack trace) when the stubbed hand-off doesn't produce `00-overview.md`.
- Run `spec-prism-flow plan draft-requirements` against a fixture `00-overview.md`. Confirm that the constructed prompt file contains the full, unabridged `requirements-doc-drafting-prompt.md` text (not a truncated or summarized version) followed by the overview content.
- Confirm that no unhandled exceptions appear in any of the above.

## Depends on
- Phase 02 merged.
