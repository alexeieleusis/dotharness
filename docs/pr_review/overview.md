# pr_review

Generic PR automation with configurable AI backends.

The CLI name (`pr_review`) and the config file names (`.harness*.toml`) differ on purpose: the config files keep the
old prefix so existing setups keep working.

- [Configuration](configuration.md) — the `.harness.toml` file every command reads.
- [Commands](commands/index.md) — `review-prs`, `focused-review`, `review-requested`, `self-review`, `address-comments`, and `local-review` (offline, with an optional `--address` phase).
- [Modules](modules.md) — generated API reference.

## Quick start

```sh
make install                       # set up the project (see README.md)
pr_review init                       # scaffold ./.harness.toml
$EDITOR .harness.toml              # set repo.name, repo.working_dir, etc.
pr_review validate                   # sanity-check the config
pr_review run review-prs             # run a command
```
