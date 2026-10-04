# Phase 01 — Shared substrate: config & Phase-File Spec

*Purpose: define the config schema, phase-file format, dependency graph, and sizing checks every later phase depends on.*

## Scope
- spec_prism_flow/config.py
- spec_prism_flow/phase_file.py
- spec_prism_flow/graph.py
- spec_prism_flow/sizing.py
- tests/test_config.py
- tests/test_phase_file.py
- tests/test_graph.py
- tests/test_sizing.py

## Requirements

Excerpt from chunk A-1's mini-requirements doc §7 (Detailed functional requirements), lightly edited for clarity:

### 7.1 Config (`spec_prism_flow/config.py`)
A per-target-project TOML, analogous to `.harness.toml`, with these sections and required-vs-optional fields:
```toml
[agent]
backend = "claude"          # "claude" or "opencode"; defaults to "claude" when the key is absent (§10) — a present
                             # but invalid value (anything other than "claude"/"opencode") is a ConfigError.

[plan]
workspace_dir = "docs"                    # required — where 00-overview.md/requirements.md/OPEN_QUESTIONS.md live
phase_dir = "docs/phases"                 # required — where NN-name-leaf.md + graph.json live
conventions_path = "docs/CONVENTIONS.md"  # optional; None if absent

[review]
enabled = false
command = "harness"

[vibe_heal]
enabled = false
command = "vibe-heal"

[build]
commands = ["pnpm build", "pnpm lint", "pnpm test"]  # required if build.workers ever consumed; empty list is valid (no gate)
workers = 1
```
`plan.workspace_dir` and `plan.phase_dir` are **required, no implicit default**. Per §11 #4's resolution that the tool "never assumes it can create a `docs/` tree," an absent value is a `ConfigError`, not a fallback to `"docs"`. `agent.backend` defaults to `"claude"` when the `[agent]` section or the `backend` key is absent. When present, it must be `"claude"` or `"opencode"`. Any other value is a `ConfigError`. This mirrors pr-review's backend validation exactly. Dataclasses: `AgentConfig`, `PlanConfig`, `ReviewConfig`, `VibeHealConfig`, `BuildConfig`, composed into one `SpecPrismFlowConfig`. `load_config(path: Path) -> SpecPrismFlowConfig` is the sole entry point. A missing config file is a `ConfigError`, not a default-config fallback.

**Open decision** (flagged at the corpus tree-review checkpoint, not yet resolved). Implement with these two fields included: two sibling phases depend on them.
- `BuildConfig` needs a `max_retry_cycles: int` field (default 3), consumed by Phase 06's `RetryBudget`.
- `BuildConfig` needs a `state_dir: Path` field (default `~/.local/share/dotharness/spec-prism-flow/state`, mirroring pr-review's `XDG_DATA`-style convention). Phase 08's `resume_state_path` consumes it.
- `ReviewConfig`/`VibeHealConfig` each need a tool-dir path field (e.g. `ReviewConfig.tool_dir`, `VibeHealConfig.tool_dir`, defaulting to `~/.harness/tools/pr-review` for `ReviewConfig` and to a vibe-heal install path for `VibeHealConfig`). Phase 09's `uv run --project <dir> ...` invocations consume these fields.
- `ReviewConfig` needs a `harness_config: Path` field — **required, no implicit default** (same "never assume a path" rule as `plan.workspace_dir`/`plan.phase_dir`). It is the target project's own per-project harness config file (e.g. this repo's `.harness-spec_prism_flow.toml`). Phase 09's `harness run --config <harness_config> <subcommand>` invocations consume it.
- A top-level `[harness]` section needs a `knowledge_dir` field (default `~/.harness/knowledge`, mirroring pr-review's `harness.knowledge_dir`). Phase 03's `plan draft-requirements` uses it to locate `requirements-doc-drafting-prompt.md` without hardcoding its path.
- The config filename is `.spec-prism-flow.toml`, by direct analogy to pr-review's `.harness.toml`. It is discovered from the target project's root the same way: a `--config` override, else `./.spec-prism-flow.toml`.

### 7.2 Phase-file parse/render (`spec_prism_flow/phase_file.py`)
`PhaseFile` frozen dataclass: `number: int`, `name: str` (slug), `scope: list[str]`, `requirements: str` (raw Markdown body, unparsed further — preserves verbatim-quoting), `acceptance_criteria: list[str]`, `manual_test_checklist: list[str]`, `depends_on: str`.
`parse_phase_file(path: Path) -> PhaseFile` requires exactly five `##`-level headers, in this exact order: `Scope`, `Requirements`, `Acceptance criteria`, `Manual test checklist`, `Depends on`. A missing, extra, reordered, or misspelled header raises `PhaseFileError` naming the problem. The `Scope`, `Acceptance criteria`, and `Manual test checklist` bodies are bullet lists parsed to `list[str]` (bullet marker stripped, order preserved). `Depends on` is the section's trimmed raw text — a single predecessor statement, e.g. `"Phase 9 merged."` or `"None (first phase)."`.
`render_phase_file(phase: PhaseFile) -> str` is the exact inverse, producing the same five-section order.
`phase_file_name(number: int, name: str) -> str` returns `f"{number:02d}-{name}-leaf.md"`. The module also exposes the regex `^(\d{2})-([a-z0-9-]+)-leaf\.md$` for callers (draft-phases, build). Callers use it to identify finished leaf files versus any other file that might exist in the phase directory (e.g. chunk-level debug drafts decompose may persist). This module parses only files matching the `-leaf.md` pattern. It does not parse any other file.

### 7.3 Graph (`spec_prism_flow/graph.py`)
`Graph` frozen dataclass: `nodes: list[str]` (phase-file stems, e.g. `"01-config-and-phase-file-leaf"`) and `edges: list[tuple[str, str]]`. An edge is written `[dependent, dependency]`: `[X, Y]` means "X depends on Y" — same direction as a phase file's own `Depends on` field. `load_graph(path) -> Graph` and `write_graph(graph, path) -> None` round-trip `graph.json` (`{"nodes": [...], "edges": [[dependent, dependency], ...]}`).
`validate_graph(graph: Graph, phase_files: list[PhaseFile]) -> list[str]` returns a list of violation strings (empty = valid), checking, in order:
1. Every phase file has exactly one corresponding graph node and vice versa (no orphans either direction).
2. The graph is acyclic (report the cycle's node sequence if not).
3. Disjoint-scope invariant: for any two nodes with no path between them in the graph, their `PhaseFile.scope` lists must not share any entry — violation names the two leaves and the overlapping entry.
4. Linear-chain coverage: for each phase *N* > 1, extract phase-number references from its `Depends on` free text via the regex `Phase\s+(\d+)` (case-insensitive). If *N*−1 appears among the extracted numbers, the corresponding `[N, N−1]` edge must be present in `graph.edges`. A violation names the missing edge. The check fires only when `Depends on` actually names *N*−1. It does not assume every phase depends on its numeric predecessor: phases starting an independent feature (§7.3 item 3) legitimately depend on a different phase number (e.g. the first phase of a second feature naming phase 01, not its own numeric predecessor).
(Item 4's caller is `plan review`, owned by a later phase — this phase only supplies the check function.)

### 7.4 Sizing (`spec_prism_flow/sizing.py`)
Pure functions, no I/O.

- `check_file_scope(paths: list[str]) -> SizingResult` — bands: `in_band` true for 5–10, `over_ceiling` true above 15 (hard ceiling), and 11–15 or under 5 flagged-but-not-failed.
- `check_word_count(text: str) -> SizingResult` — `in_band` true for 500–1500. `SizingResult` also carries the raw count and a `note` field, populated when the count falls outside the full observed Neighboku range (532–2100) — a human-reviewable flag, not a hard failure.
- Both functions are advisory. Callers decide what to do with an out-of-band result. This module never raises or blocks on a sizing violation.

## Acceptance criteria
- `load_config` raises `ConfigError` naming the specific missing or invalid field, in each of these cases: a missing config file, a missing `repo`-equivalent required field, `plan.workspace_dir` or `plan.phase_dir` absent, or `agent.backend` present but set to anything other than `"claude"` or `"opencode"`.
- `load_config` never silently defaults `plan.workspace_dir`/`plan.phase_dir` — both are required. `agent.backend` is the one exception: when the `[agent]` section or `backend` key is absent, `AgentConfig.backend` resolves to `"claude"` with no error.
- `BuildConfig` includes `max_retry_cycles: int` (default 3) and `state_dir: Path` (default `~/.local/share/dotharness/spec-prism-flow/state`).
- `ReviewConfig` and `VibeHealConfig` each include a tool-dir path field.
- `ReviewConfig` includes a required `harness_config: Path` field (no default). When `review.enabled` is `True` and the field is absent, `load_config` raises `ConfigError` naming `review.harness_config`.
- The `[harness]` section includes `knowledge_dir` (default `~/.harness/knowledge`).
- `parse_phase_file` round-trips through `render_phase_file` byte-for-byte for a well-formed five-section file. It raises `PhaseFileError` for a file missing any of the five headers, with headers reordered, or with an extra unexpected `##` section.
- `phase_file_name(1, "config-and-phase-file")` returns `"01-config-and-phase-file-leaf.md"`. The exposed regex matches that filename and rejects a filename lacking the `-leaf` suffix.
- `Graph`/`load_graph`/`write_graph` round-trip `graph.json` with `edges` as `[dependent, dependency]` pairs, exactly as written.
- `validate_graph` returns all four violation types correctly on constructed fixtures: an orphaned node, a 2-cycle, two disjoint-scope-but-unlinked leaves sharing a file, and a missing linear-chain edge — and returns an empty list for a valid graph.
- `check_file_scope`/`check_word_count` correctly classify in-band, out-of-band-but-observed-range, and over-ceiling inputs without raising.
- `uv run pytest` passes for all four test files. `ruff check` and `ty` (or whatever lint/type-check the project's own `pyproject.toml` declares) pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/test_config.py -v` and confirm every case above passes, including the `ConfigError` message-content assertions.
- Run `uv run pytest tests/test_phase_file.py -v` and confirm the round-trip and all four malformed-header cases pass.
- Run `uv run pytest tests/test_graph.py -v` and confirm all four `validate_graph` violation types plus the valid-graph pass.
- Run `uv run pytest tests/test_sizing.py -v` and confirm in-band/out-of-band/over-ceiling classification.
- Manually construct a minimal `.spec-prism-flow.toml` on disk and confirm `load_config` loads it without error. Then delete `plan.workspace_dir` from it and confirm `load_config` raises a `ConfigError` naming that field.
- Remove the `[agent]` section entirely from a minimal config and confirm `load_config` succeeds with `AgentConfig.backend == "claude"`. Then set `backend = "bogus"` and confirm `load_config` raises a `ConfigError` naming `agent.backend`.
- Confirm no unhandled exceptions or stack traces appear in any of the above — every error path surfaces a clean, named `ConfigError`/`PhaseFileError`.

## Depends on
- None (first phase).
