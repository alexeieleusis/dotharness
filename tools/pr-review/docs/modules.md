# Modules

::: harness

## Command modules

The `local-review` command lives in the package `harness.runners.local_review`. See [`local-review`](commands/local-review.md) for how to use it. The package re-exports its public names, so `from harness.runners.local_review import run` and the like keep working; each name is defined in one submodule:

- `types`: `LocalReviewError`, `NothingToReviewError`, `RepoMutatedError` and `LocalReviewTarget`.
- `git`: `check_preconditions` and the single git helper, built on `common.run_cmd`.
- `prompts`: `build_change_context`, `build_change_description`, `build_file_prompt`, `build_summary_prompt`, `build_design_prompt` and `build_address_prompt`.
- `findings`: `parse_findings`, `stamp_findings`, `stamp_findings_file`, `finding_id`, `FindingBlock`, `StampResult` and `AddressFinding`. Standard library only.
- `store`: the manifest, index and layout helpers: `read_manifest`, `write_manifest`, `review_dir_for`, `resolve_output_root`, `review_root_for_reset`, `reset`, `read_finding_status` and `write_finding_status`. Imports no backend, lock or orchestrator.
- `review`: the review phase: `run`, `run_local_review` and `ReviewResult`.
- `address`: the address phase (`--address`): `select_review_dir`, `collect_findings`, `address_findings` and `run_address_phase`.

The `ci-fix` command lives in `harness.runners.ci_fix`. See [`ci-fix`](commands/ci-fix.md) for how to use it. Public names: `run`, `CiFixError`, `DIGEST_PREFIX`, `get_attempts` and `find_step_command`.

The `merge` command lives in `harness.runners.merge`. See [`merge`](commands/merge.md) for how to use it. Public names: `run`, `MergeError`, `gate_record_path` and `read_gate_record`.
