# Phase 02 — `plan init` & agent-harness hand-off protocol

*Purpose: let a human start a plan workspace and hand prompts to their own coding-agent session, without this tool running that agent itself.*

## Scope
- spec_prism_flow/workspace.py
- spec_prism_flow/handoff.py
- spec_prism_flow/cli.py (addition: `plan init` subcommand registration)
- tests/test_workspace.py
- tests/test_handoff.py
- tests/test_cli.py (addition)

## Requirements

Excerpt, chunk A-2-1's mini-requirements doc §7 (Detailed functional requirements), quoted verbatim:

### 7.1 `plan init` (`spec_prism_flow/cli.py` addition, `spec_prism_flow/workspace.py`)
- CLI: `spec-prism-flow plan init BRIEF_PATH [--code PATH] [--conventions PATH] [--links TEXT]` (`--links` comma-separated URLs, stored verbatim, no validation of reachability).
- `BRIEF_PATH` must exist and be readable; a missing/unreadable brief is a `click.ClickException`, not a silent no-op.
- `--code` and `--conventions`, if given, must be existing paths; `--links` has no existence check (URLs aren't local paths).
- Loads config via Phase 01's `load_config` (config file discovered the same way pr-review discovers `.harness.toml`: `./.spec-prism-flow.toml` unless `--config` overrides it).
- Creates `plan.workspace_dir` (from config) if absent (`mkdir -p`) — permitted because the human already declared this path in their own config, satisfying the explicit-consent requirement in requirements.md §11 #4; no other directory is ever created.
- Writes `plan.workspace_dir/init_manifest.json`: `{"brief": "<resolved abs path>", "code": "<path|null>", "conventions": "<path|null>", "links": ["<url>", ...]}`. This is the only state `plan init` persists; later stages read it rather than re-taking these arguments.
- Re-running `plan init` on a workspace that already has `init_manifest.json` overwrites it after a `click.confirm` prompt (unless `--yes`), matching pr-review's `state reset` confirm-by-default pattern — never a silent overwrite.

### 7.2 Hand-off primitive (`spec_prism_flow/handoff.py`)
- `run_handoff(prompt_text: str, workspace_dir: Path, stage_name: str) -> str`:
  1. Writes `prompt_text` to `workspace_dir/{stage_name}_prompt.md`.
  2. Computes the expected output path `workspace_dir/{stage_name}_output.md`.
  3. Prints both paths to the console (`click.echo`).
  4. Attempts `pbcopy`-style clipboard copy of the prompt path (best-effort; catches and logs `FileNotFoundError`/non-zero exit, never raises).
  5. If `$TMUX` is set in the environment, attempts `tmux load-buffer -` (piping the prompt path) to copy it into the current tmux paste buffer; same best-effort handling.
  6. Blocks on `click.confirm("Agent finished writing output? ")` (or equivalent) — loops re-prompting, does not time out, since the human controls an entirely separate, unbounded-duration coding-agent session.
  7. On confirmation, requires the output file to exist; if missing, raises `HandoffError` naming the expected path rather than silently returning empty content.
  8. Returns the output file's full text content.
- This function has no knowledge of what the prompt/output *mean* — callers (later `plan` stages) build the prompt text and parse the returned output themselves.

## Acceptance criteria
- `plan init` rejects a missing/unreadable `BRIEF_PATH` with a `click.ClickException`, never a silent no-op or bare traceback.
- `plan init` rejects a nonexistent `--code`/`--conventions` path the same way; `--links` accepts arbitrary comma-separated text with no reachability check.
- `plan init` creates `plan.workspace_dir` (from config) via `mkdir -p` if absent, and never creates any other directory.
- `plan init` writes `init_manifest.json` with resolved absolute paths for `brief`/`code`/`conventions` and a list for `links`; missing optional fields serialize as `null`/empty list, not omitted keys.
- Re-running `plan init` against a workspace with an existing `init_manifest.json` prompts for confirmation (unless `--yes`) before overwriting; declining leaves the existing file untouched.
- `run_handoff` writes the prompt file, prints both paths, attempts clipboard and (when `$TMUX` is set) tmux-buffer copy without raising on either failing, blocks on confirmation, and raises `HandoffError` (naming the expected path) if the output file doesn't exist when the human confirms.
- Clipboard/tmux failures are logged but never propagate as exceptions out of `run_handoff`.
- `uv run pytest` passes for all touched test files; `ruff check`/`ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/test_workspace.py tests/test_handoff.py tests/test_cli.py -v` and confirm all cases above pass.
- From a scratch directory with a minimal `.spec-prism-flow.toml` and a `brief.md` file, run `spec-prism-flow plan init brief.md` and confirm `plan.workspace_dir/init_manifest.json` is created with the correct resolved path.
- Re-run the same command and confirm a confirmation prompt appears before overwrite; decline it and confirm the file is unchanged.
- Manually invoke `run_handoff` (e.g. via a small test script) and confirm the prompt/output paths print to console, and confirm behavior is correct both inside and outside a `tmux` session (best-effort tmux copy attempted only when `$TMUX` is set).
- Confirm no unhandled exceptions or stack traces appear in any of the above — clipboard/tmux failures are caught and logged without raising (per §7.2 steps 4–5).

## Depends on
- Phase 01 merged.
