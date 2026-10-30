# Orchestrator

`sisyphus.orchestrator.run_item(item, config, options, *, agent_runner, subprocess_runner, pr_client, clock=..., today=None, write_digest_file=True, lock_held=False)`
runs one work item through intake, plan, build, push, open PR. A finished item is "draft PR open, handed to the human": nothing runs after the draft PR opens. It never
imports `harness` or `spec_prism_flow`. It reaches those tools only through the injected subprocess runner.
It returns a `RunResult` with `checkpoint`, `detail`, `stages_run`, `stages_skipped`, `branch`, `pr`,
`digest_path`, `record` and `report`. `completed` is true when no checkpoint stopped the run.

## Stages

The stages are strictly sequential, in this order. Each stage marks the run record `in_progress` before and `complete` after it
runs (`RunRecord.stage_scope`). A stage that stops at a checkpoint stays `in_progress`.

| Stage | What runs |
|-------|-----------|
| `intake` | `intake.run_intake` (see [intake](intake.md)). Open decisions in the brief stop the run (C1). |
| `plan` | One orchestration agent run with `knowledge/sisyphus/orchestration-prompt.md`, stage `plan`. Then the orchestrator counts the leaf files in the phase directory. |
| `build` | Run `git fetch origin <base>`, create the Sisyphus branch from `origin/<base>` (or reuse it if Sisyphus created it), run one orchestration agent with stage `build`, then check that the checkout is on the branch and has commits. |
| `push` | `git push origin <branch>` through `rails.push_guard`: no force, no history rewrite, never the base or default branch, only a branch Sisyphus created. |
| `open-pr` | Reuse the open PR for the branch (its title and body are left as is) or open exactly one, then run the gate commands. A new PR gets a title derived from the artifacts and a body built by `sisyphus.prbody`: the header, then Summary, Phases, Decisions, Deviations and the full `requirements.md` in a `<details>` block. See below. |

The branch is `sisyphus/<slug>-<8 hex of sha256(item)>` (`branch_name`). The base branch is `options.base_branch`
(a parameter, never a constant here). The CLI resolves `--base`, then `[project].base_branch`, then `main`. `options.default_branch` is an extra branch that is never pushed to.

### Pull-request body

Only on the create path, `stage_open_pr` builds the body with the pure functions in `sisyphus.prbody` and passes it
to `open_pr`. The header (`Work item: <item>`, a blank line, `Opened by Sisyphus. Review required; this PR is not
approved.`) is unchanged and always first. The sections come from `requirements.md` sections 1-4, the leaf files
with their commits (`git log origin/<base>..HEAD`), the brief's resolved open decisions and the Deviations bullets of
the per-leaf handoffs. The plan directory is used only when its `init_manifest.json` names this item's brief.

Embedded text is sanitised: `@mentions` and `#N` references are neutralised and absolute workspace, plan
directory and home paths are removed. The body never exceeds 60,000 characters: the `<details>` block is dropped
first, then the rest is truncated with a note. A missing or unreadable artifact removes only its section (a
one-line note is left in the body); the PR is still opened and no digest note is added. A `GitHubError` from
`open_pr` still stops the run at C8.

### Pull-request title

Only on the create path, `stage_open_pr` also derives the title (`Orchestrator.build_pr_title`, using
`prbody.derive_title` and `prbody.fallback_title`) from the same artifacts as the body. The source order is:

1. The first `# ` heading of `requirements.md`, when the plan's `init_manifest.json` names this item's brief.
2. Otherwise the first `# ` heading of the brief.
3. Otherwise the fallback `Sisyphus: <item>`.

The `Requirements:` / `Brief:` label is stripped from the heading, the trailing `(#N)` is kept and no `Sisyphus:`
prefix is added. The title is one line and at most 256 characters; a longer one is cut and ends with `...`.
`@mentions` are neutralised and absolute workspace, plan directory and home paths are removed, but `#N` is left
intact so the title links to the ticket. Any failure or missing artifact gives the fallback title and never stops
the run. The title of an already-open PR is never edited.

## Checkpoints

A checkpoint records `rec.checkpoint`, adds one "Needs you" digest entry, returns it in `RunResult.checkpoint` and
runs no later stage. Run again to resume: the run starts at the stage that stopped.

| ID | Trigger in this module |
|----|------------------------|
| C1 | The brief has open decisions (from `run_intake`). |
| C2 | The plan agent writes a stop report with `C2` (`OPEN_QUESTIONS.md` has entries it cannot resolve from the repository). |
| C3 | The build agent writes a stop report with `C3` (a leaf reports an open question). |
| C4 | The build agent writes a stop report with `C4` (a deviation changes a later leaf, or it cannot tell). |
| C5 | The build agent writes a stop report with `C5` (a leaf exhausted its retries or left its scope). A gate command failed. Intake changed the target repository. The build was already stopped by C5 three times (`RetryBound`), so it is not launched again. |
| C6 | The plan has more than `max_leaves_per_run` leaves. `max_run_minutes` elapsed (checked before each stage). A rerun after the leaf-limit C6 does not run the plan agent again. |
| C7 | The branch needs a rebase or force-push (remote branch not an ancestor of HEAD). Sisyphus never rebases or force-pushes. |
| C8 | `GitHubError` from the PR client, a failed fetch, ls-remote or push. |

Other failures (an agent runner that raises, `IntakeError`, `RunRecordError`, `OrchestratorError`) are not
checkpoints. They propagate. The stage stays `in_progress`. The orchestrator still writes the digest.

## Stop report

The orchestration agent reports a stop through a JSON file at `stop_report_path(record)` (the run record path plus
`.stop.json`), with keys `checkpoint`, `leaf` and `detail`. Allowed codes: `C2` for plan, `C3`/`C4`/`C5` for build.
Any other code or an unreadable file raises `OrchestratorError`. The orchestrator deletes the file before each
agent run. No file means the
stage finished.

## Gate commands

After the PR exists, the orchestrator reads `build.commands` and `[[build.gate]]` read-only from the
item's `.spec-prism-flow.toml` (see below), a copy of `options.spf_config_path` (default
`<repo_dir>/.spec-prism-flow.toml`). The orchestrator reads it with stdlib `tomllib`. It runs the commands in the
package directories the change touched, using the injected subprocess runner at the pushed head. A package directory is the nearest ancestor of a changed file (from
`git diff --name-only origin/<base>...HEAD`) that holds `Makefile`, `pyproject.toml`, `package.json`, `Cargo.toml` or
`go.mod`. If no such ancestor holds one, the package directory is the repository root.

Plain `build.commands` strings run in every touched package directory. The optional scoped form is an array of tables:

```toml
[[build.gate]]
run = "make test"
packages = ["tools/pr-review"]
```

`run` is a required non-empty string, split with `shlex`. `packages` is an optional list of repo-relative directories
(no absolute path, no `..` segment; `"."` is the repository root). An entry with `packages` runs only in a touched
package directory that equals one of them (equality, not prefix); an entry without `packages` runs in every touched
package directory. Unknown keys or invalid values are an `OrchestratorError`. spec-prism-flow ignores `[[build.gate]]`.
Per directory, the `build.commands` run first, then the applicable entries in file order; directories run in sorted
order. If a command fails, the run stops at C5. If neither `build.commands` nor `[[build.gate]]` exists, the gate is not
run, and the digest "Deviations" section says so. If only entries exist and none applies to a touched directory, the
gate is not run and the note says `no [[build.gate]] entry applies to the touched package directories`. Commands are
never guessed. The Done entry's evidence says `gate passed` or `gate not run`.

## Injected interfaces

- `agent_runner: Callable[[str], None]` (`intake.AgentRunner`): receives a prompt, returns nothing. It is used for
  intake, plan and build. The caller picks the backend.
- `subprocess_runner: Callable[[Sequence[str], Path | None], CommandResult]`: argv and cwd in,
  `CommandResult(returncode, stdout, stderr)` out. It is used for every `git` call, the gate commands and the `harness`
  CLI. `default_subprocess_runner` is the real implementation for the CLI.
- `pr_client: PullRequestClient`: `find_pr(branch) -> PullRequest | None` and
  `open_pr(branch, base, title, body) -> PullRequest`, which creates a draft PR (the real adapter runs `gh pr create --draft`). Raise `GitHubError` for an unrecoverable failure (C8).
- `clock: Callable[[], float]` (default `time.monotonic`) feeds `LimitTracker`.

`OrchestratorOptions` carries `base_branch`, `default_branch`, `spf_config_path`, `phase_dir`,
`lock_dir`, `home`, `checkout_root` and `resume_command`. `phase_dir` defaults to `<workspace_dir>/plan-<n>/phases`.

### Per-item spec-prism-flow config

The repository's `.spec-prism-flow.toml` is shared by every item, so its `plan.workspace_dir` and `plan.phase_dir`
belong to whichever item ran last. Before each agent stage, the leaf count and the gate commands, the orchestrator
writes `<workspace_dir>/plan-<n>/.spec-prism-flow.toml`: the repository's file (`spf_config_path`) with `plan.workspace_dir`
set to `<workspace_dir>/plan-<n>` and `plan.phase_dir` set to `phase_dir` or `<workspace_dir>/plan-<n>/phases`. The agents
receive this path as `phase config path`, and the orchestrator reads `build.commands` and counts leaves from it. The
repository's file is never modified. Build-stage handoff summaries are saved as
`<workspace_dir>/plan-<n>/handoffs/NN-<leaf name>.md`, and the orchestrator creates the `handoffs/` directory before the build stage.

## Locking, digest and reruns

By default the whole run holds `lock.clone_lock(repo_dir)`. `LockHeld` propagates before the run touches any state.
`lock_held=True` means the caller (a CLI command that holds one lock for its whole invocation) already holds that
lock, so `run_item` does not take it again. Direct library callers should leave it `False`. `digest.write_digest` writes the digest for this item even when a stage raises. `DigestWriteError` propagates
(chained to the stage error, if any). A multi-item caller passes `write_digest_file=False` and aggregates `RunResult.record` and
`RunResult.report` itself.

A rerun with unchanged inputs repeats nothing. It skips completed stages from the run record. Intake skips via its
sidecar. It reuses an existing PR. A skipped stage means no agent, subprocess or client call. A rerun that skips
every stage produces a digest listing only run-record information.

The orchestration prompt is `knowledge/sisyphus/orchestration-prompt.md`. `build_orchestration_prompt` fills its
parameters with paths from `paths.py`.

## Stop record, resume and the clone

When a stage raises a checkpoint, or an unexpected error escapes, `run_item` writes the stop record
(`stoprecord.py`, a sidecar next to the run record). The run record schema is unchanged. `run_item` deletes the
stop record when a run completes. `OrchestratorOptions.resume_command` is the command text stored in the stop
record (`{item}` is replaced).
After a clean finish, `run_item` calls `cleanup.cleanup_branch` (inside the lock, never turning a clean finish into an
error). Each precondition keeps the branch and adds a `Branch kept:` note if it fails. The preconditions are: the
branch exists (else silent no-op), and the clone is clean. Then one of three proofs must hold. The orchestrator
checks them cheapest first. Once an earlier proof holds, it does not evaluate the later ones, so `gh` is only called
when git cannot prove it. Proof (1): `ls-remote` + `fetch` of `origin/<branch>` and
`merge-base --is-ancestor <branch> origin/<branch>` (upstream tracking is not consulted). Proof (2): fetch of the
base, and the head is an ancestor of `origin/<base>` (merge-commit/rebase merge, remote branch gone or not).
Proof (3): `PullRequestClient.pr_state(ref)` (`gh pr view <ref> --json number,state,headRefOid`) is MERGED and
`headRefOid` equals the local head. `ref` is the PR number stored in the run record (`retries["pr-number"]`,
written by the open-pr stage) or the branch. If none holds, the note reads
`no deletion proof held: (1) ...; (2) ...; (3) ...`.
A detached HEAD must itself be on the branch, `origin/<branch>` or `origin/<base>`. Then switch to the base, run
`fetch`, and run `merge --ff-only origin/<base>`. Then run `branch -d`. After proof (1), first run
`--set-upstream-to=origin/<branch>` (plain `-d` would refuse an unmerged PR branch. The verified upstream is
git's second check). After proof (2), the head is an ancestor of the checked-out base, so `-d` accepts it as is.
Proof (3) cannot satisfy `-d` (a squash-merged head is in neither HEAD nor an upstream). So there, and only there,
`_delete_squash_merged` runs `git branch -D <branch>` after the clone is on a clean, fast-forwarded base. `-D` is
never issued on proofs 1 or 2 (a refusal of `-d` there still keeps the branch). `--force` is never used. The
orchestrator creates no recovery ref or helper ref. The reflog is the last-resort recovery (decided by the user).
The Done line names the proof used. For proof 3, it carries the FULL 40-character head SHA, the PR number and
`git fetch origin pull/<N>/head` (GitHub keeps the commit in the merged PR's history).
A complete stage is always skipped on a rerun, except intake stopped at C1, which only re-parses the brief
(`run_intake(reparse_only=True)`) so a human-edited brief is never redrafted. The orchestrator appends the answer
file (`resume.answer_path`) to the next agent prompt and then renames it `*.used`. Before the first stage, a new
item requires a clean clone, and a resume after the build stage checks out the item's branch. A dirty clone is a
C8 stop. The orchestrator never stashes or resets it. If the local branch is gone (see below), a resume from the
PR stage on fetches `origin/<branch>` and works detached at that head. Earlier resumes still need the local
branch.
`resume.py` holds the cleared-detection rules used by `sisyphus run`.
