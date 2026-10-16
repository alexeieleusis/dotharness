# Sisyphus sizing rules

Single source for leaf sizing bands. Read this before sizing or splitting a unit of work.

## Bands

- File scope: 5 to 10 files (hard ceiling about 15).
- Session budget: roughly 150k to 200k tokens of session budget per leaf.
- Neighboku corpus: 500 to 1500 word band (observed range 532 to 2100).

## Forced split

`decompose` forces a split when a leaf is over 15 files or over 1500 words, up to --depth-cap (default 4).

## Source

These bands come from `tools/spec-prism-flow/docs/requirements.md`, section 9 ("Non-functional requirements"), and from `lifecycle-cheat-sheet.md`. Consult those documents for rationale and any further detail; this file does not restate numbers beyond the bands above.
