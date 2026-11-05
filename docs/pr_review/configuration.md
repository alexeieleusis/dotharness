# pr_review configuration: paths and defaults

This page documents the path-related settings that behave differently when `dotharness` is installed as a package. A
later change builds out the full configuration reference.

## `[harness] knowledge_dir`

Directory holding the review prompt templates (`<knowledge_dir>/pr-review/*.md`).

- Default: `~/.harness/knowledge`.
- Fallback: when `knowledge_dir` is not set in the config and `~/.harness/knowledge` does not exist, the knowledge shipped
  inside the package (`src/dotharness/knowledge/`, found with `importlib.resources`) is used.
- A `knowledge_dir` you set explicitly is used as written and never falls back; `pr_review validate` reports it when the
  path does not exist.
- This is the only setting with a packaged fallback.

## `[focused_review] vibe_types_repo`

Local checkout of the external vibe-types repository, read by focused review.

- Default: `~/.harness/vendor/vibe-types`.
- No packaged fallback: it is an external vendor checkout and cannot be shipped with the package. Clone it to that path
  or set `vibe_types_repo` to where it lives.

## Comments cache

`~/.harness/cache/pr-<n>-comments.json` holds the comments fetched for PR `<n>`. The location is unchanged from the
original tool.

## Harness-repo guard when installed

Backend runs watch the dotharness source checkout's HEAD to catch a runaway backend that commits into it. When the
package is installed outside a checkout (for example in `site-packages`), there is nothing to watch and this guard is
skipped; the repo-identity check for the configured repository (`[repo] name`) always runs. See
`docs/design/shared-async-layer.md`, "I-3 packaging-sensitive paths and repo guard".
