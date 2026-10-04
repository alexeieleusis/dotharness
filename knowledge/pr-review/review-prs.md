# review-prs instructions

This runner executes vibe_heal SonarQube analysis on new open PRs.
It does not use the AI backend directly. vibe_heal handles analysis and comment posting.

`.harness.toml` drives the configuration: its `[vibe_heal]` section and `[[repo.subdir]]` entries.
