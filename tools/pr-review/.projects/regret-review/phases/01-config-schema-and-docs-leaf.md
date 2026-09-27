## Scope
- harness/config.py
- tests/test_config.py
- docs/configuration.md
- docs/commands/index.md
- README.md

## Requirements
# Requirements: `[regret_review]` Config Schema and Docs

## 1. Purpose / origin

This is a leaf of `regret-review`'s decomposition tree (`requirements.md` §7.5, §10),
covering only the config surface: the `RegretReviewConfig` dataclass, its
`.harness.toml` parsing, and the matching `docs/configuration.md` schema entry. It is
not derived from a reference implementation beyond the three existing sibling config
sections (`VibehealConfig`, `FocusedReviewConfig`, `AddressCommentsConfig` in
`harness/config.py`), which this leaf mirrors exactly in shape.

## 2. Problem statement

Every other leaf of this project (blame/PR resolution, comment matching, runner
wiring) needs a way to read whether the pass is enabled and its three tunables
(`max_diff_lines`, `authors`, `regret_review_timeout`). Without this leaf landing
first, nothing else has anything to read `config.regret_review` from, and
`docs/configuration.md` would silently drift out of sync with the actual schema the
moment any other leaf starts consuming it.

## 3. Goals

- **G1.** `harness/config.py` gains a `RegretReviewConfig` dataclass with fields
  `enabled: bool = False`, `max_diff_lines: int = 50`, `authors: str | list[str] = "*"`,
  `regret_review_timeout: int = 300` — the exact shape `requirements.md` §7.5 already
  specifies verbatim.
- **G2.** `HarnessConfig` gains a `regret_review: RegretReviewConfig` field
  (`field(default_factory=RegretReviewConfig)`), alongside its existing
  `vibe_heal`/`focused_review`/`address_comments` fields.
- **G3.** `load_config` parses an optional `[regret_review]` TOML table into this
  dataclass, following the exact same read-with-default pattern every other section
  already uses (e.g. `vh.get("enabled", False)` for `[vibe_heal]`) — no new parsing
  idiom introduced.
- **G4.** `docs/configuration.md` gains a `### \`[regret_review]\`` section, a field
  table in the same format as the existing `### \`[vibe_heal]\`` section, and an entry
  in the "Full example" TOML block at the bottom of that file.

## 4. Non-goals

- Not implementing anything that *reads* `config.regret_review` at runtime — that is
  every downstream leaf's job. This leaf only makes the field exist and parse
  correctly.
- Not adding any new `harness validate` check for this section (e.g. bounds-checking
  `max_diff_lines > 0`). `requirements.md` doesn't ask for one, and none of the three
  sibling sections have bespoke validate-time checks either — `harness validate`'s
  existing checks (`docs/configuration.md`'s "Validation" section) are unchanged.
- Not choosing different defaults than `requirements.md` §7.5 already settled — that
  decision was made and resolved there; this leaf only implements it.

## 5. Glossary

See `requirements.md` §5 for full definitions of `bugfix-shaped PR` and the pass's own
vocabulary; this leaf only needs `RegretReviewConfig`, the dataclass this leaf
introduces, matching the code block already given in `requirements.md` §7.5.

## 6. Feature/component breakdown

Single feature — a dataclass, its parser branch, and a docs section. No table needed.

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

  and pass `regret_review=regret_review` into the final `HarnessConfig(...)`
  construction, matching `address_comments`'s existing `trusted_commenters`
  str-or-list handling for the `authors` field's own type.
- In `docs/configuration.md`, add a `### \`[regret_review]\`` section directly after
  `### \`[address_comments]\``, with a field table covering all four fields (type,
  default, description — reuse the four descriptions already written in
  `requirements.md` §7.5 nearly verbatim), and add a commented-out
  `[regret_review]` block to the file's "Full example" TOML at the bottom, matching
  how `[vibe_heal]`/`[focused_review]`/`[address_comments]` are already shown there.

## 8. Non-functional requirements

None beyond what already applies to every other config section: parsing must not
raise for a `.harness.toml` that omits `[regret_review]` entirely (every field must
have a working default, per G1).

## 9. Out-of-scope / explicit exclusions

Not in scope: any change to `harness/cli.py`'s `validate`/`init` commands beyond what
falls out naturally from `RegretReviewConfig` existing (i.e., none — `harness init`'s
template and `harness validate`'s checks are both driven by the docs/example block and
the generic per-field defaults, not a per-section special case).

## 10. System/tool shape

- **Files touched:** `harness/config.py` (edit), `tests/test_config.py` (edit — new
  tests mirroring the existing `test_vibe_heal_defaults`/`test_build_parsed`-style
  tests, covering defaults and a fully-populated `[regret_review]` table),
  `docs/configuration.md` (edit), `docs/commands/index.md` (edit — a one-line mention
  that a `regret-review` pass now exists, alongside the existing per-command list),
  `README.md` (edit — the top-level feature summary paragraph gains one clause listing
  this new pass alongside self-review/design-review/traceability-review).
- **Tests:** unit tests only, no fixtures beyond what `tests/test_config.py` already
  uses (in-memory TOML strings via `tmp_path`).

## 11. Open decisions log

None — this leaf implements `requirements.md` §7.5/§11.3 verbatim, which is already
fully resolved.

## 12. Next step

Once this leaf's phase merges, every other leaf in this project can read
`config.regret_review` — in particular the blame/PR-resolver leaf (which reads
`max_diff_lines`/`authors`) and both runner-wiring leaves (which read `enabled`).

## Acceptance criteria
- `harness/config.py` gains a `RegretReviewConfig` dataclass with fields `enabled: bool = False`, `max_diff_lines: int = 50`, `authors: str | list[str] = "*"`, `regret_review_timeout: int = 300` — the exact shape `requirements.md` §7.5 already specifies verbatim.
- `HarnessConfig` gains a `regret_review: RegretReviewConfig` field (`field(default_factory=RegretReviewConfig)`), alongside its existing `vibe_heal`/`focused_review`/`address_comments` fields.
- `load_config` parses an optional `[regret_review]` TOML table into this dataclass, following the exact same read-with-default pattern every other section already uses (e.g. `vh.get("enabled", False)` for `[vibe_heal]`) — no new parsing idiom introduced.
- `docs/configuration.md` gains a `### \`[regret_review]\`` section, a field table in the same format as the existing `### \`[vibe_heal]\`` section, and an entry in the "Full example" TOML block at the bottom of that file.

## Manual test checklist
- Manually verify: `harness/config.py` gains a `RegretReviewConfig` dataclass with fields `enabled: bool = False`, `max_diff_lines: int = 50`, `authors: str | list[str] = "*"`, `regret_review_timeout: int = 300` — the exact shape `requirements.md` §7.5 already specifies verbatim.
- Manually verify: `HarnessConfig` gains a `regret_review: RegretReviewConfig` field (`field(default_factory=RegretReviewConfig)`), alongside its existing `vibe_heal`/`focused_review`/`address_comments` fields.
- Manually verify: `load_config` parses an optional `[regret_review]` TOML table into this dataclass, following the exact same read-with-default pattern every other section already uses (e.g. `vh.get("enabled", False)` for `[vibe_heal]`) — no new parsing idiom introduced.
- Manually verify: `docs/configuration.md` gains a `### \`[regret_review]\`` section, a field table in the same format as the existing `### \`[vibe_heal]\`` section, and an entry in the "Full example" TOML block at the bottom of that file.

## Depends on
- None (first phase).
