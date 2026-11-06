# Shared async layer: subprocess runner and AI backend launcher

Status: implemented in #164 (I-2). Code: `src/dotharness/subprocess_runner.py`
(`run`, `RunResult`) and `src/dotharness/ai_backend.py` (`launch`, `launch_sync`).

Launch logic for `claude` and `opencode` existed in five places that disagreed on flags,
kill signals and prompt delivery. This document records the comparison, one decision per
difference, and the contract for I-3, I-4 and I-5. Comparison inputs live under `tools/`
and are read only: nothing under `tools/` is changed by #164.

## Copies compared

| Copy | Where | Backends | Prompt delivery | Timeout / kill | Result on failure |
| --- | --- | --- | --- | --- | --- |
| pr-review `_cmd_for` | `tools/pr-review/harness/backend.py` | claude, opencode | file under `~/.local/share/dotharness/tmp`, argv "Read <path> and follow the instructions exactly." | `proc.communicate(timeout)`, SIGKILL to the group on timeout | `CompletedProcess`; `TimeoutExpired` raised |
| pr-review `run_cmd` | `tools/pr-review/harness/runners/common.py` | any command | n/a | SIGTERM to the group, `GRACE_PERIOD_SECONDS = 10`, SIGKILL | `CalledProcessError` when `check`, `TimeoutExpired` raised |
| spec-prism-flow Claude | `tools/spec-prism-flow/.../build/claude_backend.py` | claude | file, "Read" prompt | `DEFAULT_TIMEOUT_SECONDS = 1800`, `_KILL_GRACE_SECONDS = 5`, SIGKILL to the group | `ClaudeCommandError` on non-zero, `TimeoutExpired` raised |
| spec-prism-flow opencode | `tools/spec-prism-flow/.../build/opencode_backend.py` | opencode | file, "Read" prompt | same constants, SIGKILL to the group | `OpencodeCommandError` on non-zero |
| sisyphus `cli.py` | `tools/sisyphus/sisyphus/cli.py` | claude, opencode | prompt in argv | none in the command table | n/a |
| sisyphus `subproc.py` | `tools/sisyphus/sisyphus/subproc.py` | any command | n/a | `subprocess.run(timeout)`, no group kill | `CommandResult`; missing executable 127, timeout 124 |
| vibe-heal `claude_code.py` | `src/dotharness/vibe_heal/ai_tools/` | claude | file in cwd, "Read" prompt | `asyncio.timeout(600)` (ctor default 300), `process.kill()` of the child only | `FixResult`; `TimeoutError` caught |
| vibe-heal `opencode.py` | same | opencode | file in cwd, "Read" prompt | `asyncio.timeout(1200)`, child only | `FixResult` |
| vibe-heal `utils.py` | same | any command | `temp_prompt_file` | child kill, no group | `CommandResult(success, stdout, stderr, exit_code)` |

Argv per copy:

| Copy | claude | opencode |
| --- | --- | --- |
| pr-review | `claude --dangerously-skip-permissions --disable-slash-commands -p <text>` | `opencode run --standalone --auto [--dir <d>] <text>` |
| spec-prism-flow | same as pr-review | `opencode run <prompt> --standalone --dir <cwd>` (never a permission flag, no `--auto`) |
| sisyphus | `claude -p <prompt> --dangerously-skip-permissions` (`PERMISSION_FLAG`) | `opencode run <prompt>` |
| vibe-heal | `claude --print <file prompt> --output-format json --permission-mode acceptEdits --allowedTools Edit,Read` | `opencode run <file prompt> [-m <model>]` |

## Decisions per difference

| Difference | Decision | Reason when not adopted |
| --- | --- | --- |
| `--standalone` (pr-review, spec-prism-flow yes; sisyphus, vibe-heal no) | Adopted as a parameter (`standalone`), off by default. pr-review callers pass it. | Not forced on: it isolates sessions and is only wanted by callers that run concurrent sessions. |
| `--auto` (pr-review only) | Adopted as a parameter (`auto`), off by default. | Not forced on: it auto-approves permission prompts, which a caller must opt into. |
| `--dir` | Adopted as a parameter (`opencode_dir`). `cwd` is always passed to the child separately. | spec-prism-flow passes `--dir` and `cwd`; both are kept independent. |
| SIGTERM vs SIGKILL | SIGTERM to the process group, 5 s grace (`KILL_GRACE_SECONDS`), then SIGKILL to the group; same on cancellation; a SIGKILL sweep after a normal exit. | spec-prism-flow's direct SIGKILL and vibe-heal's child-only kill are not adopted: they give no clean shutdown, and vibe-heal leaves grandchildren running. pr-review's 10 s grace is shortened to 5 s (spec-prism-flow value). |
| Permission flags | Caller chooses: `skip_permissions`, `permission_mode`, `disable_slash_commands`, `allowed_tools`; none on by default. The launcher holds no vibe-heal or pr-review values. | A default of `--dangerously-skip-permissions` is not adopted: sisyphus and pr-review want it, vibe-heal must not have it. |
| Prompt file vs argv | File by default (`prompt_via_file=True`) under `~/.local/share/dotharness/tmp` (directory forced to 0700; file from `mkstemp`: `O_EXCL`, random name, 0600), removed in `finally` (also on cancellation). Inline argv via `prompt_via_file=False`. | vibe-heal's file in cwd is not adopted: it litters the working tree. sisyphus's argv prompt is kept as an option for short prompts. |
| 127 / 124 vs exceptions | `RunResult` with `not_found` (127) and `timed_out` (124) flags; the launcher does not raise for these. Guard failures raise typed errors. | pr-review and spec-prism-flow raise `TimeoutExpired`/`CalledProcessError`: callers that want that can check the flags. Return codes alone are ambiguous, hence the flags. |
| 1800 s default (spec-prism-flow) | No default: `timeout` is required. | A silent default hides a decision. Per-caller values: pr-review config, spec-prism-flow 1800, vibe-heal Claude 1200 (D-6, implemented). |
| opencode without a permission flag (spec-prism-flow) | Adopted: `opencode` never receives `--dangerously-skip-permissions` (opencode 2.0 dropped it); `auto` is the only approval flag. | n/a |
| Global opencode lock (pr-review `_OPENCODE_GLOBAL_LOCK_KEY`) | Dropped. | opencode 2.0 no longer needs it. |

pr-review `Backend` behaviours kept, each optional by parameter: retries (`max_retries`),
cwd-divergence monitor (`monitor_cwd`, uses `lsof`), `opencode plugin list` check
(`check_plugins`), repo-identity guard (`expected_repo_name`), harness-repo guard
(`guard_harness_repo`), `PWD` pinned to `cwd`, and the wait for the process group to exit
(with a warning when members survive, and a by-name check for escaped daemonised children
after a timeout-kill).

Changes from pr-review worth knowing: retries cover timeouts only unless `retry_on_nonzero=True` is passed
(same as pr-review; a failed run can already have posted replies or pushed commits, so re-running it is opt-in); the plugin check runs once per `launch` call (pr-review: once per `Backend`
instance and cwd); the survivor check logs pids only, never command lines.

## Treatment of the other copies

- spec-prism-flow `claude_backend.py` / `opencode_backend.py` and sisyphus
  `cli.py` / `subproc.py`: not migrated, edited or deprecated in #164. They stay as they
  are until I-3, I-4 and I-5 move each tool onto this layer.
- pr-review `backend.py`: replaced in I-3 by a synchronous adapter over `launch_sync` (see "I-3 adapter contract"). pr-review `runners/common.py` `run_cmd` (generic `gh`/`git` runner, not a backend launcher) is rebuilt in I-3 as a sync wrapper over `subprocess_runner.run` (see "I-3 `run_cmd` replacement").
- vibe-heal `ai_tools/`: migrated in the next leaf of #164.

## Contract for I-3, I-4 and I-5

```python
from dotharness.ai_backend import ClaudeOptions, OpencodeOptions, launch, launch_sync
from dotharness.subprocess_runner import RunResult, run

async def launch(
    backend: str,            # "claude" or "opencode"; anything else raises ValueError
    prompt: str,
    *,
    cwd: str | Path,         # required; also becomes PWD in the child
    timeout: float,          # required, seconds; no default
    env: Mapping[str, str] | None = None,       # added to os.environ
    path_prepend: Sequence[str] = (),
    prompt_via_file: bool = True,
    context: str | None = None,                 # log prefix only
    max_retries: int = 0,
    retry_on_nonzero: bool = False,    # retry also after a non-zero exit; timeouts are always retried
    expected_repo_name: str | None = None,      # "owner/repo"
    guard_harness_repo: bool = False,
    harness_repo_root: str | Path | None = None,
    backend_options: ClaudeOptions | OpencodeOptions | None = None,  # type must match `backend`; TypeError otherwise
) -> RunResult
```

`ClaudeOptions` (`print_mode`, `print_flag`, `skip_permissions`, `permission_mode`, `disable_slash_commands`,
`output_format`, `allowed_tools`) and `OpencodeOptions` (`standalone`, `auto`, `opencode_dir`, `model`,
`monitor_cwd`, `check_plugins`) are frozen dataclasses; every flag is off by default.

`launch_sync` has the same signature and calls `launch` through `asyncio.run`; it must not
be called from a running event loop. It is documented for the sync tools (I-3, I-4, I-5)
and is not wired into any of them yet.

- Result: `RunResult(returncode, stdout, stderr, timed_out=False, not_found=False)`, the
  last attempt's. It never carries argv or env.
- Plain non-zero exit, timeout (`timed_out`, 124) and missing executable (`not_found`, 127)
  are returned, not raised. Retries (`max_retries`) re-run after a timeout, and after a non-zero exit
  only with `retry_on_nonzero=True` (default `False`); never after `not_found`; the last result is returned.
- Raised: `ValueError` (unknown backend, option of the other backend), `OpencodePluginError`,
  `BackendCwdDivergedError` (not retried), `RepoIdentityError`.
- A caller must pass `backend`, `prompt`, `cwd` and `timeout`, and choose every permission
  and isolation flag itself. Reference mappings: pr-review claude =
  `skip_permissions=True, disable_slash_commands=True`; pr-review opencode =
  `standalone=True, auto=True, monitor_cwd=True, check_plugins=True`; vibe-heal claude =
  `print_flag="--print", output_format="json", permission_mode="acceptEdits",
  allowed_tools="Edit,Read"`.
- Prompt file outside cwd: verified with a real `claude --print --permission-mode acceptEdits --allowedTools Edit,Read` run (cwd = a repo, prompt under `~/.local/share/dotharness/tmp`): the `Read` is not denied, because `--allowedTools Read` pre-approves it for any path. So no `add_dirs` parameter and no prompt file in cwd; I-3/I-4/I-5 copy the reference mappings as written. A caller that omits `Read` from `allowed_tools` and has no `skip_permissions` would be denied. Opt-in live test: `DOTHARNESS_LIVE_CLAUDE=1 pytest tests/test_ai_backend.py -k outside_cwd`.
- Cancelling the task kills the process group and removes the prompt file.
- Neither argv nor env is logged or placed in exception text. Callers must keep to this.
- Not provided: lock files, `gh`, config loading, `build_subprocess_env`.
- Decision D-6: the Claude timeout in vibe-heal is one value, 1200 s (`CLAUDE_TIMEOUT_SECONDS` in `ClaudeCodeTool`). It is passed to the launcher, which enforces it, and printed in the timeout message. The old 300 s constructor default and the literal 600 s wrapper are removed. `ClaudeCodeTool` and `OpenCodeTool` call `launch` only; their prompt files now live under `~/.local/share/dotharness/tmp` instead of the working tree.

## I-3 adapter contract and hardening map

`dotharness.pr_review.backend.Backend` keeps its constructor
`Backend(backend, timeout, path_prepend, env_vars, max_retries=1, *, expected_repo_name=None)` and
`run(instructions, cwd, opencode_dir, context)`. Internally it calls `launch_sync` only; `_cmd_for`, `_build_command` and
the Popen/killpg code are deleted. It must not be called from async code.

Adapter contract:

- Flags: claude `skip_permissions=True, disable_slash_commands=True`; opencode `standalone=True, auto=True,
  monitor_cwd=True, check_plugins=True` (+ `opencode_dir`). The adapter also passes `expected_repo_name` and
  `guard_harness_repo=True`, because guards are opt-in in the shared layer. Argv is pinned in tests through
  `build_claude_command` and `build_opencode_command`; it equals the old argv.
- Retry default: the adapter keeps `max_retries=1` (old) and passes `max_retries` to `launch_sync` explicitly; the shared
  default is 0. It leaves `retry_on_nonzero` at `False`, so only timeouts are retried (old behaviour). A non-zero exit is
  run once: `address-comments` and `auto-approve` are not idempotent (replies, pushes) and must not run twice.
- Bytes vs text: the shared `RunResult` is text; the adapter returns `subprocess.CompletedProcess` with stdout/stderr
  encoded to UTF-8 bytes (`args` holds only the backend name; the shared layer never exposes argv).
- Timeout (`timed_out`, 124): raises `subprocess.TimeoutExpired` (old behaviour).
- Non-zero exit: returned as `CompletedProcess` with that return code (old behaviour: `_execute` returned it; old callers
  check `returncode`). `CalledProcessError` is never raised.
- Missing executable (`not_found`, 127): returned as `CompletedProcess(returncode=127)`. The old code let `Popen` raise
  `FileNotFoundError`; every old caller of `Backend.run` either checks `returncode` or catches `Exception`, so a returned 127
  is handled the same way as any failed run. This is a deliberate change.
- Propagated unchanged: `ValueError`, `TypeError`, `OpencodePluginError`, `BackendCwdDivergedError`, `RepoIdentityError`
  (`dotharness.repo_guard`; re-exported from `pr_review.backend`).
- Accepted I-2 differences (see "Decisions per difference"): SIGTERM to the group then SIGKILL after
  `KILL_GRACE_SECONDS = 5` (old: SIGKILL); plugin check once per launch call; survivor
  check logs pids only; prompt files use the `dotharness_` prefix in a 0700 directory.
- Logging (D-9): the adapter logs no argv, env, or backend stdout/stderr. The old log of up to 2000 characters of failed-run
  output is dropped.

Hardening map (every retained item from the old `Backend`):

| Item | Class | Where |
| --- | --- | --- |
| Process-group SIGTERM, grace, SIGKILL on timeout and cancellation | shared layer | `subprocess_runner.run` |
| Prompt written to a private temp file, removed after each attempt | shared layer | `ai_backend._write_prompt_file` |
| `PWD` pinned to `cwd` | shared layer | `ai_backend._build_env` |
| `path_prepend` and extra env merged into the child env | shared layer | `ai_backend._build_env` |
| Wait (bounded) for the process group to exit; warn on survivors | shared layer | `ai_backend._wait_for_group_exit` |
| By-name survivor check after a timeout-kill (pids only) | shared layer | `ai_backend._warn_if_backend_survived` |
| cwd-divergence monitor (`lsof`) and `BackendCwdDivergedError` (not retried) | shared layer | `opencode_guards.watch_cwd` |
| `opencode plugin list` check | shared layer | `opencode_guards.assert_no_opencode_plugins` |
| Repo-identity check before and after each attempt; logged after a timeout-kill | shared layer | `repo_guard.assert_repo_identity` |
| Harness-repo HEAD unchanged guard | shared layer | `repo_guard.assert_harness_unchanged` |
| Retry loop mechanics | shared layer | `ai_backend._run_attempts` |
| Choice of flags: `skip_permissions`, `disable_slash_commands`, `standalone`, `auto`, `monitor_cwd`, `check_plugins` | pr-review policy | `Backend.run` |
| Enabling guards: `expected_repo_name`, `guard_harness_repo` | pr-review policy | `Backend.run` |
| Retry count (`max_retries`, adapter default 1) | pr-review policy | `Backend.__init__` |
| Timeout value and `TimeoutExpired` / `CompletedProcess` result shape | pr-review policy | `Backend.run` |
| Per-working-dir `fcntl.flock` lock files | pr-review policy | `pr_review/lock.py` (kept) |
| Global opencode lock | dropped (D-1) | opencode 2.0 no longer needs it |

## I-3 `run_cmd` replacement

Decision OD-1: option (a). `dotharness.pr_review.runners.common.run_cmd` keeps its signature
`run_cmd(cmd, cwd, env, timeout, check=True, shell=False)` but is a thin sync wrapper over `subprocess_runner.run`
(through `asyncio.run`, so it must not be called from async code). Option (b) was rejected: it would keep a second
`Popen`/`killpg` implementation, the duplication ticket #62 names, and the shared runner covers every old behaviour
except the gaps listed below. `GRACE_PERIOD_SECONDS` is deleted. `src/dotharness/pr_review` contains no `os.killpg`
and no `start_new_session` (AC-2).

Mapping of `RunResult` back to the old contract:

- Bytes vs text (OD-3): every caller of `run_cmd` received bytes (`.stdout.decode(...)`, `text(...)`); callers are not
  edited. The wrapper encodes the shared text result to UTF-8. Bytes that are not valid UTF-8 were already replaced
  with U+FFFD by the shared layer, so such output is lossy where the old code returned the raw bytes.
- `timed_out`: raises `subprocess.TimeoutExpired(cmd, timeout, output=<bytes or None>, stderr=<bytes or None>)`.
  `stderr` now ends with the shared `timed out after {timeout}s` line.
- Non-zero exit with `check`: raises `subprocess.CalledProcessError(returncode, cmd, stdout, stderr)`; without `check`
  the `CompletedProcess` is returned. A child killed by a signal has a negative code, as before.
- `not_found` (OD-2): the old `Popen` raised `FileNotFoundError` (an `OSError`) for a missing executable or a missing
  `cwd`, not a `CalledProcessError`. The wrapper raises `FileNotFoundError(<error text>)`. The shared layer folds every
  `OSError` into `not_found`, so a `PermissionError` on exec now also surfaces as `FileNotFoundError`. With
  `shell=True` a missing command is still a shell exit code 127, as before.
- `shell=True`: mapped to `["/bin/sh", "-c", cmd]` (a list `cmd` becomes `["/bin/sh", "-c", *cmd]`, as `Popen` does on
  POSIX). A `str` `cmd` without `shell` is a single program name, as with `Popen`.
- Accepted differences: the grace period is 5 s (`KILL_GRACE_SECONDS`), not 10 s (FR-3, not reintroduced); stdin is
  `/dev/null` (old: inherited); a `SIGKILL` sweep of the process group also runs after a normal exit, so a background
  process a command started on purpose no longer outlives the call.
- Logging (D-9): the wrapper logs nothing; the shared runner never carries argv or env.

Hardening map (every item found in the old `run_cmd`):

| Item | Class | Where |
| --- | --- | --- |
| Child in its own session (`start_new_session`) so it leads a process group | shared layer | `subprocess_runner.run` |
| Timeout: SIGTERM to the group, grace period, SIGKILL to the group, reap the child | shared layer | `subprocess_runner._kill_group` |
| Exceptions are raised only after the child is reaped (no zombie, no orphan group on timeout) | shared layer | `subprocess_runner.run` |
| stdout and stderr captured through pipes | shared layer | `subprocess_runner.run` |
| `cwd` and explicit `env` passed to the child | shared layer | `subprocess_runner.run` |
| Grace period length | shared layer | `KILL_GRACE_SECONDS = 5` (replaces `GRACE_PERIOD_SECONDS = 10`) |
| `TimeoutExpired` raised on timeout | pr-review policy | `run_cmd` |
| `CalledProcessError` when `check` and exit code is non-zero | pr-review policy | `run_cmd` |
| Bytes `CompletedProcess` result shape | pr-review policy | `run_cmd` |
| `FileNotFoundError` for a missing executable or `cwd` | pr-review policy | `run_cmd` |
| `shell` mode and `str` command handling | pr-review policy | `_argv` in `runners/common.py` |
| Timeout values (`TIMEOUT_GH`, `TIMEOUT_GIT`, per-call budgets) | pr-review policy | `runners/common.py` and callers |

## I-3 packaging-sensitive paths and repo guard

The old `tools/pr-review` code assumed it ran from a source checkout. Installed as a wheel, files move into
`site-packages`, which may sit inside an unrelated git repository. This section records the decisions (D-1, D-2, D-3) and
the `__file__` audit (baseline `cb0efc1`).

### D-1: harness-repo guard and `harness_repo_root`

Decision: pr-review passes `guard_harness_repo=True` and an explicit `harness_repo_root` whenever it can identify the
dotharness source checkout, and skips the harness-HEAD guard otherwise.

- The shared `snapshot_harness_repo(cwd, root)` falls back to `git rev-parse --show-toplevel` from
  `dotharness/repo_guard.py`'s own directory when `root` is None. Installed, that directory is in `site-packages`, so the
  guard would pin an unrelated repository or silently watch nothing. pr-review therefore never relies on that default.
- `pr_review.backend.find_harness_checkout()` returns the checkout root, or None. It is pure path inspection (no git, no
  subprocess): a parent of `backend.py` qualifies only when it has a `.git` entry (directory, or file for a worktree)
  and `backend.py` lives at `<parent>/src/dotharness/pr_review/`. A `site-packages` install inside an unrelated repo has
  no such layout, so it returns None. An editable install resolves into the checkout and is found.
- Source of the root, in precedence order: `Backend(..., harness_repo_root=<Path>)` (explicit, new keyword-only argument,
  default None), then `find_harness_checkout()`. There is no config key and no environment variable (YAGNI; add a
  `[harness]` key later if an operator needs to point at a different checkout).
- Root absent (installed package): the guard is skipped (`guard_harness_repo=False`, `harness_repo_root=None`), logged at
  debug level. Fail-open is chosen because there is no checkout to watch and failing closed would make every installed
  package unusable. The primary defence, the repo-identity check (`expected_repo_name`, `assert_repo_identity`), is
  unaffected and stays enabled.
- Root present but git cannot read its HEAD: the shared layer raises `RepoIdentityError` (fail closed), as before.
- The shared layer is unchanged (`src/dotharness/repo_guard.py` still discovers from its module directory when called
  without a root; other callers may rely on that).

### D-2: `harness.repo_guard` is replaced, not kept

Decision: the pr-review sync module `repo_guard.py` is deleted; `dotharness.repo_guard` (shared, async) replaces it.
Ledger row `harness.repo_guard` (`tools/pr-review/harness/repo_guard.py`): status is replaced by
`src/dotharness/repo_guard.py`.

| Old function | Replacement |
| --- | --- |
| `RepoIdentityError` | `dotharness.repo_guard.RepoIdentityError` (re-exported by `pr_review.backend`) |
| `assert_repo_identity` | `dotharness.repo_guard.assert_repo_identity`, called by the shared launcher via `Backend` (phase 05) |
| `discover_repo_root`, `assert_repo_unchanged` | `snapshot_harness_repo` / `assert_harness_unchanged`, called by the shared launcher (`guard_harness_repo`) |
| SSH host alias resolution (`_resolve_ssh_host`, `_origin_matches_repo`) | same names in the shared module (async, with a fail-closed fallback) |
| `head_sha` (used by `local_review/review.py` for its own mutation guard) | `review._read_head_sha`, a 5-line helper over the existing `git()` wrapper; it raises the shared `RepoIdentityError` |

After phase 05 the only remaining user of the sync module was `local_review/review.py`, which needed only `head_sha`, so
no sync-to-async bridge is required. Keeping the sync module would have left two `RepoIdentityError` classes. The tests
of the old module moved to `tests/test_repo_guard.py` (async, against the shared layer).

### D-3 and the `Path(__file__)` audit

Every `__file__`-relative lookup in `tools/pr-review` at `cb0efc1` (`git grep __file__`; nothing else in the package uses
`dirname`, `importlib.resources`, `inspect.getfile` or `.parents[`). D-3 answer: yes, there is one more production lookup
beyond `_HARNESS_REPO_ROOT`, plus test-only lookups.

| File (old) | Expression | Locates | Disposition |
| --- | --- | --- | --- |
| `harness/backend.py:43` | `_HARNESS_REPO_ROOT = discover_repo_root(Path(__file__).resolve().parent)` | the checkout to watch with the harness-HEAD guard | Replace: removed with the Phase 05 adapter; root now comes from `find_harness_checkout()` / `harness_repo_root` (D-1) |
| `harness/runners/common.py:52` | `PR_COMMENTS_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "pr-comments.py"` | `tools/pr-review/scripts/pr-comments.py`, run as `python3 <script> fetch --pr N` | Replace with a packaged resource or config. Resolved in phase 07: `pr-comments.py` is packaged as package data at `src/dotharness/scripts/pr-comments.py`, which is where the expression resolves (see below) |
| `tests/runners/test_focused_review.py:220` | `Path(focused_review.__file__).read_text()` | module source, for a static source guard | Keep (module-relative by design; works installed from source) |
| `tests/runners/test_focused_review.py:720` | `Path(__file__).resolve().parents[4] / "knowledge" / "pr-review" / "focused-review.md"` | repo-root `knowledge/` template | Keep, depth adjusted to `parents[3]` (phase 01); the root `knowledge` is a symlink to the package |
| `tests/runners/test_local_review.py:40` | `Path(__file__).resolve().parents[4] / "knowledge" / "pr-review"` | repo-root `knowledge/` | Keep, `parents[3]` (phase 01) |
| `tests/runners/test_local_review.py:559,576` | `Path(lr.__file__).parent` | package dir, for source guards | Keep |
| `tests/runners/test_local_review_findings.py:43`, `test_local_review_store.py:63` | `Path(<module>.__file__).parent` | package dir, for source guards | Keep |
| `tests/runners/test_merge.py:264` | `Path(__file__).resolve().parents[3] / "contracts/gate_record_golden.json"` | gate-record fixture | Keep, `parents[2]` into `tests/contracts/` (phase 01) |

Not a `Path(__file__)` lookup but listed for completeness: `src/dotharness/repo_guard.py:108` (shared layer, new tree) uses
`Path(__file__).resolve().parent` as the discovery fallback in `snapshot_harness_repo`; pr-review avoids it (D-1).

`PR_COMMENTS_SCRIPT_PATH` follow-up: resolved in phase 07 (commit 31843c4). `pr-comments.py` ships as package data at
`src/dotharness/scripts/pr-comments.py` (hatch `packages = ["src/dotharness"]` includes it in the wheel; a wheel build lists
`dotharness/scripts/pr-comments.py`), and the `Path(__file__)` expression in `runners/common.py` resolves to it, so
`address-comments`, `focused-review` and `regret-review` find it when installed.
`tests/test_package_data.py::test_pr_comments_script_is_package_data_and_matches_runner_path` pins this.

### Knowledge and vendor defaults (D-7, D-10)

- `knowledge_dir` (`[harness] knowledge_dir`) defaults to `~/.harness/knowledge`. When the key is not set and that directory
  does not exist, `config.default_knowledge_dir()` resolves to the packaged `src/dotharness/knowledge/` via
  `importlib.resources.files("dotharness") / "knowledge"` (a plain path: wheels are installed unpacked). An explicit
  `knowledge_dir` is never replaced, so `validate` still reports a configured path that does not exist. The fallback
  applies to `knowledge_dir` only. Note: the `init` template in `cli.py` writes `knowledge_dir = "~/.harness/knowledge"`
  explicitly, which opts out of the fallback.
- `vibe_types_repo` (`[focused_review]`) defaults to `~/.harness/vendor/vibe-types` with no packaged fallback: it is an
  external vendor checkout.
- `~/.harness/cache/pr-<n>-comments.json` (`fetch_pr_comments`) is unchanged.
