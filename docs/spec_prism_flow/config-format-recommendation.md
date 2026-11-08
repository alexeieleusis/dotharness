# Config format recommendation

This page recommends a file format for each section of the `spec_prism_flow`
configuration after the plan-side-only migration. It also records what happens
to the sections that are no longer validated. It is a **recommendation only**.
Nothing here is applied: no code, parser, default or existing config file
changes because of this page.

The shapes below come from the config dataclasses in
`src/dotharness/spec_prism_flow/config.py`. No real config files were
inspected. This page refers to files by name pattern only: `.spec-prism-flow*.toml`,
`.harness*.toml`, `.sisyphus*.toml`. For the command-line side, see
[Overview](overview.md).

## Summary

**Recommendation: keep TOML.** The remaining keys are flat scalars and paths.
TOML types them directly. TOML is already in use and validated.

| Section | Keys | Recommended format | Change from today |
|---|---|---|---|
| `[agent]` | `backend` | TOML table | Kept; currently unused |
| `[plan]` | `workspace_dir`, `phase_dir`, `conventions_path` | TOML table of paths | No |
| `[prose_review]` | `enabled`, `backend` | TOML table | No |
| `[harness]` | `knowledge_dir` | TOML table | No |

`[build]`, `[review]` and `[vibe_heal]` are no longer validated by
`spec_prism_flow`. See [Sections no longer validated](#sections-no-longer-validated).

## Per-section recommendations

### `[agent]`

- **Keys:** `backend`.
- **Type in use:** `backend: str`, default `"claude"`. Allowed values are
  `claude` and `opencode`. Any other value raises `ConfigError`.
- **Recommended format:** keep as a TOML table.
- **Reason:** a single string.
- **Change from today:** none to the format. The key is kept but currently
  unused (D6): it is accepted and validated, and no `plan` command reads it.
  The code does not state a reason for keeping it, so this page does not give
  one.

### `[plan]`

- **Keys:** `workspace_dir`, `phase_dir`, `conventions_path`.
- **Types in use:** `workspace_dir: Path` (required), `phase_dir: Path`
  (required), `conventions_path: Path | None` (optional, default none). A
  leading `~` is expanded.
- **Recommended format:** keep as a TOML table of paths.
- **Reason:** three scalar values with no nesting. Paths are plain strings in
  TOML.
- **Change from today:** none.

### `[prose_review]`

- **Keys:** `enabled`, `backend`.
- **Types in use:** `enabled: bool`, default `false`. `backend: str`, default
  `"claude"`, with the same allowed values as `[agent] backend`.
- **Recommended format:** keep as a TOML table.
- **Reason:** a flag and a string, both direct in TOML.
- **Change from today:** none.

### `[harness]`

- **Keys:** `knowledge_dir`.
- **Type in use:** `knowledge_dir: Path`, optional. When absent, the shared
  default knowledge directory is used. A leading `~` is expanded.
- **Recommended format:** keep as a TOML table.
- **Reason:** a single path.
- **Change from today:** none.

## Sections no longer validated

`spec_prism_flow` no longer validates `[build]`, `[review]` and `[vibe_heal]`.
The config loader ignores unknown keys and tables, so an existing config file
that still contains these sections loads without error. The loader does not check their
contents: a malformed `[build]` or `[review]` table no longer fails
`spec_prism_flow`, and the values have no effect on it.

Sisyphus owns `[build]`. Keys under that section are documented by and
validated in sisyphus, not here.

## Out of scope

- Applying any format or validation change described above.
- Migrating or rewriting existing config files.
- Documenting `[build]` keys.
