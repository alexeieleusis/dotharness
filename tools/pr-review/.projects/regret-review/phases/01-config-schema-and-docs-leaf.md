## Scope
- harness/config.py
- tests/test_config.py
- docs/configuration.md
- docs/commands/index.md
- README.md

## Requirements
# Requirements: `[regret_review]` Config Schema and Docs

## 1. Purpose / origin

This leaf covers only the config surface: the `RegretReviewConfig` dataclass, its
`.harness.toml` parsing, and the matching `docs/configuration.md` schema entry. It is
a leaf of the `regret-review` decomposition tree (`requirements.md` §7.5, §10). Its
shape comes only from the three existing sibling config sections in `harness/config.py`
(`VibehealConfig`, `FocusedReviewConfig`, and `AddressCommentsConfig`).

## 2. Problem statement

Every other leaf of this project (blame/PR resolution, comment matching, runner
wiring) needs to read whether the pass is enabled and its three tunables
(`max_diff_lines`, `authors`, `regret_review_timeout`). Until this leaf lands, nothing
else can read `config.regret_review`. `docs/configuration.md` would also silently
drift out of sync with the actual schema once any other leaf starts consuming it.

## 3. Goals

- **G1.** `harness/config.py` gains a `RegretReviewConfig` dataclass with fields
  `enabled: bool = False`, `max_diff_lines: int = 50`, `authors: str | list[str] = "*"`,
  and `regret_review_timeout: int = 300`. That is the exact shape `requirements.md`
  §7.5 already specifies verbatim.
- **G2.** `HarnessConfig` gains a `regret_review: RegretReviewConfig` field
  (`field(default_factory=RegretReviewConfig)`), alongside its existing
  `vibe_heal`/`focused_review`/`address_comments` fields.
- **G3.** `load_config` parses an optional `[regret_review]` TOML table into this
  dataclass, using the same read-with-default pattern every other section already
  uses, for example `vh.get("enabled", False)` for `[vibe_heal]`. It introduces no new
  parsing idiom.
- **G4.** `docs/configuration.md` gains a `### \`[regret_review]\`` section and a field
  table in the same format as the existing `### \`[vibe_heal]\`` section. It also gains
  an entry in the "Full example" TOML block at the bottom of the file.

## 4. Non-goals

- This leaf does not implement anything that *reads* `config.regret_review` at
  runtime. That is every downstream leaf's job. It only makes the field exist and
  parse correctly.
- This leaf adds no new `harness validate` check for this section, for example a
  bounds check for `max_diff_lines > 0`. `requirements.md` doesn't ask for one. None
  of the three sibling sections has a bespoke validate-time check, either. The
  existing `harness validate` checks (the "Validation" section of
  `docs/configuration.md`) are unchanged.
- This leaf does not choose different defaults. `requirements.md` §7.5 already
  settled them. This leaf only implements that decision.

## 5. Glossary

See `requirements.md` §5 for the full definitions of `bugfix-shaped PR` and the
pass's own vocabulary. This leaf only needs `RegretReviewConfig`, the dataclass it
introduces, which matches the code block already given in `requirements.md` §7.5.

## 6. Feature/component breakdown

This leaf is a single feature: a dataclass, its parser branch, and a docs section.
No table is needed.

## 7. Detailed functional requirements

- Add to `harness/config.py`, directly below `AddressCommentsConfig`:

  ```python
  @dataclass
  class RegretReviewConfig:
      enabled: bool = False
      max_diff_lines: int = 50
      authors: str | list[str] = "*"
      regret_review_timeout: int = 300
  ```

- Add `regret_review: RegretReviewConfig = field(default_factory=RegretReviewConfig)`
  to `HarnessConfig`.
- In `load_config`, add a block mirroring the existing `[vibe_heal]`/
  `[focused_review]` parsing exactly:

  ```python
  rr = data.get("regret_review", {})
  raw_rr_authors = rr.get("authors", "*")
  regret_review = RegretReviewConfig(
      enabled=rr.get("enabled", False),
      max_diff_lines=rr.get("max_diff_lines", 50),
      authors=raw_rr_authors if isinstance(raw_rr_authors, str) else list(raw_rr_authors),
      regret_review_timeout=rr.get("regret_review_timeout", 300),
  )
  ```

  Pass `regret_review=regret_review` into the final `HarnessConfig(...)`. Handle the
  str-or-list `authors` field the way `address_comments` handles its
  `trusted_commenters` field.
- In `docs/configuration.md`, add a `### \`[regret_review]\`` section directly after
  `### \`[address_comments]\``, with a field table for all four fields: type,
  default, and description. Reuse the four descriptions already written in
  `requirements.md` §7.5, nearly verbatim. Also add a commented-out
  `[regret_review]` block to the "Full example" TOML at the bottom of the file,
  matching how `[vibe_heal]`/`[focused_review]`/`[address_comments]` are already
  shown there.

## 8. Non-functional requirements

This leaf adds no new non-functional requirements beyond what every other config
section already has. Parsing must not raise for a `.harness.toml` that omits
`[regret_review]` entirely. Every field must have a working default, per G1.

## 9. Out-of-scope / explicit exclusions

No changes to `harness/cli.py`'s `validate` or `init` commands are in scope. No such
changes result from `RegretReviewConfig` existing. The `harness init` template and
the `harness validate` checks are both driven by the docs/example block and the
generic per-field defaults, not a per-section special case.

## 10. System/tool shape

- **Files touched:** `harness/config.py` (edit), `tests/test_config.py` (edit: new
  tests in the style of the existing `test_vibe_heal_defaults`/`test_build_parsed`
  tests, covering defaults and a fully-populated `[regret_review]` table),
  `docs/configuration.md` (edit), `docs/commands/index.md` (edit: a one-line mention
  that a `regret-review` pass now exists, alongside the existing per-command list),
  and `README.md` (edit: the top-level paragraph that summarizes the features gains
  one clause listing this new pass alongside self-review/design-review/traceability-review).
- **Tests:** unit tests only. No fixtures beyond what `tests/test_config.py` already
  uses (in-memory TOML strings via `tmp_path`).

## 11. Open decisions log

None. This leaf implements `requirements.md` §7.5/§11.3 verbatim. Those sections are
already fully resolved.

## 12. Next step

Once this leaf's phase merges, every other leaf in this project can read
`config.regret_review`. In particular, the blame/PR-resolver leaf reads
`max_diff_lines`/`authors`, and both runner-wiring leaves read `enabled`.

## Acceptance criteria
- `harness/config.py` gains a `RegretReviewConfig` dataclass with fields `enabled: bool = False`, `max_diff_lines: int = 50`, `authors: str | list[str] = "*"`, and `regret_review_timeout: int = 300`. That is the exact shape `requirements.md` §7.5 already specifies verbatim.
- `HarnessConfig` gains a `regret_review: RegretReviewConfig` field (`field(default_factory=RegretReviewConfig)`), alongside its existing `vibe_heal`/`focused_review`/`address_comments` fields.
- `load_config` parses an optional `[regret_review]` TOML table into this dataclass, using the same read-with-default pattern every other section already uses, for example `vh.get("enabled", False)` for `[vibe_heal]`. It introduces no new parsing idiom.
- `docs/configuration.md` gains a `### \`[regret_review]\`` section and a field table in the same format as the existing `### \`[vibe_heal]\`` section. It also gains an entry in the "Full example" TOML block at the bottom of the file.

## Manual test checklist
- Manually verify: `harness/config.py` gains a `RegretReviewConfig` dataclass with fields `enabled: bool = False`, `max_diff_lines: int = 50`, `authors: str | list[str] = "*"`, and `regret_review_timeout: int = 300`. That is the exact shape `requirements.md` §7.5 already specifies verbatim.
- Manually verify: `HarnessConfig` gains a `regret_review: RegretReviewConfig` field (`field(default_factory=RegretReviewConfig)`), alongside its existing `vibe_heal`/`focused_review`/`address_comments` fields.
- Manually verify: `load_config` parses an optional `[regret_review]` TOML table into this dataclass, using the same read-with-default pattern every other section already uses, for example `vh.get("enabled", False)` for `[vibe_heal]`. It introduces no new parsing idiom.
- Manually verify: `docs/configuration.md` gains a `### \`[regret_review]\`` section and a field table in the same format as the existing `### \`[vibe_heal]\`` section. It also gains an entry in the "Full example" TOML block at the bottom of the file.

## Depends on
- None (first phase).
