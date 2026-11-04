# Migration ledger

This ledger tracks every module of the four tools that are consolidated into the
`dotharness` project (issues I-2 to I-8 of #163). There is one row per module.

## Row format

```text
module | origin tool | origin path | status | shared module reused or extended | issue that moved it
```

Allowed `status` values:

- `not migrated`
- `migrated`
- `dropped` (give the reason in the "shared module" or "issue" column)

## Source trees and commits

The rows were seeded from these trees at fixed commits:

| Origin tool | Source tree | Commit |
|---|---|---|
| pr-review | `tools/pr-review/harness` | `cb0efc1` (this repository) |
| spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow` | `cb0efc1` (this repository) |
| sisyphus | `tools/sisyphus/sisyphus` | `cb0efc1` (this repository) |
| vibe-heal | `src/vibe_heal` | `9363868` (the vibe-heal repository) |

A module is one `*.py` file under a source tree. Test files (`test_*.py`,
`conftest.py`, anything under a `tests` directory) and empty `__init__.py` files are
not modules. A directory (package) is not a module.

## Re-running the seed script

```bash
python scripts/ledger_modules.py --count                       # row count
python scripts/ledger_modules.py                               # tool and path per module
python scripts/ledger_modules.py --markdown                    # ledger rows
python scripts/ledger_modules.py --vibe-heal /path/to/vibe-heal  # vibe-heal checkout
```

The script reads files with `git ls-tree` and `git show` at the commits above, so
the working tree does not affect the result. The default vibe-heal checkout is
`~/development/vibe-heal`. The row count must equal the number of rows below
(147 at seeding: pr-review 29, spec-prism-flow 39, sisyphus 20, vibe-heal 59).

## Update rule

Every PR for I-2 to I-8 that touches `src/dotharness/` updates this ledger in the
same PR. Set the `status`, name the shared module that was reused or extended, and
give the issue that moved the module. The PR template has a checkbox for this.

## Vendored files

`vendor/containers/docker-compose.yml` and `vendor/containers/manage-sonar.sh` were
imported from the vibe-heal repository at commit `9363868`. They are not Python
modules and have no row.

## Rows

| module | origin tool | origin path | status | shared module reused or extended | issue that moved it |
|---|---|---|---|---|---|
| `harness.backend` | pr-review | `tools/pr-review/harness/backend.py` | not migrated | | |
| `harness.cli` | pr-review | `tools/pr-review/harness/cli.py` | not migrated | | |
| `harness.config` | pr-review | `tools/pr-review/harness/config.py` | not migrated | | |
| `harness.lock` | pr-review | `tools/pr-review/harness/lock.py` | not migrated | | |
| `harness.repo_guard` | pr-review | `tools/pr-review/harness/repo_guard.py` | not migrated | | |
| `harness.runners.address_comments` | pr-review | `tools/pr-review/harness/runners/address_comments.py` | not migrated | | |
| `harness.runners.auto_approve` | pr-review | `tools/pr-review/harness/runners/auto_approve.py` | not migrated | | |
| `harness.runners.auto_approve_github` | pr-review | `tools/pr-review/harness/runners/auto_approve_github.py` | not migrated | | |
| `harness.runners.auto_approve_size` | pr-review | `tools/pr-review/harness/runners/auto_approve_size.py` | not migrated | | |
| `harness.runners.ci_checks` | pr-review | `tools/pr-review/harness/runners/ci_checks.py` | not migrated | | |
| `harness.runners.ci_fix` | pr-review | `tools/pr-review/harness/runners/ci_fix.py` | not migrated | | |
| `harness.runners.common` | pr-review | `tools/pr-review/harness/runners/common.py` | not migrated | | |
| `harness.runners.focused_review` | pr-review | `tools/pr-review/harness/runners/focused_review.py` | not migrated | | |
| `harness.runners.local_review` | pr-review | `tools/pr-review/harness/runners/local_review/__init__.py` | not migrated | | |
| `harness.runners.local_review.address` | pr-review | `tools/pr-review/harness/runners/local_review/address.py` | not migrated | | |
| `harness.runners.local_review.findings` | pr-review | `tools/pr-review/harness/runners/local_review/findings.py` | not migrated | | |
| `harness.runners.local_review.git` | pr-review | `tools/pr-review/harness/runners/local_review/git.py` | not migrated | | |
| `harness.runners.local_review.prompts` | pr-review | `tools/pr-review/harness/runners/local_review/prompts.py` | not migrated | | |
| `harness.runners.local_review.review` | pr-review | `tools/pr-review/harness/runners/local_review/review.py` | not migrated | | |
| `harness.runners.local_review.store` | pr-review | `tools/pr-review/harness/runners/local_review/store.py` | not migrated | | |
| `harness.runners.local_review.types` | pr-review | `tools/pr-review/harness/runners/local_review/types.py` | not migrated | | |
| `harness.runners.merge` | pr-review | `tools/pr-review/harness/runners/merge.py` | not migrated | | |
| `harness.runners.prune_projects` | pr-review | `tools/pr-review/harness/runners/prune_projects.py` | not migrated | | |
| `harness.runners.regret_review` | pr-review | `tools/pr-review/harness/runners/regret_review.py` | not migrated | | |
| `harness.runners.review_prs` | pr-review | `tools/pr-review/harness/runners/review_prs.py` | not migrated | | |
| `harness.runners.review_requested` | pr-review | `tools/pr-review/harness/runners/review_requested.py` | not migrated | | |
| `harness.runners.self_review` | pr-review | `tools/pr-review/harness/runners/self_review.py` | not migrated | | |
| `harness.scheduler` | pr-review | `tools/pr-review/harness/scheduler.py` | not migrated | | |
| `harness.state` | pr-review | `tools/pr-review/harness/state.py` | not migrated | | |
| `spec_prism_flow.build.agent_instructions` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/agent_instructions.py` | not migrated | | |
| `spec_prism_flow.build.agent_runner` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/agent_runner.py` | not migrated | | |
| `spec_prism_flow.build.claude_backend` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/claude_backend.py` | not migrated | | |
| `spec_prism_flow.build.completion_log` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/completion_log.py` | not migrated | | |
| `spec_prism_flow.build.errors` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/errors.py` | not migrated | | |
| `spec_prism_flow.build.gh_ops` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/gh_ops.py` | not migrated | | |
| `spec_prism_flow.build.git_ops` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/git_ops.py` | not migrated | | |
| `spec_prism_flow.build.harness_integration` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/harness_integration.py` | not migrated | | |
| `spec_prism_flow.build.manual_test` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/manual_test.py` | not migrated | | |
| `spec_prism_flow.build.merge_gates` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/merge_gates.py` | not migrated | | |
| `spec_prism_flow.build.opencode_backend` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/opencode_backend.py` | not migrated | | |
| `spec_prism_flow.build.parallel_runner` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/parallel_runner.py` | not migrated | | |
| `spec_prism_flow.build.phase_corpus` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/phase_corpus.py` | not migrated | | |
| `spec_prism_flow.build.phase_runner` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/phase_runner.py` | not migrated | | |
| `spec_prism_flow.build.resume_state` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/resume_state.py` | not migrated | | |
| `spec_prism_flow.build.retry_budget` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/retry_budget.py` | not migrated | | |
| `spec_prism_flow.build.scope_guard` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/scope_guard.py` | not migrated | | |
| `spec_prism_flow.build.toolchain` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/toolchain.py` | not migrated | | |
| `spec_prism_flow.build.track_runner` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/track_runner.py` | not migrated | | |
| `spec_prism_flow.build.vibe_heal_integration` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/vibe_heal_integration.py` | not migrated | | |
| `spec_prism_flow.chunk` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/chunk.py` | not migrated | | |
| `spec_prism_flow.cli` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/cli.py` | not migrated | | |
| `spec_prism_flow.config` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/config.py` | not migrated | | |
| `spec_prism_flow.decompose` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/decompose.py` | not migrated | | |
| `spec_prism_flow.decompose_journal` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/decompose_journal.py` | not migrated | | |
| `spec_prism_flow.draft_phases` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/draft_phases.py` | not migrated | | |
| `spec_prism_flow.errors` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/errors.py` | not migrated | | |
| `spec_prism_flow.generator` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/generator.py` | not migrated | | |
| `spec_prism_flow.graph` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/graph.py` | not migrated | | |
| `spec_prism_flow.handoff` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/handoff.py` | not migrated | | |
| `spec_prism_flow.linearize` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/linearize.py` | not migrated | | |
| `spec_prism_flow.markdown_utils` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/markdown_utils.py` | not migrated | | |
| `spec_prism_flow.overview_stage` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/overview_stage.py` | not migrated | | |
| `spec_prism_flow.phase_file` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/phase_file.py` | not migrated | | |
| `spec_prism_flow.prose_review` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/prose_review.py` | not migrated | | |
| `spec_prism_flow.requirements_stage` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/requirements_stage.py` | not migrated | | |
| `spec_prism_flow.review` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/review.py` | not migrated | | |
| `spec_prism_flow.sizing` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/sizing.py` | not migrated | | |
| `spec_prism_flow.workspace` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/workspace.py` | not migrated | | |
| `sisyphus.archive` | sisyphus | `tools/sisyphus/sisyphus/archive.py` | not migrated | | |
| `sisyphus.cleanup` | sisyphus | `tools/sisyphus/sisyphus/cleanup.py` | not migrated | | |
| `sisyphus.cli` | sisyphus | `tools/sisyphus/sisyphus/cli.py` | not migrated | | |
| `sisyphus.config` | sisyphus | `tools/sisyphus/sisyphus/config.py` | not migrated | | |
| `sisyphus.digest` | sisyphus | `tools/sisyphus/sisyphus/digest.py` | not migrated | | |
| `sisyphus.intake` | sisyphus | `tools/sisyphus/sisyphus/intake.py` | not migrated | | |
| `sisyphus.lock` | sisyphus | `tools/sisyphus/sisyphus/lock.py` | not migrated | | |
| `sisyphus.orchestrator` | sisyphus | `tools/sisyphus/sisyphus/orchestrator.py` | not migrated | | |
| `sisyphus.paths` | sisyphus | `tools/sisyphus/sisyphus/paths.py` | not migrated | | |
| `sisyphus.pick` | sisyphus | `tools/sisyphus/sisyphus/pick.py` | not migrated | | |
| `sisyphus.plan` | sisyphus | `tools/sisyphus/sisyphus/plan.py` | not migrated | | |
| `sisyphus.prbody` | sisyphus | `tools/sisyphus/sisyphus/prbody.py` | not migrated | | |
| `sisyphus.rails` | sisyphus | `tools/sisyphus/sisyphus/rails.py` | not migrated | | |
| `sisyphus.resume` | sisyphus | `tools/sisyphus/sisyphus/resume.py` | not migrated | | |
| `sisyphus.runrecord` | sisyphus | `tools/sisyphus/sisyphus/runrecord.py` | not migrated | | |
| `sisyphus.settled` | sisyphus | `tools/sisyphus/sisyphus/settled.py` | not migrated | | |
| `sisyphus.stages` | sisyphus | `tools/sisyphus/sisyphus/stages.py` | not migrated | | |
| `sisyphus.stoprecord` | sisyphus | `tools/sisyphus/sisyphus/stoprecord.py` | not migrated | | |
| `sisyphus.subproc` | sisyphus | `tools/sisyphus/sisyphus/subproc.py` | not migrated | | |
| `sisyphus.ticketfile` | sisyphus | `tools/sisyphus/sisyphus/ticketfile.py` | not migrated | | |
| `vibe_heal` | vibe-heal | `src/vibe_heal/__init__.py` | not migrated | | |
| `vibe_heal.__main__` | vibe-heal | `src/vibe_heal/__main__.py` | not migrated | | |
| `vibe_heal.ai_tools` | vibe-heal | `src/vibe_heal/ai_tools/__init__.py` | not migrated | | |
| `vibe_heal.ai_tools.aider` | vibe-heal | `src/vibe_heal/ai_tools/aider.py` | not migrated | | |
| `vibe_heal.ai_tools.base` | vibe-heal | `src/vibe_heal/ai_tools/base.py` | not migrated | | |
| `vibe_heal.ai_tools.claude_code` | vibe-heal | `src/vibe_heal/ai_tools/claude_code.py` | not migrated | | |
| `vibe_heal.ai_tools.external_docs` | vibe-heal | `src/vibe_heal/ai_tools/external_docs.py` | not migrated | | |
| `vibe_heal.ai_tools.factory` | vibe-heal | `src/vibe_heal/ai_tools/factory.py` | not migrated | | |
| `vibe_heal.ai_tools.gemini` | vibe-heal | `src/vibe_heal/ai_tools/gemini.py` | not migrated | | |
| `vibe_heal.ai_tools.models` | vibe-heal | `src/vibe_heal/ai_tools/models.py` | not migrated | | |
| `vibe_heal.ai_tools.opencode` | vibe-heal | `src/vibe_heal/ai_tools/opencode.py` | not migrated | | |
| `vibe_heal.ai_tools.prompts` | vibe-heal | `src/vibe_heal/ai_tools/prompts.py` | not migrated | | |
| `vibe_heal.ai_tools.utils` | vibe-heal | `src/vibe_heal/ai_tools/utils.py` | not migrated | | |
| `vibe_heal.cleanup` | vibe-heal | `src/vibe_heal/cleanup/__init__.py` | not migrated | | |
| `vibe_heal.cleanup.orchestrator` | vibe-heal | `src/vibe_heal/cleanup/orchestrator.py` | not migrated | | |
| `vibe_heal.cleanup_pr` | vibe-heal | `src/vibe_heal/cleanup_pr/__init__.py` | not migrated | | |
| `vibe_heal.cleanup_pr.main_duplications` | vibe-heal | `src/vibe_heal/cleanup_pr/main_duplications.py` | not migrated | | |
| `vibe_heal.cleanup_pr.orchestrator` | vibe-heal | `src/vibe_heal/cleanup_pr/orchestrator.py` | not migrated | | |
| `vibe_heal.cleanup_pr.scope` | vibe-heal | `src/vibe_heal/cleanup_pr/scope.py` | not migrated | | |
| `vibe_heal.cli` | vibe-heal | `src/vibe_heal/cli.py` | not migrated | | |
| `vibe_heal.config` | vibe-heal | `src/vibe_heal/config/__init__.py` | not migrated | | |
| `vibe_heal.config.exceptions` | vibe-heal | `src/vibe_heal/config/exceptions.py` | not migrated | | |
| `vibe_heal.config.models` | vibe-heal | `src/vibe_heal/config/models.py` | not migrated | | |
| `vibe_heal.config.scanner_discovery` | vibe-heal | `src/vibe_heal/config/scanner_discovery.py` | not migrated | | |
| `vibe_heal.converters` | vibe-heal | `src/vibe_heal/converters/__init__.py` | not migrated | | |
| `vibe_heal.converters.oxlint` | vibe-heal | `src/vibe_heal/converters/oxlint.py` | not migrated | | |
| `vibe_heal.deduplication` | vibe-heal | `src/vibe_heal/deduplication/__init__.py` | not migrated | | |
| `vibe_heal.deduplication.client` | vibe-heal | `src/vibe_heal/deduplication/client.py` | not migrated | | |
| `vibe_heal.deduplication.models` | vibe-heal | `src/vibe_heal/deduplication/models.py` | not migrated | | |
| `vibe_heal.deduplication.orchestrator` | vibe-heal | `src/vibe_heal/deduplication/orchestrator.py` | not migrated | | |
| `vibe_heal.deduplication.processor` | vibe-heal | `src/vibe_heal/deduplication/processor.py` | not migrated | | |
| `vibe_heal.git` | vibe-heal | `src/vibe_heal/git/__init__.py` | not migrated | | |
| `vibe_heal.git.branch_analyzer` | vibe-heal | `src/vibe_heal/git/branch_analyzer.py` | not migrated | | |
| `vibe_heal.git.diff_parser` | vibe-heal | `src/vibe_heal/git/diff_parser.py` | not migrated | | |
| `vibe_heal.git.exceptions` | vibe-heal | `src/vibe_heal/git/exceptions.py` | not migrated | | |
| `vibe_heal.git.file_selection` | vibe-heal | `src/vibe_heal/git/file_selection.py` | not migrated | | |
| `vibe_heal.git.manager` | vibe-heal | `src/vibe_heal/git/manager.py` | not migrated | | |
| `vibe_heal.git.paths` | vibe-heal | `src/vibe_heal/git/paths.py` | not migrated | | |
| `vibe_heal.models` | vibe-heal | `src/vibe_heal/models.py` | not migrated | | |
| `vibe_heal.orchestrator` | vibe-heal | `src/vibe_heal/orchestrator.py` | not migrated | | |
| `vibe_heal.output` | vibe-heal | `src/vibe_heal/output.py` | not migrated | | |
| `vibe_heal.processor` | vibe-heal | `src/vibe_heal/processor/__init__.py` | not migrated | | |
| `vibe_heal.processor.issue_processor` | vibe-heal | `src/vibe_heal/processor/issue_processor.py` | not migrated | | |
| `vibe_heal.processor.models` | vibe-heal | `src/vibe_heal/processor/models.py` | not migrated | | |
| `vibe_heal.review` | vibe-heal | `src/vibe_heal/review/__init__.py` | not migrated | | |
| `vibe_heal.review.duplication_scope` | vibe-heal | `src/vibe_heal/review/duplication_scope.py` | not migrated | | |
| `vibe_heal.review.github` | vibe-heal | `src/vibe_heal/review/github.py` | not migrated | | |
| `vibe_heal.review.line_filter` | vibe-heal | `src/vibe_heal/review/line_filter.py` | not migrated | | |
| `vibe_heal.review.models` | vibe-heal | `src/vibe_heal/review/models.py` | not migrated | | |
| `vibe_heal.review.orchestrator` | vibe-heal | `src/vibe_heal/review/orchestrator.py` | not migrated | | |
| `vibe_heal.review.reporter` | vibe-heal | `src/vibe_heal/review/reporter.py` | not migrated | | |
| `vibe_heal.sonarqube` | vibe-heal | `src/vibe_heal/sonarqube/__init__.py` | not migrated | | |
| `vibe_heal.sonarqube.analysis_runner` | vibe-heal | `src/vibe_heal/sonarqube/analysis_runner.py` | not migrated | | |
| `vibe_heal.sonarqube.client` | vibe-heal | `src/vibe_heal/sonarqube/client.py` | not migrated | | |
| `vibe_heal.sonarqube.exceptions` | vibe-heal | `src/vibe_heal/sonarqube/exceptions.py` | not migrated | | |
| `vibe_heal.sonarqube.models` | vibe-heal | `src/vibe_heal/sonarqube/models.py` | not migrated | | |
| `vibe_heal.sonarqube.project_manager` | vibe-heal | `src/vibe_heal/sonarqube/project_manager.py` | not migrated | | |
| `vibe_heal.sonarqube.properties_handler` | vibe-heal | `src/vibe_heal/sonarqube/properties_handler.py` | not migrated | | |
| `vibe_heal.utils` | vibe-heal | `src/vibe_heal/utils/__init__.py` | not migrated | | |
