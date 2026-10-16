# Orchestrator

`sisyphus.orchestrator.run_item(item, config, options, *, agent_runner, subprocess_runner, pr_client, clock=..., today=None, write_digest_file=True, lock_held=False)`
runs one work item through intake, plan, build, push, open PR and, optionally, ci-fix and merge. It never
imports `harness` or `spec_prism_flow`; those tools are reached only through the injected subprocess runner.
It returns a `RunResult` (`checkpoint`, `detail`, `stages_run`, `stages_skipped`, `branch`, `pr`, `head_sha`,
`gate_recorded`, `digest_path`, `record`, `report`; `completed` is true when no checkpoint stopped the run).

## Stages

Strictly sequential, in this order. Each stage marks the run record `in_progress` before and `complete` after it
runs (`RunRecord.stage_scope`). A stage that stops at a checkpoint stays `in_progress`.

| Stage | What runs |
|-------|-----------|
| `intake` | `intake.run_intake` (see [intake](intake.md)). Open decisions in the brief stop the run (C1). |
| `plan` | One orchestration agent run with `knowledge/sisyphus/orchestration-prompt.md`, stage `plan`. Then the leaf files in the phase directory are counted. |
| `build` | `git fetch origin <base>`, create the Sisyphus branch from `origin/<base>` (or reuse it if Sisyphus created it), one orchestration agent run with stage `build`, then check the checkout is on the branch and has commits. |
| `push` | `git push origin <branch>` through `rails.push_guard`: no force, no history rewrite, never the base or default branch, only a branch Sisyphus created. |
| `open-pr` | Reuse the open PR for the branch or open exactly one, then run the gate commands and write the gate record. |
| `ci-fix` (optional, `options.ci_fix`) | `harness run ci-fix --pr N` up to `max_fix_attempts_per_pr` times; each push is re-gated. |
| `merge` (only when `[merge].enabled`) | `harness run merge --pr N`. The orchestrator never approves. |

The branch is `sisyphus/<slug>-<8 hex of sha256(item)>` (`branch_name`). The base branch is `options.base_branch`
(a parameter, never a constant here; the CLI resolves `--base`, then `[project].base_branch`, then `main`); `options.default_branch` is an extra branch that is never pushed to.

## Checkpoints

A checkpoint records `rec.checkpoint`, adds one "Needs you" digest entry, returns it in `RunResult.checkpoint` and
runs no later stage. Run again to resume: the run starts at the stage that stopped.

| ID | Trigger in this module |
|----|------------------------|
| C1 | The brief has unresolved open decisions (from `run_intake`). |
| C2 | The plan agent writes a stop report with `C2` (`OPEN_QUESTIONS.md` has entries it cannot resolve from the repository). |
| C3 | The build agent writes a stop report with `C3` (a leaf reports an open question). |
| C4 | The build agent writes a stop report with `C4` (a deviation changes a later leaf, or it cannot tell). |
| C5 | A stop report with `C5` (a leaf exhausted its retries or left its scope); a gate command failed (no gate record is written); intake changed the target repository; the build was already stopped by C5 three times (`RetryBound`), so it is not launched again. |
| C6 | The plan has more than `max_leaves_per_run` leaves; `max_run_minutes` elapsed (checked before each stage); `max_fix_attempts_per_pr` ci-fix attempts all pushed a new head. A rerun after the leaf-limit C6 does not run the plan agent again. |
| C7 | The branch needs a rebase or force-push (remote branch not an ancestor of HEAD, or the clone cannot fast-forward after ci-fix). |
| C8 | `GitHubError` from the PR client, a failed fetch, ls-remote or push, or a non-zero `harness run ci-fix` or `harness run merge`. |

Other failures (an agent runner that raises, `IntakeError`, `RunRecordError`, `OrchestratorError`) are not
checkpoints: they propagate, the stage stays `in_progress`, and the digest is still written.

## Stop report

The orchestration agent reports a stop through a JSON file at `stop_report_path(record)` (the run record path plus
`.stop.json`), keys `checkpoint`, `leaf`, `detail`. Allowed codes: `C2` for plan, `C3`/`C4`/`C5` for build; any other
code or an unreadable file raises `OrchestratorError`. The file is deleted before each agent run. No file means the
stage finished.

## Gate record and gate commands

After the PR exists, and again after every ci-fix push, the orchestrator reads `build.commands` read-only from the
target repository's `.spec-prism-flow.toml` (`options.spf_config_path`, default `<repo_dir>/.spec-prism-flow.toml`,
stdlib `tomllib`). It runs each command in each package directory the change touched, using the injected subprocess
runner at the pushed head. A package directory is the nearest ancestor of a changed file (from
`git diff --name-only origin/<base>...HEAD`) that holds `Makefile`, `pyproject.toml`, `package.json`, `Cargo.toml` or
`go.mod`, else the repository root. If every command exits 0 it writes `gaterecord.write_gate_record` with the exact
`git rev-parse HEAD` that was tested; if one fails it writes nothing and raises C5. If the file or `build.commands` is
absent it writes nothing and the digest "Deviations" section says so; commands are never guessed.

`gaterecord.py` is stdlib only: `gate_record_path(repo_slug, pr_number, state_dir=None)`,
`write_gate_record(repo_slug, pr_number, head_sha, state_dir=None, commands=None)` (atomic: temp file, fsync,
`os.replace`), `read_gate_record`, plus the slug helpers `repo_slug_from_name` and `fallback_repo_slug`. The path is
`~/.local/share/dotharness/state/<owner-repo>/build_gate_<PR>.json` with content `{"head_sha": ..., "commands": [...],
"passed_at": ...}`; this is the file `harness/runners/merge.py` reads (it uses only `head_sha`). `state_dir` replaces
`~/.local/share/dotharness/state`.

## Injected interfaces

- `agent_runner: Callable[[str], None]` (`intake.AgentRunner`): receives a prompt, returns nothing. Used for intake,
  plan and build. The caller picks the backend.
- `subprocess_runner: Callable[[Sequence[str], Path | None], CommandResult]`: argv and cwd in,
  `CommandResult(returncode, stdout, stderr)` out. Used for every `git` call, the gate commands and the `harness`
  CLI. `default_subprocess_runner` is the real implementation for the CLI.
- `pr_client: PullRequestClient`: `find_pr(branch) -> PullRequest | None` and
  `open_pr(branch, base, title, body) -> PullRequest`. Raise `GitHubError` for an unrecoverable failure (C8).
- `clock: Callable[[], float]` (default `time.monotonic`) feeds `LimitTracker`.

`OrchestratorOptions` carries `base_branch`, `repo_slug`, `default_branch`, `ci_fix`, `spf_config_path`, `phase_dir`,
`state_dir`, `lock_dir`, `home` and `checkout_root`. `phase_dir` defaults to `plan.phase_dir` from the
`.spec-prism-flow.toml` (relative paths resolve against `[project].repo_dir`).

## Locking, digest and reruns

By default the whole run holds `lock.clone_lock(repo_dir)`; `LockHeld` propagates before any state is touched.
`lock_held=True` means the caller (a CLI command that holds one lock for its whole invocation) already holds that
lock, so `run_item` does not take it again; direct library callers should leave it `False`. The digest is
written for this item by `digest.write_digest` even when a stage raises; `DigestWriteError` propagates (chained to
the stage error, if any). A multi-item caller passes `write_digest_file=False` and aggregates `RunResult.record` and
`RunResult.report` itself.

A rerun with unchanged inputs repeats nothing: completed stages are skipped from the run record, intake skips via its
sidecar, an existing PR is reused, and no stage means no agent, subprocess or client call. A rerun that skips every
stage produces a digest listing only run-record information.

The orchestration prompt is `knowledge/sisyphus/orchestration-prompt.md`; its parameters are filled by
`build_orchestration_prompt` with paths from `paths.py`.

## Stop record, resume and the clone

When a stage raises a checkpoint, or an unexpected error escapes, `run_item` writes the stop record
(`stoprecord.py`, a sidecar next to the run record; the run record schema is unchanged) and deletes it when a
run completes. `OrchestratorOptions.resume_command` is the command text stored in it (`{item}` is replaced).
After a clean finish `run_item` calls `cleanup.cleanup_branch` (inside the lock, never turning a clean finish into an
error). Steps, each of which keeps the branch and adds a `Branch kept:` note if it fails: branch exists (else
silent no-op); clean clone; `ls-remote` and `fetch` of `origin/<branch>`; `merge-base --is-ancestor <branch>
origin/<branch>` (upstream tracking is not consulted); a detached HEAD must itself be on `origin/<branch>`; switch to
the base, `fetch` and `merge --ff-only origin/<base>`; `branch --set-upstream-to=origin/<branch>` then
`branch -d` (plain `-d` alone would refuse an unmerged PR branch; the verified upstream is git's second check).
`-D` is never used (unlike pr-review's `git_restore`, which uses `-D` after saving a recovery ref).
A complete stage is always skipped on a rerun, except intake stopped at C1, which only re-parses the brief
(`run_intake(reparse_only=True)`) so a human-edited brief is never redrafted. An answer file
(`resume.answer_path`) is appended to the next agent prompt and then renamed `*.used`. Before the first stage a
new item requires a clean clone, and a resume after the build stage checks out the item's branch; a dirty clone
is a C8 stop and is never stashed or reset. If the local branch is gone (see below) a resume from the PR stage on
fetches `origin/<branch>` and works detached at that head; earlier resumes still need the local branch.
`resume.py` holds the cleared-detection rules used by `sisyphus run`.
