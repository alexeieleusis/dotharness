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

Excerpt, chunk A-1's mini-requirements doc §7 (Detailed functional requirements), quoted verbatim:

### 7.1 Config (`spec_prism_flow/config.py`)
A per-target-project TOML, analogous to `.harness.toml`, with these sections and required-vs-optional fields:
```toml
[agent]
backend = "claude"          # "claude" or "opencode"; required, no default guessed silently — but "claude" is
                             # explicitly named as the resolved default (§10), so absence of this key is not an error.

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
`plan.workspace_dir` and `plan.phase_dir` are **required, no implicit default** — per §11 #4's resolution that the tool "never assumes it can create a `docs/` tree," an absent value is a `ConfigError`, not a fallback to `"docs"`. `agent.backend` must be one of `"claude"`/`"opencode"`; any other value is a `ConfigError` (mirrors pr-review's backend validation exactly). Dataclasses: `AgentConfig`, `PlanConfig`, `ReviewConfig`, `VibeHealConfig`, `BuildConfig`, composed into one `SpecPrismFlowConfig`. `load_config(path: Path) -> SpecPrismFlowConfig` is the sole entry point; a missing config file is a `ConfigError`, not a default-config fallback.

**Open decision (flagged at the corpus tree-review checkpoint, not yet resolved — implement with these two fields included, since two sibling phases depend on them):**
- `BuildConfig` needs a `max_retry_cycles: int` field (default 3), consumed by Phase 06's `RetryBudget`.
- `ReviewConfig`/`VibeHealConfig` need tool-dir path fields (e.g. `ReviewConfig.tool_dir`, `VibeHealConfig.tool_dir`, defaulting to `~/.harness/tools/pr-review` / a vibe-heal install path), consumed by Phase 09's `uv run --project <dir> ...` invocations.
- A top-level `[harness]` section needs a `knowledge_dir` field (default `~/.harness/knowledge`, mirroring pr-review's `harness.knowledge_dir`), consumed by Phase 03's `plan draft-requirements` to locate `requirements-doc-drafting-prompt.md` without hardcoding its path.
- The config filename is resolved here by direct analogy to pr-review's `.harness.toml`: `.spec-prism-flow.toml`, discovered from the target project's root the same way (`--config` override, else `./.spec-prism-flow.toml`).

### 7.2 Phase-file parse/render (`spec_prism_flow/phase_file.py`)
`PhaseFile` frozen dataclass: `number: int`, `name: str` (slug), `scope: list[str]`, `requirements: str` (raw Markdown body, unparsed further — preserves verbatim-quoting), `acceptance_criteria: list[str]`, `manual_test_checklist: list[str]`, `depends_on: str`.
`parse_phase_file(path: Path) -> PhaseFile` requires exactly five `##`-level headers, in this exact order: `Scope`, `Requirements`, `Acceptance criteria`, `Manual test checklist`, `Depends on` — any missing, extra, reordered, or misspelled header raises `PhaseFileError` naming the problem. `Scope`/`Acceptance criteria`/`Manual test checklist` bodies are bullet lists parsed to `list[str]` (bullet marker stripped, order preserved); `Depends on` is the section's trimmed raw text (single predecessor statement, e.g. `"Phase 9 merged."` or `"None (first phase)."`).
`render_phase_file(phase: PhaseFile) -> str` is the exact inverse, producing the same five-section order.
`phase_file_name(number: int, name: str) -> str` returns `f"{number:02d}-{name}-leaf.md"`. A regex `^(\d{2})-([a-z0-9-]+)-leaf\.md$` is exposed for callers (draft-phases, build) to identify finished leaf files versus any other file that might exist in the phase directory (e.g. chunk-level debug drafts decompose may persist) — this module does not parse those, only files matching the `-leaf.md` pattern.

### 7.3 Graph (`spec_prism_flow/graph.py`)
`Graph` frozen dataclass: `nodes: list[str]` (phase-file stems, e.g. `"01-config-and-phase-file-leaf"`), `edges: list[tuple[str, str]]` (`[dependent, dependency]` — an edge `[X, Y]` means "X depends on Y", same direction as a phase file's own `Depends on` field). `load_graph(path) -> Graph` / `write_graph(graph, path) -> None` round-trip `graph.json` (`{"nodes": [...], "edges": [[dependent, dependency], ...]}`).
`validate_graph(graph: Graph, phase_files: list[PhaseFile]) -> list[str]` returns a list of violation strings (empty = valid), checking, in order:
1. Every phase file has exactly one corresponding graph node and vice versa (no orphans either direction).
2. The graph is acyclic (report the cycle's node sequence if not).
3. Disjoint-scope invariant: for any two nodes with no path between them in the graph, their `PhaseFile.scope` lists must not share any entry — violation names the two leaves and the overlapping entry.
4. Linear-chain coverage: for every consecutive pair in DFS numeric order (phase *N*'s `Depends on` naming phase *N−1*), the corresponding `[N, N−1]` edge must be present in `graph.edges` — violation names the missing edge.
(Item 4's caller is `plan review`, owned by a later phase — this phase only supplies the check function.)

### 7.4 Sizing (`spec_prism_flow/sizing.py`)
Pure functions, no I/O: `check_file_scope(paths: list[str]) -> SizingResult` — bands: `in_band` true for 5–10, `over_ceiling` true above 15 (hard ceiling), otherwise flagged-but-not-failed for 11–15 or under 5. `check_word_count(text: str) -> SizingResult` — `in_band` true for 500–1500; `SizingResult` also carries the raw count and a `note` field populated when the count falls outside the full observed Neighboku range (532–2100), for a human-reviewable flag rather than a hard failure. Both are advisory: callers decide what to do with an out-of-band result — this module never itself raises/blocks on a sizing violation.

## Acceptance criteria
- `load_config` raises `ConfigError` naming the specific missing/invalid field for: a missing config file, a missing `repo`-equivalent required field, `plan.workspace_dir`/`plan.phase_dir` absent, or `agent.backend` set to anything other than `"claude"`/`"opencode"`.
- `load_config` never silently defaults `plan.workspace_dir`/`plan.phase_dir` — both are required.
- `BuildConfig` includes `max_retry_cycles: int` (default 3); `ReviewConfig`/`VibeHealConfig` include a tool-dir path field each; a `[harness]` section includes `knowledge_dir` (default `~/.harness/knowledge`).
- `parse_phase_file` round-trips through `render_phase_file` byte-for-byte for a well-formed five-section file, and raises `PhaseFileError` for a file missing any of the five headers, with headers reordered, or with an extra unexpected `##` section.
- `phase_file_name(1, "config-and-phase-file")` returns `"01-config-and-phase-file-leaf.md"`; the exposed regex matches that filename and rejects a filename lacking the `-leaf` suffix.
- `Graph`/`load_graph`/`write_graph` round-trip `graph.json` with `edges` as `[dependent, dependency]` pairs, exactly as written.
- `validate_graph` returns all four violation types correctly on constructed fixtures: an orphaned node, a 2-cycle, two disjoint-scope-but-unlinked leaves sharing a file, and a missing linear-chain edge — and returns an empty list for a valid graph.
- `check_file_scope`/`check_word_count` correctly classify in-band, out-of-band-but-observed-range, and over-ceiling inputs without raising.
- `uv run pytest` passes for all four test files; `ruff check`/`ty` (or whatever lint/type-check the project's own `pyproject.toml` declares) pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/test_config.py -v` and confirm every case above passes, including the `ConfigError` message-content assertions.
- Run `uv run pytest tests/test_phase_file.py -v` and confirm the round-trip and all four malformed-header cases pass.
- Run `uv run pytest tests/test_graph.py -v` and confirm all four `validate_graph` violation types plus the valid-graph pass.
- Run `uv run pytest tests/test_sizing.py -v` and confirm in-band/out-of-band/over-ceiling classification.
- Manually construct a minimal `.spec-prism-flow.toml` on disk and confirm `load_config` loads it without error; then delete `plan.workspace_dir` from it and confirm `load_config` raises a `ConfigError` naming that field.
- Confirm no unhandled exceptions or stack traces appear in any of the above — every error path surfaces a clean, named `ConfigError`/`PhaseFileError`.

## Depends on
- None (first phase).
