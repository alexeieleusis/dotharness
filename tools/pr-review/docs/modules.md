# Modules

::: harness

## Command modules

The `local-review` command lives in `harness.runners.local_review`. See [`local-review`](commands/local-review.md) for how to use it. The address phase (`--address`) is in the same module: `select_review_dir`, `collect_findings`, `address_findings` and `run_address_phase`.

The `ci-fix` command lives in `harness.runners.ci_fix`. See [`ci-fix`](commands/ci-fix.md) for how to use it. Public names: `run`, `CiFixError`, `DIGEST_PREFIX`, `get_attempts` and `find_step_command`.

The `merge` command lives in `harness.runners.merge`. See [`merge`](commands/merge.md) for how to use it. Public names: `run`, `MergeError`, `gate_record_path` and `read_gate_record`.
