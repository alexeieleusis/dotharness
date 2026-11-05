# Config format recommendation

This page recommends a file format for each section of the `pr_review`
configuration and states how secret-bearing fields should be handled. It is a
**recommendation only**. Nothing here is applied: no code, parser, default or
existing config file changes because of this page. Any change that follows from
it is a separate piece of work.

The shapes below come from the config dataclasses in
`src/dotharness/pr_review/config.py`. No real config files were inspected.
Files are referred to by name pattern only: `.harness*.toml`, `.env*`,
`.sisyphus*.toml`. For the current keys and defaults, see
[Configuration](configuration.md).

## Summary

| Section | Dataclass | Recommended format | Change from today |
|---|---|---|---|
| `[harness]` | `HarnessSection` | TOML table; secrets move out of `env` | No format change; `env` usage tightened |
| `[repo]` | `RepoConfig` | TOML table of paths | No |
| `[[repo.subdir]]` | `SubDir` | TOML array of tables | No |
| `[vibe_heal]` | `VibehealConfig` | TOML table | No |
| `[focused_review]` | `FocusedReviewConfig` | TOML table | No |
| `[address_comments]` | `AddressCommentsConfig` | TOML table | No |
| `[regret_review]` | `RegretReviewConfig` | TOML table | No |
| `[local_review]` | `LocalReviewConfig` | TOML table | No |
| `[auto_approve]` | `AutoApproveConfig` | TOML table | No |
| `[ci_fix]` | `CiFixConfig` | TOML table | No |
| `[merge]` | `MergeConfig` | TOML table | No |

The one recommendation that changes practice is about secrets, in
[Secret handling](#secret-handling).

## Per-section recommendations

### `[harness]`

- **Types in use:** `backend: str`, `gh_token_cmd: str`,
  `backend_timeout_seconds: int`, `knowledge_dir: Path`,
  `path_prepend: list[str]`, `env: dict[str, str]`,
  `review_knowledge_file: Path | None`.
- **Recommended format:** keep as a TOML table.
- **Reason:** the section is scalars, one short list and one flat string map.
  TOML types these directly (strings, integers, arrays, inline or sub-tables).
  The only concern is `env`, which invites secrets; see below.
- **Change from today:** no format change. Stop putting secrets in `env`.

### `[repo]`

- **Types in use:** `name: str`, `working_dir: Path`,
  `opencode_dir: Path | None`.
- **Recommended format:** keep as a TOML table of paths.
- **Reason:** three scalar values with no nesting. Paths are plain strings in
  TOML.
- **Change from today:** none.

### `[[repo.subdir]]`

- **Types in use:** `path: str`, `pre_commands: list[PreCommand]` (each with
  `cmd: str`, `critical: bool`), `coverage: bool`, `timeout: int`,
  `env_file: str | None`.
- **Recommended format:** keep as a TOML array of tables, with
  `[[repo.subdir.pre_commands]]` or an inline array for the nested list.
- **Reason:** this is a repeated record that contains a nested list of records.
  Flat key/value formats (dotenv, INI) cannot express repetition or nesting
  without ad hoc key-naming schemes. TOML and YAML both can; TOML is already
  in use and already validated.
- **Change from today:** none. `env_file` is a path, not contents; see below.

### `[vibe_heal]`

- **Types in use:** `enabled: bool`, `python: str`,
  `authors: str | list[str]`, `vibe_heal_timeout: int`,
  `vibe_heal_post_timeout: int`, `min_reanalysis_interval_hours: float`,
  `prune_projects_enabled: bool`, `prune_older_than_minutes: int`,
  `prune_projects_timeout: int`.
- **Recommended format:** keep as a TOML table.
- **Reason:** flat scalars. The `str | list[str]` union for `authors` is
  natural in TOML (`"*"` or an array of strings).
- **Change from today:** none.

### `[focused_review]`

- **Types in use:** `enabled: bool`, `vibe_types_repo: Path`.
- **Recommended format:** keep as a TOML table.
- **Reason:** a flag and a path.
- **Change from today:** none.

### `[address_comments]`

- **Types in use:** `enabled: bool`,
  `trusted_commenters: str | list[str]`.
- **Recommended format:** keep as a TOML table.
- **Reason:** a flag and a string-or-list value, both direct in TOML.
- **Change from today:** none.

### `[regret_review]`

- **Types in use:** `enabled: bool`, `max_diff_lines: int`,
  `authors: str | list[str]`, `regret_review_timeout: int`.
- **Recommended format:** keep as a TOML table.
- **Reason:** flat scalars and one string-or-list value.
- **Change from today:** none.

### `[local_review]`

- **Types in use:** `base: str | None`, `output_dir: str | None`.
- **Recommended format:** keep as a TOML table.
- **Reason:** two optional strings. Absence of a key maps to `None`.
- **Change from today:** none.

### `[auto_approve]`

- **Types in use:** `enabled: bool`, `viewed_max_lines: int`,
  `approve_max_lines: int`.
- **Recommended format:** keep as a TOML table.
- **Reason:** a flag and two integer thresholds.
- **Change from today:** none.

### `[ci_fix]`

- **Types in use:** `enabled: bool`, `max_fix_attempts: int`.
- **Recommended format:** keep as a TOML table.
- **Reason:** a flag and an integer.
- **Change from today:** none.

### `[merge]`

- **Types in use:** `enabled: bool`, `require_all_files_viewed: bool`,
  `max_changed_lines: int`.
- **Recommended format:** keep as a TOML table.
- **Reason:** two flags and an integer threshold.
- **Change from today:** none.

## Secret handling

Three fields touch secrets or secret-bearing files. The config file itself
(`.harness*.toml`, `.sisyphus*.toml`) should be treated as non-secret: safe to
read in a screen share and safe to copy between machines.

### `gh_token_cmd`

- **Today:** `HarnessSection.gh_token_cmd: str`, default `"gh auth token"`
  (marked `# noqa: S105` because the name looks like a password to the linter).
  The config stores a **command that yields the token**, never the token.
- **Recommendation:** keep this pattern.
- **Rule:** the command string must not embed a literal token. Values such as
  `echo ghp_...` or `printf %s <token>` defeat the purpose. Acceptable commands
  read from a credential helper, such as `gh auth token`, a keychain lookup or
  a secrets-manager CLI.

### `[harness.env]`

- **Today:** `HarnessSection.env: dict[str, str]`, written inline in the config
  file.
- **Never place here:** tokens, API keys, passwords, private keys or any
  value whose disclosure would matter. Everything in this table is plain text
  in a file that may be backed up, synced or pasted into an issue.
- **Fine to place here:** non-secret process settings, such as feature flags,
  locale, log level or non-sensitive URLs.
- **Recommended alternatives for secrets:**
    - Reference an external env file kept outside the config, with the
      permission expectations below. For vibe-heal credentials this is already
      possible through `[[repo.subdir]] env_file`.
    - Or provide the variable in the process environment of the scheduler
      (shell profile, launchd or cron environment), so it never lands in the
      config file.
- **Change from today:** a documentation and review rule only. The parser still
  accepts any string; enforcement (for example a validate-time warning on
  token-shaped keys) would be separate work.

### `[[repo.subdir]]` `env_file`

- **Today:** `SubDir.env_file: str | None`. `_parse_env_file` raises
  `ConfigError` when the value is not a string. The field holds a **path to a
  file**, not file contents.
- **Recommendation:** keep it a path. Expectations for the referenced file:
    - Store it outside the repository working tree, or inside it only when
      the file is git-ignored. Cover the `.env*` pattern in the global or
      repo `.gitignore`.
    - Restrict permissions to the owning user (mode `0600`, and `0700` on the
      containing directory).
    - Do not commit, paste into tickets or copy between machines in clear
      text.
- **Migration note:** the migration to `dotharness.pr_review` does not read,
  copy or rewrite the contents of any such file. Only the path field and its
  validation move with the code.

## Out of scope

- Applying any format or validation change described above.
- Migrating or rewriting existing config files.
- Changing how secrets are stored in any real environment.
