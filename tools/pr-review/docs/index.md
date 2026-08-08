# dotharness-pr-review

Generic PR automation with configurable AI backends.

- [Configuration](configuration.md) — the `.harness.toml` file every command reads.
- [Commands](commands/index.md) — `review-prs`, `review-requested`, `self-review`, and `address-comments`.
- [Modules](modules.md) — generated API reference.

## Quick start

```sh
make install                       # set up the project (see README.md)
harness init                       # scaffold ./.harness.toml
$EDITOR .harness.toml              # set repo.name, repo.working_dir, etc.
harness validate                   # sanity-check the config
harness run review-prs             # run a command
```
