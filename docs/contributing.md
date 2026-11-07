# Contributing

## Branch rule

The integration branch is `consolidation`. It is created from `main` after I-1
merges.

- I-1 targets `main`.
- I-2 to I-5 target `consolidation`.
- I-6 to I-8 target `main` after I-5 merges.
- `consolidation` lands on `main` as one squash commit. The `main` ruleset (squash
  only, linear history) is not changed.

### Post-merge steps (after I-1 merges)

1. Create `consolidation` from `main` and push it.
2. Open each of the I-2 to I-5 PRs against `consolidation`. The CI workflow has no
   `branches` filter, so it runs for these PRs.
3. After I-5 merges into `consolidation`, squash `consolidation` onto `main`.
4. Check the GitHub Pages setting (see below).

### GitHub Pages

Pages is `legacy` with source `main:/docs`. A root `docs/` directory on `main`
may change what Pages serves. The old `docs.yml` keeps deploying the three tool
sites until I-5 merges. The new workflow only builds the docs (`zensical build -s`).
Correcting the Pages setting needs a repo-admin action and is done after I-5 if
needed. No redirects are added: old URLs return 404 until I-5 merges.

## Migration ledger rule

Every PR for I-2 to I-8 that touches `src/dotharness/` updates
[`docs/migration-ledger.md`](migration-ledger.md) in the same PR. The PR template
has a checkbox for it.

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

## Tooling provenance

These choices were made in I-1 and are recorded here because no other file records
them.

### Ruff version

Ruff 0.15.7 and 0.17.3 were run over pr-review, spec-prism-flow and sisyphus at
`cb0efc1` (no findings) and over vibe-heal `src/vibe_heal` at `9363868` (78
findings: TRY003 33, TRY300 16, UP041 11, B008 10, UP017 6, UP042 1, UP035 1).
`ruff==0.17.3` is pinned, in the dev group and in `.pre-commit-config.yaml`. The
ignore list stays `E501` and `E731` (plus `S101` in tests). TRY003, TRY300, B008 and
the UP rules were not added to it. The vibe-heal findings are left for I-2, when vibe-heal
is migrated.

### Cookiecutter answers

The project layout follows the cookiecutter-uv template with these answers: no
Dockerfile, no devcontainer, no PyPI publishing and no mypy (`ty` is the type
checker). The files were written by hand. Cookiecutter was not run.

### Excludes for tools and vendor

Ruff, `ty` and pre-commit exclude `tools` and `vendor`, so the old tools are not
modified or checked by the new project. Later phases may remove these excludes as
the tools migrate.

### Coverage policy

I-1 enforces no project-wide numeric coverage floor (D-6, staged floor). The
`dotharness` Codecov status is informational. The tools are migrated first, and the
floor is set in I-6, once all four tools have migrated, at the level then reached.
A 90% floor would fail as soon as vibe-heal migrates in I-2.

Input figures, from local `coverage.xml` files. They are non-authoritative and are
recomputed in CI before any floor is set:

| Tool | Coverage |
| --- | --- |
| pr-review | 0.9396 |
| spec-prism-flow | 0.9667 |
| sisyphus | 0.9675 |
| vibe-heal | 0.7949 |

The existing `sisyphus` status and flag stay untouched until I-8.
