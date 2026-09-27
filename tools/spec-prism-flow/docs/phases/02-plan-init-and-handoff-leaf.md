# Phase 02 — `plan init` & agent-harness handoff protocol

*Purpose: let a human start a plan workspace and pass prompts to their own coding-agent session. This tool does not run the coding agent.*

## Scope
- spec_prism_flow/workspace.py
- spec_prism_flow/handoff.py
- spec_prism_flow/cli.py (addition: `plan init` subcommand registration)
- tests/test_workspace.py
- tests/test_handoff.py
- tests/test_cli.py (addition)

## Requirements

The following is an excerpt, restructured for clarity, from chunk A-2-1's mini-requirements doc, section 7 (Detailed functional requirements).

### 7.1 `plan init` (`spec_prism_flow/cli.py` addition, `spec_prism_flow/workspace.py`)
- CLI command: `spec-prism-flow plan init BRIEF_PATH [--code PATH] [--conventions PATH] [--links TEXT] [--config PATH] [--yes]`
  - `--links` takes comma-separated URLs. The tool stores them verbatim. It does not check whether a URL is reachable.
  - `--config` overrides config-file discovery.
  - `--yes` skips the overwrite-confirmation prompt.
- The brief at `BRIEF_PATH` must exist and be readable. If the brief is missing or unreadable, `plan init` raises a `click.ClickException` rather than a silent no-op.
- The `--code` and `--conventions` paths, if given, must exist. `--links` has no existence check because URLs are not local paths.
- The tool loads the config via Phase 01's `load_config`. It discovers the config file the same way pr-review discovers `.harness.toml`. The config file is `./.spec-prism-flow.toml` unless `--config` overrides it.
- The tool creates `plan.workspace_dir` (from the config) if it does not exist, using `mkdir -p`. This is permitted because the human already declared this path in their config, which satisfies the explicit-consent requirement in requirements.md §11 #4. The tool never creates any other directory.
- The tool writes `plan.workspace_dir/init_manifest.json` with the shape `{"brief": "<resolved abs path>", "code": "<path|null>", "conventions": "<path|null>", "links": ["<url>", ...]}`. This is the only state `plan init` persists. Later stages read it rather than re-taking these arguments.
- Re-running `plan init` on a workspace that already has `init_manifest.json` overwrites the file after a `click.confirm` prompt, unless `--yes` is set. This matches pr-review's `state reset` confirm-by-default pattern. The tool never overwrites silently.

### 7.2 Handoff primitive (`spec_prism_flow/handoff.py`)
- `run_handoff(prompt_text: str, workspace_dir: Path, stage_name: str, output_filename: str | None = None) -> str`:
  1. Writes `prompt_text` to `workspace_dir/{stage_name}_prompt.md`.
  2. Computes the expected output path: `workspace_dir/{output_filename}` when `output_filename` is given, and `workspace_dir/{stage_name}_output.md` otherwise. Callers whose output has a fixed name (for example, Phase 03's `00-overview.md`/`requirements.md`) pass `output_filename` explicitly instead of renaming the file afterward.
  3. Prints both paths to the console (`click.echo`).
  4. Attempts a `pbcopy`-style clipboard copy of the prompt path. This step is best-effort. It catches and logs a `FileNotFoundError` or a non-zero exit code and never raises.
  5. If `$TMUX` is set in the environment, attempts `tmux load-buffer -` (piping the prompt path) to copy it into the current tmux paste buffer. It uses the same best-effort handling.
  6. Blocks on `click.confirm("Agent finished writing output? ")` (or an equivalent prompt). It re-prompts in a loop and does not time out. The human controls a separate coding-agent session of unlimited duration.
  7. After confirmation, requires the output file to exist. If the file is missing, it raises a `HandoffError` that names the expected path, instead of silently returning empty content.
  8. Returns the output file's full text content.
- This function does not interpret the meaning of the prompt or output. Callers (later `plan` stages) build the prompt text and parse the returned output.

## Acceptance criteria
- `plan init` rejects a missing or unreadable `BRIEF_PATH` with a `click.ClickException`, never a silent no-op or a bare traceback.
- `plan init` rejects a nonexistent `--code` or `--conventions` path the same way. `--links` accepts arbitrary comma-separated text with no reachability check.
- `plan init` creates `plan.workspace_dir` (from config) via `mkdir -p` if it does not exist, and never creates any other directory.
- `plan init` writes `init_manifest.json` with resolved absolute paths for `brief`, `code`, and `conventions`, and a list for `links`. Missing optional fields serialize as `null` or an empty list, not as omitted keys.
- Re-running `plan init` against a workspace with an existing `init_manifest.json` prompts for confirmation before overwriting, unless `--yes` is set. Declining leaves the existing file untouched.
- `run_handoff` does the following:
  - writes the prompt file
  - prints both paths
  - attempts the clipboard copy and, when `$TMUX` is set, the tmux-buffer copy, without raising if either fails
  - blocks on confirmation
  - raises a `HandoffError` (naming the expected path) if the output file does not exist when the human confirms
- A "no" answer to the `run_handoff` confirmation re-prompts the same confirmation. It is not a final decision. Only a "yes" answer proceeds to checking for the output file.
- Clipboard and tmux failures are logged but never propagate as exceptions out of `run_handoff`.
- `uv run pytest` passes for all touched test files. `ruff check` and `ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/test_workspace.py tests/test_handoff.py tests/test_cli.py -v` and confirm all cases above pass.
- From a scratch directory with a minimal `.spec-prism-flow.toml` and a `brief.md` file, run `spec-prism-flow plan init brief.md`. Confirm `plan.workspace_dir/init_manifest.json` is created with the correct resolved path.
- Re-run the same command. Confirm a prompt appears before the overwrite. Decline it and confirm the file is unchanged.
- Manually invoke `run_handoff` (for example, via a small test script). Confirm the prompt and output paths print to the console. Confirm the behavior is correct both inside and outside a `tmux` session. The best-effort tmux copy is attempted only when `$TMUX` is set.
- Manually invoke `run_handoff`. Answer "no" once at the confirmation prompt. Confirm it re-prompts the same confirmation rather than raising or returning. Then answer "yes" and confirm it proceeds to checking for the output file.
- Confirm no unhandled exceptions or stack traces appear in any of the steps above. Clipboard and tmux failures are caught and logged without raising (per §7.2 steps 4–5).

## Depends on
- Phase 01 merged.
