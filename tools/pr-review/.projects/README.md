# Projects

Tracks projects designed and implemented against this codebase (`tools/pr-review/`)
using [spec-prism-flow](../../spec-prism-flow/): brief → phase corpus → merged PRs.
See `tools/spec-prism-flow/docs/lifecycle-cheat-sheet.md` for the full `plan`/`build`
lifecycle this drives.

## Layout

One subfolder per project, named by a short slug:

```
.projects/
  <slug>/
    .spec-prism-flow.toml   # copied from ../../.spec-prism-flow.toml.template
    init_manifest.json      # written by `plan init`
    00-overview.md          # written by `plan draft-overview`
    OPEN_QUESTIONS.md        # written by `plan draft-overview`, if any
    requirements.md          # written by `plan draft-requirements`
    graph.json                # written by `plan decompose`
    phases/
      NN-name-leaf.md        # written by `plan draft-phases`
```

Projects stay in this directory once merged — completed and in-progress projects are
both visible side by side, as a record of what spec-prism-flow has built here.

## Starting a new project

```bash
cd tools/pr-review
slug=my-feature
mkdir -p .projects/$slug
sed "s/<slug>/$slug/g" .spec-prism-flow.toml.template > .projects/$slug/.spec-prism-flow.toml

spec-prism-flow plan init BRIEF.md --config .projects/$slug/.spec-prism-flow.toml
```

Then continue through `plan draft-overview` → `draft-requirements` → `decompose` →
`draft-phases` → `plan review`, passing the same `--config` flag each time (see the
lifecycle cheat sheet for what each stage produces and the human checkpoints between
them).

## Running `build`

`build run` needs a git clone to work in (it branches, commits, pushes, and opens
PRs from whatever directory it's invoked in). Following the same pattern already used
for the spec-prism-flow project itself, run it from the `tools/pr-review` copy inside
the dedicated `~/.harness` clone of this repo — not this interactive checkout — so
agent-driven builds never collide with work in progress here:

```bash
cd ~/.harness/tools/pr-review
spec-prism-flow build run --config .projects/$slug/.spec-prism-flow.toml
```

## Review integration

The template config points `review.harness_config` at `~/.harness/.harness-pr-review.toml`
(not committed — see that repo's own `.gitignore` pattern `.harness*.toml`). It reuses
the same `working_dir` (`~/.harness`) and `vibe_heal` settings as
`~/.harness/.harness-spec_prism_flow.toml`, scoped to the `tools/pr-review` subdir.
