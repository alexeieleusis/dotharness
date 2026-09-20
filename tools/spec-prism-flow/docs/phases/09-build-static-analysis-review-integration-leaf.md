# Phase 09 — Static analysis & AI review integration

*Purpose: port the static-analysis and AI-review integrations a build phase needs, dropping the two pieces specific to the source project that don't apply here.*

## Scope
- spec_prism_flow/build/harness_integration.py
- spec_prism_flow/build/vibe_heal_integration.py
- tests/build/test_harness_integration.py
- tests/build/test_vibe_heal_integration.py

## Requirements

Excerpt, chunk A-3-3-2's mini-requirements doc §7 (Detailed functional requirements), quoted verbatim:

### 7.1 `spec_prism_flow/build/harness_integration.py`
Ports `harness_runner.py`'s `run`/`self_review`/`address_comments` near-verbatim: `uv run --project <harness_tool_dir> harness run --config <harness_config> <subcommand>` subprocess (`cwd=clone`, `capture_output=True`, `text=True`, `check=False`, caller-supplied `timeout`), non-zero exit → `HarnessCommandError(args, returncode, stderr)` (a `CommandError` subclass per Phase 06). `harness_tool_dir` and `harness_config` path come from `config.review` (Phase 01's `ReviewConfig`, already resolved — this phase does no path discovery of its own). `focused_review` is **not** ported — no Spec Prism Flow equivalent of the vibe-types-elaboration experiment exists or is planned. Each of `self_review`/`address_comments` checks `config.review.enabled` first and short-circuits to a no-op (returns `""`) when `False` — callers may also check `enabled` themselves before calling, but this phase's functions must be safe to call unconditionally either way.

### 7.2 `spec_prism_flow/build/vibe_heal_integration.py`
Ports `vibe_heal_runner.py`'s `_run`/`scan`/`post`/`fingerprints_from_report`/`should_post`/`diff_fingerprints` near-verbatim, including the `sonar-scanner`-on-`PATH` discovery (`@lru_cache`d) and the explicit `--report-file`/`--env-file` requirement on every `vibe-heal review` invocation — omitting `--report-file` risks an unhandled `sys.exit(1)` on default-path SonarQube-config resolution failure, a correctness requirement inherited verbatim from the source, not a stylistic choice. `vibe_heal_tool_dir` comes from `config.vibe_heal` (Phase 01's `VibeHealConfig`). `scan`/`post` both check `config.vibe_heal.enabled` first; `scan` returns `None` and `post` is a no-op when `False`. Fingerprint helpers, unchanged in behavior: `fingerprints_from_report(report) -> set[Fingerprint]` (restricted to `on_changed_line=True` issues — the only kind vibe-heal actually posts, others 422 on trailing-context lines), `should_post(current, already_posted) -> bool` (post only if this cycle surfaced a fingerprint never posted before), `diff_fingerprints(previous, current) -> tuple[int, int]` (opened/resolved counts). `generate_lensflow_report` is **not** ported — it regenerates a LensFlow-app-specific ESLint report file that Spec Prism Flow has no equivalent need for; the target project's own `sonar-project.properties`, if any, is assumed already correctly configured by the human.

## Acceptance criteria
- `self_review`/`address_comments` invoke `uv run --project <harness_tool_dir> harness run --config <harness_config> <subcommand>` with `cwd=clone`, and raise `HarnessCommandError` (carrying the full args/returncode/stderr) on non-zero exit.
- Both harness functions are safe to call unconditionally and short-circuit to a no-op (`""`, no subprocess call) when `config.review.enabled` is `False` — verified by a test asserting `subprocess.run`/`subprocess.Popen` is never invoked in the disabled case.
- `scan` always passes an explicit `--report-file` and `--env-file`; returns `None` and makes no subprocess call when `config.vibe_heal.enabled` is `False`.
- `post` is a no-op when `config.vibe_heal.enabled` is `False`, and carries no deduplication logic itself — callers must call `should_post` first.
- `fingerprints_from_report` includes only `on_changed_line=True` issues; `should_post`/`diff_fingerprints` behave exactly as specified against constructed fixtures.
- `focused_review` and `generate_lensflow_report` are not present anywhere in this phase's modules.
- `uv run pytest` passes for both test files (subprocess calls mocked, including a dedicated no-op-when-disabled assertion per file); `ruff check`/`ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/build/test_harness_integration.py tests/build/test_vibe_heal_integration.py -v` and confirm all cases above pass, including both no-op-when-disabled assertions.
- With `config.review.enabled = False`, call `self_review` in a scratch script and confirm (e.g. via a monkeypatched `subprocess.run` that raises if called) that no subprocess is spawned.
- With `config.vibe_heal.enabled = True` and a mocked `vibe-heal` binary, call `scan` and confirm the constructed command includes explicit `--report-file`/`--env-file`/`--base-branch`/`--pr` arguments.
- Feed `fingerprints_from_report` a fixture report mixing `on_changed_line=True` and `False` issues and confirm only the `True` ones are included.
- Confirm no unhandled exceptions appear in any of the above.

## Depends on
- Phase 08 merged.
