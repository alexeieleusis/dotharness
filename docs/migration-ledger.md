# Migration ledger

Frozen at I-6: this ledger is a historical migration record; SonarQube is the duplicate tracker.

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
V=/path/to/vibe-heal                                  # a clone containing 9363868
python scripts/ledger_modules.py --vibe-heal $V --count      # row count
python scripts/ledger_modules.py --vibe-heal $V              # tool and path per module
python scripts/ledger_modules.py --vibe-heal $V --markdown   # ledger rows
python scripts/ledger_modules.py --vibe-heal $V --snapshot   # tests/data/ledger_modules.tsv
```

The script reads files with `git ls-tree` and `git show` at the commits above, so
the working tree does not affect the result. The script is a one-off
regeneration tool: `--vibe-heal` is required. CI checks the ledger against the frozen
`tests/data/ledger_modules.tsv`, so no external checkout is needed. Set `VIBE_HEAL_PATH`
to also check that the snapshot still matches the source commits. The row count must equal the number of rows below
(147 at seeding: pr-review 29, spec-prism-flow 39, sisyphus 20, vibe-heal 59).

## Update rule

Every PR for I-2 to I-8 that touches `src/dotharness/` updates this ledger in the
same PR. Set the `status`, name the shared module that was reused or extended, and
give the issue that moved the module. The PR template has a checkbox for this.

## Vendored files

`vendor/containers/docker-compose.yml` and `vendor/containers/manage-sonar.sh` were
imported from the vibe-heal repository at commit `9363868`. They are not Python
modules and have no row.

Known deviation from upstream in `docker-compose.yml`: SonarQube is bound to
`127.0.0.1:9000` instead of all interfaces. The database password comes from the
required `SONAR_DB_PASSWORD` environment variable instead of a committed value.

## Tests dropped or rewritten in I-2

Tests are not ledger modules: the ledger has one row per source module, and test files
have no row. This section records what happened to the vibe-heal tests in I-2 (#164).

The vibe-heal tree at `9363868` has 44 `test_*.py` files (57 `.py` files under `tests`).
The migrated tree has 43 `test_*.py` files (56 `.py` files): 44 minus the 2 deleted
files plus the new `tests/vibe_heal/test_cli_surface.py`.

Deleted with the removed backends:

- `tests/ai_tools/test_aider.py`
- `tests/ai_tools/test_gemini.py`

Rewritten for the claude-code and opencode backends and the shared AI backend launcher:

- `tests/ai_tools/test_factory.py`
- `tests/ai_tools/test_base.py`
- `tests/config/test_config_loading.py`
- `tests/config/test_models.py`
- `tests/cleanup_pr/test_cli.py`
- also `tests/ai_tools/test_claude_code.py`, `test_opencode.py` and `test_utils.py`

New tests for the shared layer: `tests/test_subprocess_runner.py`, `tests/test_ai_backend.py`
and `tests/vibe_heal/test_cli_surface.py`.

## Tests dropped or rewritten in I-3

Tests are not ledger modules and have no row. This section records what happened to the
pr-review tests in I-3 (#165).

Counts, recounted at the baseline `cb0efc1` (`git ls-tree`, not taken from the ticket):
`tools/pr-review/tests` has 34 `test_*.py` files (7 at the top level: `test_backend`,
`test_cli`, `test_config`, `test_lock`, `test_repo_guard`, `test_scheduler`, `test_state`; 27 under
`runners/`), plus `__init__.py` and `runners/__init__.py`, plus `conftest.py`. The migrated tree
`tests/pr_review` has 35 `test_*.py` files: 34, minus `test_repo_guard.py` (moved, see below), plus
`test_layout_sample.py` (I-1) and the new `runners/test_local_review_head_sha.py`.

Dropped:

- Global opencode lock tests (D-1): `test_opencode_run_acquires_global_lock` and
  `test_claude_run_does_not_acquire_global_lock` in `test_backend.py`. The global lock
  (`_OPENCODE_GLOBAL_LOCK_KEY`) was removed from `Backend`. `test_removed_internals_are_gone`
  pins that it is gone.
- The other `test_backend.py` tests of internals that moved into the shared layer (temp-file
  handling, process-group wait, cwd-divergence monitor, `lsof` parsing, opencode plugin check,
  harness-repo snapshot). `test_backend.py` went from 44 to 19 tests. The behavior is covered by
  `tests/test_ai_backend.py` and `tests/test_subprocess_runner.py` (I-2).
- `test_repo_guard.py`: the pr-review `repo_guard` module was replaced by `dotharness.repo_guard`
  (D-2), so its tests moved to `tests/test_repo_guard.py`.

Rewritten:

- `test_cli.py`: rewritten from click (`click.testing.CliRunner`) to typer (`typer.testing.CliRunner`),
  same assertions. `test_state.py` and `runners/test_local_review_address.py` only switched the
  `CliRunner` import.
- `test_backend.py`: rewritten for the `launch_sync` adapter (19 tests, 3 of them end to end against
  a fake executable).
- `runners/test_common.py`: real-subprocess tests for the new `run_cmd` (bytes output,
  `CalledProcessError` fields, `check=False`, timeout, grandchild killed, missing executable and cwd,
  shell mode and exit 127, env and cwd passthrough, and a source guard against `killpg`,
  `start_new_session` and `Popen`).
- `test_scheduler.py`: the 31 existing tests kept. New exact-text tests cover the cron line and the
  launchd plist, XML escaping, marker round trip, cron edge cases, and
  `test_run_config_then_runner_works_and_runner_then_config_is_usage_error`.
- `test_config.py`: tests for `default_knowledge_dir()` and the `vibe_types_repo` default.
- `runners/test_review_prs.py` and `runners/test_prune_projects.py`: pin the full argv with
  `-m dotharness.vibe_heal` and the `--env-file` position.
- `runners/test_merge.py`: fixture path `parents[2]` (the golden record moved to
  `tests/contracts/gate_record_golden.json`). `runners/test_focused_review.py` and
  `runners/test_local_review.py`: `parents[4]` became `parents[3]` for the knowledge symlink path.
- `test_lock.py`: uses `get_context("fork")` because spawn children cannot import the module under
  pytest's importlib mode (the lock is POSIX-only anyway).
- `conftest.py` (disposition: kept, edited): the autouse fixtures that patched removed internals
  (`_HARNESS_REPO_ROOT`, `assert_no_opencode_plugins`, `Backend._processes_in_group`, the
  `repo_guard` SSH cache) were removed. `tmp_xdg` now patches `dotharness.ai_backend.TMP_DIR`,
  `dotharness.pr_review.state.XDG_DATA` and `dotharness.pr_review.lock.XDG_RUNTIME`.
- `__init__.py` (both, empty): kept as `tests/pr_review/__init__.py` and
  `tests/pr_review/runners/__init__.py`, so test module names are `pr_review.*`.

New tests outside `tests/pr_review`: `tests/contracts/test_gate_record_fixture.py` (golden record is
byte-identical to the copy under `tools/`), and `tests/test_stubs.py` no longer lists `pr_review`.

`test_layout_sample.py` (kept): it does not exist at `cb0efc1`. It was added to the dotharness
repository by I-1 (#163, `838494f`) to prove that a same-basename test collects under pytest's
importlib mode. It was kept in `tests/pr_review/`. It can be removed in a later PR.

### Typer versus click behavior differences

The CLI is `pr_review` (typer). Differences observed from the click `harness` CLI:

1. `click.UsageError` became `typer.BadParameter`: the exit code stays 2, but the message is shown
   as a rich "Error" box with `Invalid value: <msg>` instead of `Error: <msg>`.
2. `click.ClickException` became a local `_fail()`: exit code 1 and `Error: <msg>` are identical.
3. Usage line: positional arguments show `{COMMAND}` instead of `COMMAND`. `init --help` shows
   `DIRECTORY  [default: .]` where click showed `[DIRECTORY]`. Metavars `PATH`, `TEXT`, `N` and
   `[cron|launchd]` are set explicitly to match click.
4. `--config` after a runner name fails with `No such option`, exit 2, as in click.
5. Group help is rendered in typer's plain style, and command descriptions are truncated with `...`
   in the group listing. `focused-review`, `self-review` and `address-comments` still have no help text.
6. `rich_markup_mode=None` (the default rich markup dropped `[ci_fix]` and `[merge]` from help) and
   `pretty_exceptions_enable=False` (plain tracebacks as in click).
7. `no_args_is_help=True` on the groups mimics click. `sys.exit(...)` is kept where the source used it.
8. `click` is not a direct dependency (typer is). A module-level alias `cli = app` keeps the wiring
   `from dotharness.pr_review.cli import cli as pr_review` working.

### Typer versus click behavior differences for spec_prism_flow

The CLI is `spec_prism_flow` (typer). Differences observed by running the click `spec-prism-flow` CLI and the typer one side by side:

1. There is no root `build` group, so `spec_prism_flow build run` fails with `No such command 'build'` and exit code 2. The
   root help lists only `plan`.
2. Missing positional argument: the usage line shows `{brief_path}` where click showed `BRIEF_PATH`, and the message is
   `Missing argument 'brief_path'.` where click said `'BRIEF_PATH'`. Exit code stays 2.
3. Unknown option: the message is `No such option: --bogus` where click said `No such option '--bogus'`. Exit code stays 2.
4. `--help` lists arguments in an `Arguments:` section (`brief_path  [required]`), which click did not, and shows option metavars
   as `<path>` and `<str>` where click showed `PATH` and `TEXT`.
5. `plan --help` lists the commands in definition order (`init`, `draft-overview`, ...) instead of alphabetically, and the
   group descriptions drop the "(Feature A)" suffix.
6. Runtime errors raised through `_fail()` keep exit code 1 and `Error: <msg>`. `plan init` of a missing brief prints
   `Error: BRIEF_PATH does not exist: <path>`, as before.
7. `plan` with no command prints help and exits 2, as in click.

### Behavior differences from the shared-layer adapters

`Backend` (adapter over `ai_backend.launch`):
- prompt files use the prefix `dotharness_`.
- retries still cover timeouts only (`retry_on_nonzero` stays off).
- argv is no longer logged and the 2000-character failed-run log is dropped.
- a missing executable returns `CompletedProcess(127)` instead of raising `FileNotFoundError`.

The harness-repo guard runs only for a source checkout (`find_harness_checkout()`). An
installed package skips it.

`run_cmd` (`async def` in `pr_review/runners/common.py`, over `subprocess_runner.run`):
- the grace period is 5 s instead of 10 s.
- stdin is `/dev/null` instead of inherited.
- non-UTF-8 output is lossy (U+FFFD).
- `TimeoutExpired.stderr` ends with the shared "timed out after Ns" line.
- `PermissionError` on exec surfaces as `FileNotFoundError`.
- the process group gets a SIGKILL sweep after a normal exit.

A missing executable or cwd still raises `FileNotFoundError`, as the baseline `Popen` did.

The same differences apply to `git_toplevel`, `vibe_heal` `run_command` and `GitHubReviewClient._post_json`, which now run
through `subprocess_runner.run` (Phase 07, D-15 in `docs/design/shared-async-layer.md`). These are the accepted I-3
differences, now applied to the async `run`. `git_toplevel` is `async def` too.

### Cron versus launchd argument order

At the baseline `cb0efc1` the two scheduler forms differed:

- cron: `<bin> run --config <cfg> <command>`
- launchd: `<bin> run <command> --config <cfg>`

The typer test written in I-3 shows that the launchd form fails: `run review-prs --config <cfg>` exits 2
with `Usage: pr_review run review-prs [OPTIONS]` and `Error: No such option: --config`
(`test_run_config_then_runner_works_and_runner_then_config_is_usage_error` in
`tests/pr_review/test_scheduler.py`). This was probably already broken at the baseline (D-8). I-3
fixed it with user approval: `schedule install` now writes the cron order for launchd entries too, and
the binary is looked up as `shutil.which("pr_review")`. Schedules installed by the old `harness` binary
(old crontab entries and old launchd plists) must be reinstalled. I-7 owns that.

### External reference inventory (seed for I-7)

Command word `harness` outside `tools/pr-review` and outside the migrated pr-review code. Fact labels:
[R] read from the repository, [B] taken from the brief or ticket only, not read.
`tools/pr-review` itself stays until I-8. No file below was edited by I-3, except where marked.

Repository files that invoke or name the command `harness` or its install directory [R]:

| File | Matched text |
|---|---|
| `README.md` | `uv tool install --editable ~/.harness/tools/pr-review/`; `harness init          # in each repo you want to automate`; `harness validate      # confirm config is correct`; link ``[`tools/pr-review/`](tools/pr-review/README.md)`` and `[docs](tools/pr-review/docs/index.md)` |
| `tools/spec-prism-flow/spec_prism_flow/config.py` | `DEFAULT_REVIEW_COMMAND = "harness"`; `DEFAULT_REVIEW_TOOL_DIR = "~/.harness/tools/pr-review"` |
| `tools/spec-prism-flow/spec_prism_flow/build/harness_integration.py` | runs `uv run --project <config.tool_dir> <config.command> run --config <config.harness_config> <subcommand>` (docstring: ``"""A `harness run` subprocess exited non-zero ...``) |
| `tools/spec-prism-flow/tests/test_config.py` | `assert cfg.review.command == "harness"` |
| `tools/spec-prism-flow/tests/build/test_harness_integration.py` | `command="harness",` and `"harness",` in the expected argv |
| `tools/spec-prism-flow/docs/phases/01-config-and-phase-file-leaf.md` | `command = "harness"` |
| `tools/spec-prism-flow/docs/phases/09-build-static-analysis-review-integration-leaf.md` | ``uv run --project <harness_tool_dir> harness run --config <harness_co...`` |
| `tools/spec-prism-flow/docs/lifecycle-cheat-sheet.md` | `` `harness run all` already runs on a repeating cycle ``; ``See `pr-review`'s command docs for what each `harness run all` step does``; ``isn't** already swept by `harness run`` |
| `tools/spec-prism-flow/docs/lifecycle-cheat-sheet.md`, `tools/spec-prism-flow/docs/requirements.md`, `tools/sisyphus/docs/safety-rails.md`, `tools/sisyphus/sisyphus/paths.py` | `~/.harness/tools/spec-prism-flow` and `~/.harness/knowledge` (install directory, not the command) |
| `tools/sisyphus/tests/test_orchestrator.py` | `("harness", "run", "ci-fix")`, `("harness", "run", "merge")`, `assert argv[0] in {"git", "harness", "make"}`, and a forbidden-word list containing `"harness"` |
| `tools/sisyphus/tests/test_cli.py`, `test_digest.py`, `test_intake.py`, `test_pick.py`, `test_prbody.py`, `test_rails.py`, `test_runrecord.py` | import guards: `("harness", "spec_prism_flow")` as forbidden top-level module names |
| `tools/sisyphus/tests/test_paths.py` | `home / ".harness/tools/spec-prism-flow"` |
| `.github/workflows/main.yml` | `paths: "tools/pr-review/**"` (push and pull_request) and `working-directory: tools/pr-review`; `hashFiles('tools/pr-review/.pre-commit-config.yaml')` |
| `.github/workflows/docs.yml` | path filters `tools/pr-review/docs/**`, `tools/pr-review/zensical.toml`, `tools/pr-review/harness/**`; `working-directory: tools/pr-review`; `cp -r tools/pr-review/site/. combined-site/` |
| `.github/actions/setup-python-env/action.yml` | `default: "tools/pr-review"` |
| `docs/design/shared-async-layer.md` | ``pr-review `_cmd_for` \| `tools/pr-review/harness/backend.py` `` and the `run_cmd` row `tools/pr-review/harness/runners/common.py` (historical description of the baseline) |

Migrated copies that still say `harness` as a command or install path on purpose (history, or
quoted from the old tool) [R]:

- `docs/pr_review/design-review-requirements.md` and `docs/pr_review/requirement-traceability-requirements.md`:
  ``Drafted per `~/.harness/tools/requirements-doc-drafting-prompt.md` ``, `` `harness validate` ``,
  `` `harness run self-review` / `harness run review-requested` `` (marked Historical).
- `docs/pr_review/.spec-prism-flow.toml.template`: `command = "harness"` and
  `tool_dir = "~/.harness/tools/pr-review"` (marked Historical and copied as-is).
- `docs/pr_review/scripts/pr-comments.py`: historical copy. The shipped script is
  `src/dotharness/scripts/pr-comments.py`.

Identifiers that contain `harness` but are not references to the command, kept on purpose [R]:
- the `[harness]` config section and `cfg.harness.*` fields (`harness.backend`, `harness.knowledge_dir`, ...)
- the config file names `.harness*.toml`
- the directory `~/.harness/...`
- the cron marker `# harness: <command> ...` in `src/dotharness/pr_review/scheduler.py`
- the launchd label `com.harness.<command>-<slug>` in `scheduler.py` and `src/dotharness/pr_review/cli.py`
- the ref `refs/harness-recovery`
- the message `harness.backend: binary '<bin>' not found on PATH` in `pr_review validate` and `No harness schedules found.` in `pr_review schedule list`

Changing the cron marker or the launchd label would orphan existing schedules, so they stay until I-7
decides.

Outside the repository [B]: wrapper scripts under `~/development/dotharness-work/bin/` call `harness`.
This comes from the ticket only. The files were not read and are not edited here. Live config files
(`.harness*.toml`) were not read either.

### Files not migrated

`tools/pr-review/.projects/` (`README.md` and `regret-review/`: 18 files, the spec-prism-flow working
tree used to design the regret review) is not migrated, by human decision. The original stays in
`tools/pr-review` until I-8. The other historical non-code files (`design-review-requirements.md`,
`requirement-traceability-requirements.md`, `scripts/pr-comments.py`, `.spec-prism-flow.toml.template`) are
migrated as historical files under `docs/pr_review/`. They have no ledger row because the ledger counts
only Python modules under `harness/` (`pr-comments.py` is also shipped as
`src/dotharness/scripts/pr-comments.py`).

## Tests dropped or rewritten in I-4

Tests are not ledger modules and have no row. This section records what happened to the
spec-prism-flow tests in I-4 (#166). `tools/spec-prism-flow/tests` has 17 top-level `test_*.py` files
and `tests/build/` (21 files: `conftest.py` plus 20 test files).

Dropped:

- `tests/build/*`: all 21 files, with the build command group (`build command group not migrated
  (#166, D master 4.2)`).
- The build-only assertions in `test_cli.py` and `test_config.py` (build group, `[build]` config).

Rewritten:

- `test_prose_review.py`: rewritten for the shared `launch_sync` backend launcher and
  `dotharness.git_utils.git_toplevel`.

Migrated to `tests/spec_prism_flow/` (17 files): `test_chunk`, `test_cli`, `test_config`,
`test_decompose_journal`, `test_decompose`, `test_draft_phases`, `test_generator`, `test_graph`,
`test_handoff`, `test_linearize`, `test_overview_stage`, `test_phase_file`, `test_prose_review`,
`test_requirements_stage`, `test_review`, `test_sizing`, `test_workspace`. `test_cli.py` and
`test_config.py` are migrated with the build assertions removed. `test_prose_review.py` is migrated as
rewritten. `markdown_utils` and `errors` have no top-level test file, as in the original.

## Rows

| module | origin tool | origin path | status | shared module reused or extended | issue that moved it |
|---|---|---|---|---|---|
| `harness.backend` | pr-review | `tools/pr-review/harness/backend.py` | migrated | `dotharness.ai_backend` (`launch_sync`), `dotharness.subprocess_runner`, `dotharness.repo_guard` | #165 (now a thin adapter over `launch_sync`; global opencode lock dropped per D-1; missing executable returns exit 127) |
| `harness.cli` | pr-review | `tools/pr-review/harness/cli.py` | migrated | none: pr-review specific (click converted to typer; wired as `pr_review` in `dotharness.cli`) | #165 |
| `harness.config` | pr-review | `tools/pr-review/harness/config.py` | migrated | none: pr-review specific (adds packaged `default_knowledge_dir()` fallback and a `vibe_types_repo` default) | #165 |
| `harness.lock` | pr-review | `tools/pr-review/harness/lock.py` | migrated | none: pr-review specific | #165 |
| `harness.repo_guard` | pr-review | `tools/pr-review/harness/repo_guard.py` | dropped | replaced by `dotharness.repo_guard` (D-2: the pr-review copy was deleted; `local_review.review` raises the shared `RepoIdentityError`) | #165 (module replaced by the shared one, not copied) |
| `harness.runners.address_comments` | pr-review | `tools/pr-review/harness/runners/address_comments.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.auto_approve` | pr-review | `tools/pr-review/harness/runners/auto_approve.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.auto_approve_github` | pr-review | `tools/pr-review/harness/runners/auto_approve_github.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.auto_approve_size` | pr-review | `tools/pr-review/harness/runners/auto_approve_size.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.ci_checks` | pr-review | `tools/pr-review/harness/runners/ci_checks.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.ci_fix` | pr-review | `tools/pr-review/harness/runners/ci_fix.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.common` | pr-review | `tools/pr-review/harness/runners/common.py` | migrated | `dotharness.subprocess_runner` (`run_cmd` is a sync wrapper over `subprocess_runner.run`) | #165 (`run_cmd` replaced, option (a); see "Behavior differences" below) |
| `harness.runners.focused_review` | pr-review | `tools/pr-review/harness/runners/focused_review.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.local_review` | pr-review | `tools/pr-review/harness/runners/local_review/__init__.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.local_review.address` | pr-review | `tools/pr-review/harness/runners/local_review/address.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.local_review.findings` | pr-review | `tools/pr-review/harness/runners/local_review/findings.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.local_review.git` | pr-review | `tools/pr-review/harness/runners/local_review/git.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.local_review.prompts` | pr-review | `tools/pr-review/harness/runners/local_review/prompts.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.local_review.review` | pr-review | `tools/pr-review/harness/runners/local_review/review.py` | migrated | `dotharness.repo_guard` (`RepoIdentityError`) | #165 |
| `harness.runners.local_review.store` | pr-review | `tools/pr-review/harness/runners/local_review/store.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.local_review.types` | pr-review | `tools/pr-review/harness/runners/local_review/types.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.merge` | pr-review | `tools/pr-review/harness/runners/merge.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.prune_projects` | pr-review | `tools/pr-review/harness/runners/prune_projects.py` | migrated | none: pr-review specific (spawns `python -m dotharness.vibe_heal`) | #165 |
| `harness.runners.regret_review` | pr-review | `tools/pr-review/harness/runners/regret_review.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.review_prs` | pr-review | `tools/pr-review/harness/runners/review_prs.py` | migrated | none: pr-review specific (spawns `python -m dotharness.vibe_heal`) | #165 |
| `harness.runners.review_requested` | pr-review | `tools/pr-review/harness/runners/review_requested.py` | migrated | none: pr-review specific | #165 |
| `harness.runners.self_review` | pr-review | `tools/pr-review/harness/runners/self_review.py` | migrated | none: pr-review specific | #165 |
| `harness.scheduler` | pr-review | `tools/pr-review/harness/scheduler.py` | migrated | none: pr-review specific (launchd argument order fixed to match cron) | #165 |
| `harness.state` | pr-review | `tools/pr-review/harness/state.py` | migrated | none: pr-review specific | #165 |
| `spec_prism_flow.build.agent_instructions` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/agent_instructions.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.agent_runner` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/agent_runner.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.claude_backend` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/claude_backend.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.completion_log` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/completion_log.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.errors` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/errors.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.gh_ops` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/gh_ops.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.git_ops` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/git_ops.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.harness_integration` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/harness_integration.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.manual_test` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/manual_test.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.merge_gates` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/merge_gates.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.opencode_backend` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/opencode_backend.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.parallel_runner` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/parallel_runner.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.phase_corpus` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/phase_corpus.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.phase_runner` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/phase_runner.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.resume_state` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/resume_state.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.retry_budget` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/retry_budget.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.scope_guard` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/scope_guard.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.toolchain` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/toolchain.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.track_runner` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/track_runner.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.build.vibe_heal_integration` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/build/vibe_heal_integration.py` | dropped | | build command group not migrated (#166, D master 4.2) |
| `spec_prism_flow.chunk` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/chunk.py` | migrated | | #166 |
| `spec_prism_flow.cli` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/cli.py` | migrated | | #166 |
| `spec_prism_flow.config` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/config.py` | migrated | `dotharness.paths` (`default_knowledge_dir`) | #166 |
| `spec_prism_flow.decompose` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/decompose.py` | migrated | | #166 |
| `spec_prism_flow.decompose_journal` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/decompose_journal.py` | migrated | | #166 |
| `spec_prism_flow.draft_phases` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/draft_phases.py` | migrated | | #166 |
| `spec_prism_flow.errors` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/errors.py` | migrated | | #166 |
| `spec_prism_flow.generator` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/generator.py` | migrated | | #166 |
| `spec_prism_flow.graph` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/graph.py` | migrated | | #166 |
| `spec_prism_flow.handoff` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/handoff.py` | migrated | | #166 |
| `spec_prism_flow.linearize` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/linearize.py` | migrated | | #166 |
| `spec_prism_flow.markdown_utils` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/markdown_utils.py` | migrated | | #166 |
| `spec_prism_flow.overview_stage` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/overview_stage.py` | migrated | | #166 |
| `spec_prism_flow.phase_file` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/phase_file.py` | migrated | | #166 |
| `spec_prism_flow.prose_review` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/prose_review.py` | migrated | `dotharness.ai_backend` (`launch_sync`), `dotharness.git_utils` (`git_toplevel`) | #166 |
| `spec_prism_flow.requirements_stage` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/requirements_stage.py` | migrated | | #166 |
| `spec_prism_flow.review` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/review.py` | migrated | | #166 |
| `spec_prism_flow.sizing` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/sizing.py` | migrated | | #166 |
| `spec_prism_flow.workspace` | spec-prism-flow | `tools/spec-prism-flow/spec_prism_flow/workspace.py` | migrated | | #166 |
| `sisyphus.archive` | sisyphus | `tools/sisyphus/sisyphus/archive.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.cleanup` | sisyphus | `tools/sisyphus/sisyphus/cleanup.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.cli` | sisyphus | `tools/sisyphus/sisyphus/cli.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.config` | sisyphus | `tools/sisyphus/sisyphus/config.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.digest` | sisyphus | `tools/sisyphus/sisyphus/digest.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.intake` | sisyphus | `tools/sisyphus/sisyphus/intake.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.lock` | sisyphus | `tools/sisyphus/sisyphus/lock.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.orchestrator` | sisyphus | `tools/sisyphus/sisyphus/orchestrator.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.paths` | sisyphus | `tools/sisyphus/sisyphus/paths.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.pick` | sisyphus | `tools/sisyphus/sisyphus/pick.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.plan` | sisyphus | `tools/sisyphus/sisyphus/plan.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.prbody` | sisyphus | `tools/sisyphus/sisyphus/prbody.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.rails` | sisyphus | `tools/sisyphus/sisyphus/rails.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.resume` | sisyphus | `tools/sisyphus/sisyphus/resume.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.runrecord` | sisyphus | `tools/sisyphus/sisyphus/runrecord.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.settled` | sisyphus | `tools/sisyphus/sisyphus/settled.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.stages` | sisyphus | `tools/sisyphus/sisyphus/stages.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.stoprecord` | sisyphus | `tools/sisyphus/sisyphus/stoprecord.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.subproc` | sisyphus | `tools/sisyphus/sisyphus/subproc.py` | migrated | none: sisyphus specific | #175 |
| `sisyphus.ticketfile` | sisyphus | `tools/sisyphus/sisyphus/ticketfile.py` | migrated | none: sisyphus specific | #175 |
| `vibe_heal` | vibe-heal | `src/vibe_heal/__init__.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.__main__` | vibe-heal | `src/vibe_heal/__main__.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.ai_tools` | vibe-heal | `src/vibe_heal/ai_tools/__init__.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.ai_tools.aider` | vibe-heal | `src/vibe_heal/ai_tools/aider.py` | dropped | none: aider backend removed | #164 (backend removed; only claude-code and opencode remain) |
| `vibe_heal.ai_tools.base` | vibe-heal | `src/vibe_heal/ai_tools/base.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.ai_tools.claude_code` | vibe-heal | `src/vibe_heal/ai_tools/claude_code.py` | migrated | `dotharness.ai_backend`, `dotharness.subprocess_runner` | #164 |
| `vibe_heal.ai_tools.external_docs` | vibe-heal | `src/vibe_heal/ai_tools/external_docs.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.ai_tools.factory` | vibe-heal | `src/vibe_heal/ai_tools/factory.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.ai_tools.gemini` | vibe-heal | `src/vibe_heal/ai_tools/gemini.py` | dropped | none: gemini backend removed | #164 (backend removed; only claude-code and opencode remain) |
| `vibe_heal.ai_tools.models` | vibe-heal | `src/vibe_heal/ai_tools/models.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.ai_tools.opencode` | vibe-heal | `src/vibe_heal/ai_tools/opencode.py` | migrated | `dotharness.ai_backend`, `dotharness.subprocess_runner` | #164 |
| `vibe_heal.ai_tools.prompts` | vibe-heal | `src/vibe_heal/ai_tools/prompts.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.ai_tools.utils` | vibe-heal | `src/vibe_heal/ai_tools/utils.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.cleanup` | vibe-heal | `src/vibe_heal/cleanup/__init__.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.cleanup.orchestrator` | vibe-heal | `src/vibe_heal/cleanup/orchestrator.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.cleanup_pr` | vibe-heal | `src/vibe_heal/cleanup_pr/__init__.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.cleanup_pr.main_duplications` | vibe-heal | `src/vibe_heal/cleanup_pr/main_duplications.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.cleanup_pr.orchestrator` | vibe-heal | `src/vibe_heal/cleanup_pr/orchestrator.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.cleanup_pr.scope` | vibe-heal | `src/vibe_heal/cleanup_pr/scope.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.cli` | vibe-heal | `src/vibe_heal/cli.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.config` | vibe-heal | `src/vibe_heal/config/__init__.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.config.exceptions` | vibe-heal | `src/vibe_heal/config/exceptions.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.config.models` | vibe-heal | `src/vibe_heal/config/models.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.config.scanner_discovery` | vibe-heal | `src/vibe_heal/config/scanner_discovery.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.converters` | vibe-heal | `src/vibe_heal/converters/__init__.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.converters.oxlint` | vibe-heal | `src/vibe_heal/converters/oxlint.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.deduplication` | vibe-heal | `src/vibe_heal/deduplication/__init__.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.deduplication.client` | vibe-heal | `src/vibe_heal/deduplication/client.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.deduplication.models` | vibe-heal | `src/vibe_heal/deduplication/models.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.deduplication.orchestrator` | vibe-heal | `src/vibe_heal/deduplication/orchestrator.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.deduplication.processor` | vibe-heal | `src/vibe_heal/deduplication/processor.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.git` | vibe-heal | `src/vibe_heal/git/__init__.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.git.branch_analyzer` | vibe-heal | `src/vibe_heal/git/branch_analyzer.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.git.diff_parser` | vibe-heal | `src/vibe_heal/git/diff_parser.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.git.exceptions` | vibe-heal | `src/vibe_heal/git/exceptions.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.git.file_selection` | vibe-heal | `src/vibe_heal/git/file_selection.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.git.manager` | vibe-heal | `src/vibe_heal/git/manager.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.git.paths` | vibe-heal | `src/vibe_heal/git/paths.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.models` | vibe-heal | `src/vibe_heal/models.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.orchestrator` | vibe-heal | `src/vibe_heal/orchestrator.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.output` | vibe-heal | `src/vibe_heal/output.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.processor` | vibe-heal | `src/vibe_heal/processor/__init__.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.processor.issue_processor` | vibe-heal | `src/vibe_heal/processor/issue_processor.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.processor.models` | vibe-heal | `src/vibe_heal/processor/models.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.review` | vibe-heal | `src/vibe_heal/review/__init__.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.review.duplication_scope` | vibe-heal | `src/vibe_heal/review/duplication_scope.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.review.github` | vibe-heal | `src/vibe_heal/review/github.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.review.line_filter` | vibe-heal | `src/vibe_heal/review/line_filter.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.review.models` | vibe-heal | `src/vibe_heal/review/models.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.review.orchestrator` | vibe-heal | `src/vibe_heal/review/orchestrator.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.review.reporter` | vibe-heal | `src/vibe_heal/review/reporter.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.sonarqube` | vibe-heal | `src/vibe_heal/sonarqube/__init__.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.sonarqube.analysis_runner` | vibe-heal | `src/vibe_heal/sonarqube/analysis_runner.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.sonarqube.client` | vibe-heal | `src/vibe_heal/sonarqube/client.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.sonarqube.exceptions` | vibe-heal | `src/vibe_heal/sonarqube/exceptions.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.sonarqube.models` | vibe-heal | `src/vibe_heal/sonarqube/models.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.sonarqube.project_manager` | vibe-heal | `src/vibe_heal/sonarqube/project_manager.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.sonarqube.properties_handler` | vibe-heal | `src/vibe_heal/sonarqube/properties_handler.py` | migrated | none: vibe-heal specific | #164 |
| `vibe_heal.utils` | vibe-heal | `src/vibe_heal/utils/__init__.py` | migrated | none: vibe-heal specific | #164 |
