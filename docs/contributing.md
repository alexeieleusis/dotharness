# Contributing

## GitHub Pages

Pages is built and deployed by `.github/workflows/docs.yml` through Actions. Old
multi-site URLs are not redirected.

## Migration ledger rule

[`docs/migration-ledger.md`](migration-ledger.md) records the migration history.
The ledger is frozen at I-6.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run zensical build -s
```

Unset `VIRTUAL_ENV` if `uv run` complains about a mismatched environment.

## Shared async layer

`dotharness.subprocess_runner` and `dotharness.ai_backend` are the shared way to run
subprocesses and launch `claude` or `opencode`. The comparison of the old copies, the
decisions and the contract for I-3 to I-5 are in
[`design/shared-async-layer.md`](design/shared-async-layer.md).

## Finding and decision IDs

I-6 findings are `F-<area>-<n>` and carry a verdict (`apply now`, `defer`, `no action (justified)`). Triage table, I-7
hand-off list and fixture decision IDs are in `docs/design/shared-async-layer.md` (sections k to m). Decisions from D-13 on
record any fixture edit or any later-added item; no apply-now PR merges before the user approves the triage table (D-1).

## Tooling provenance

These choices were made in I-1 and are recorded here because no other file records
them.

### Ruff version

Ruff 0.15.7 and 0.17.3 were run over pr-review, spec-prism-flow and sisyphus at
`cb0efc1` (no findings). They were also run over vibe-heal `src/vibe_heal` at
`9363868` (78 findings: TRY003 33, TRY300 16, UP041 11, B008 10, UP017 6,
UP042 1, UP035 1).
`ruff==0.17.3` is pinned, in the dev group and in `.pre-commit-config.yaml`. The
ignore list stays `E501` and `E731` (plus `S101` in tests). TRY003, TRY300, B008 and
the UP rules were not added to it. The vibe-heal findings are left for I-2, when vibe-heal
is migrated.

### Cookiecutter answers

The project layout follows the cookiecutter-uv template with these answers:

- no Dockerfile
- no devcontainer
- no PyPI publishing
- no mypy (`ty` is the type checker)

The files were written by hand. Cookiecutter was not run.

### Excludes for vendor

Ruff, `ty` and pre-commit exclude `vendor`.

### Coverage policy

The project-wide branch coverage floor is 91%. It is `fail_under` in
`[tool.coverage.report]` of `pyproject.toml`, enforced by `make test` and in CI.
The measured value when it was set was 91.47%. The target is 91% with a Codecov threshold of 0.5%. `fail_under = 91` in
`[tool.coverage.report]` is never lowered.
