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
(`harness_repo_root`), `PWD` pinned to `cwd`, and the wait for the process group to exit
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
- Not provided: lock files, `gh`, config loading. `build_subprocess_env` lives in `subprocess_runner.py` (F-DUP-7).
- Decision D-6: the Claude timeout in vibe-heal is one value, 1200 s (`CLAUDE_TIMEOUT_SECONDS` in `ClaudeCodeTool`). It is passed to the launcher, which enforces it, and printed in the timeout message. The old 300 s constructor default and the literal 600 s wrapper are removed. `ClaudeCodeTool` and `OpenCodeTool` call `launch` only; their prompt files now live under `~/.local/share/dotharness/tmp` instead of the working tree.

## I-3 adapter contract and hardening map

`dotharness.pr_review.backend.Backend` keeps its constructor
`Backend(backend, timeout, path_prepend, env_vars, max_retries=1, *, expected_repo_name=None)` and
`run(instructions, cwd, opencode_dir, context)`. Internally it calls `launch_sync` only; `_cmd_for`, `_build_command` and
the Popen/killpg code are deleted. It must not be called from async code.

Adapter contract:

- Flags: claude `skip_permissions=True, disable_slash_commands=True`; opencode `standalone=True, auto=True,
  monitor_cwd=True, check_plugins=True` (+ `opencode_dir`). The adapter also passes `expected_repo_name` and
  `harness_repo_root`, because guards are opt-in in the shared layer. Argv is pinned in tests through
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
| Enabling guards: `expected_repo_name`, `harness_repo_root` | pr-review policy | `Backend.run` |
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

Decision: pr-review passes an explicit `harness_repo_root` whenever it can identify the
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
- Root absent (installed package): the guard is skipped (`harness_repo_root=None`), logged at
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
| `discover_repo_root`, `assert_repo_unchanged` | `snapshot_harness_repo` / `assert_harness_unchanged`, called by the shared launcher (`harness_repo_root`) |
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

## I-6 analysis (#168, R1)

Scope: analysis only, no source change (G4). Tree read: dotharness `77aa8b7` (I-5 merged). Evidence is a path plus matched
text, re-greppable with `grep -rn '<text>' <path>`. No secret file was read and no secret value is quoted (NG6).

Finding IDs are `F-<area>-<n>`. Verdicts are exactly `apply now`, `defer` or `no action (justified)`. An `apply now`
finding names the implementing phase of the I-6 plan (Phase NN). Behaviour differences accepted in I-3 (grace 5 s, stdin
`/dev/null`, `PermissionError` surfacing as `FileNotFoundError`, see `docs/migration-ledger.md`) are not re-opened (NG8).
Open defects #87-#92, #107-#117, #127, #136, #161 are out of scope (NG2).

Shared layer after I-6 (module names fixed here, D-6): `subprocess_runner.py` (extended with the group-kill helper),
`ai_backend.py`, `paths.py` (data directory), plus new modules created by the named phases: a `gh` wrapper, a file-lock
module, an atomic-write helper, a TOML loader and the env builder. Each phase states the exact file name in its own
handoff; the AC3 table below lists the allowed files by finding ID.

Sonar duplication figure (D-9): not measurable here (no scanner access in this slice). It is recorded at the triage
checkpoint (Phase 03) next to this manual inventory, which is the other input.

### a. Duplicates

| ID | Evidence | Verdict |
| --- | --- | --- |
| F-DUP-1 | Process-group kill: `subprocess_runner.py` `os.killpg(pgid, sig)` in `_killpg`; `opencode_guards.py` `os.killpg(pgid, sig)` in `_kill_group`; `ai_backend.py` `os.killpg(pgid, 0)` in `_group_alive`. The first two are identical (suppress `ProcessLookupError`, then `killpg`). The `0` probe is a liveness check, not a kill, with a different `PermissionError` rule (alive). | apply now (Phase 04): one public kill helper in `subprocess_runner.py`, used by `opencode_guards.py`; the probe moves next to it as a sibling helper in the same module (one `os.killpg` home).Landed: `subprocess_runner.killpg(pgid, sig)` and `subprocess_runner.group_alive(pgid)`; `opencode_guards._kill_group` removed; `ai_backend` imports `group_alive` under its old private name `_group_alive` because `tests/test_ai_backend.py` patches it. No behaviour difference found between the probe and the kill copies beyond the documented `PermissionError` rule (probe: alive; kill: propagates), so no new decision ID. |
| F-DUP-2 | `gh` invocation: `pr_review/runners/*.py` (`common.py`, `ci_checks.py`, `auto_approve.py`, `auto_approve_github.py`, `address_comments.py`, `review_requested.py`, `self_review.py`, `regret_review.py`) match `"gh",` via `run_cmd`; `vibe_heal/review/github.py` `async def _run_gh` and `_post_json` (own `create_subprocess_exec`, 60 s `asyncio.timeout`); `sisyphus/pick.py` `class GhCli`; `scripts/pr-comments.py` `run(["gh", *args])`; `pr_review/cli.py` `shutil.which("gh")`. | apply now (Phase 06): one `gh` wrapper over `subprocess_runner.run`. `scripts/pr-comments.py` is a special case, see F-DUP-8. Landed (Phase 06): `subprocess_runner.gh_argv(*args)` is the only module naming `"gh"` (constant `GH`); the three sites build argv through it and keep their own adapters (`run_cmd`, `_run_gh`, `GhCli`), all unchanged in timeout, error mapping and decoding. `vibe_heal._run_gh` still runs on `run_command` until the Phase 07 routing; the findings about differing timeouts and decoding stay as recorded, no behaviour was unified. `pr_review/cli.py` `shutil.which("gh")` and `scripts/pr-comments.py` stay as special cases (F-DUP-8). |
| F-DUP-3 | File locks: `pr_review/lock.py` `fcntl.flock(fd, fcntl.LOCK_EX)`; `pr_review/state.py` `class _state_lock` `fcntl.flock(self._fd, fcntl.LOCK_EX)`; `sisyphus/lock.py` `fcntl.flock(fd, fcntl.LOCK_EX \| fcntl.LOCK_NB)`. Variations: blocking vs `LOCK_NB`; contention raises `SystemExit` (pr_review) vs `LockHeld` (sisyphus); lock dir `<data>/locks` vs `<data>/sisyphus/locks`; `open("w")` vs `os.open(..., 0o600)`. | apply now (Phase 09): one lock implementation with blocking and non-blocking modes and a typed contention error; callers keep their current exit behaviour. Landed (Phase 09): `fileio.py` `file_lock(path, *, blocking=False)` raises `LockContended`; `pr_review/lock.py` maps it to `SystemExit`, `sisyphus/lock.py` to `LockHeld`, `pr_review/state.py` `_state_lock` uses it blocking. Behaviour note (D-20): lock files are now always created with mode 0o600 (pr_review used `open("w")`, state `open("a")`: umask mode); lock files are empty and existing files keep their mode |
| F-DUP-4 | Data directory literal: `ai_backend.py` `TMP_DIR = Path.home() / ".local/share/dotharness/tmp"`; `pr_review/lock.py` `XDG_RUNTIME = Path.home() / ".local/share/dotharness"`; `pr_review/state.py` `XDG_DATA = ...`; `pr_review/cli.py` `XDG_LOGS = ...`; `pr_review/scheduler.py` `log_dir = Path.home() / ".local/share/dotharness/logs"` (twice); `pr_review/runners/local_review/store.py` `DEFAULT_OUTPUT_ROOT = Path("~/.local/share/dotharness/reviews")`; `sisyphus/lock.py` `DEFAULT_LOCK_DIR`; `sisyphus/runrecord.py` `DEFAULT_STATE_DIR`. `paths.py` has only `default_knowledge_dir()`. The name `XDG_RUNTIME` is also wrong (it is the data dir). | apply now (Phase 09): one `data_dir()` in `paths.py`; all eight sites derive from it. Landed (Phase 09): `paths.py` `DATA_DIR_TILDE` (the only literal) and `data_dir()` (`Path.home()` based); the three sites that used unexpanded `"~/..."` defaults (`store.py`, `runrecord.py`, `sisyphus/lock.py`) derive from `DATA_DIR_TILDE` and keep their lazy expansion. Constant names `XDG_DATA`, `XDG_RUNTIME`, `XDG_LOGS` kept because tests patch them (D-21) |
| F-DUP-5 | Atomic writes: `pr_review/state.py` `def _atomic_write` (`tmp.rename`, no fsync); `pr_review/runners/local_review/store.py` `def _atomic_write_text` (`tmp.replace`, no fsync); `sisyphus/runrecord.py` `def atomic_write_json` (`mkstemp`, `fsync`, `os.replace`). | apply now (Phase 09): one helper, the `sisyphus/runrecord.py` semantics (fsync, `os.replace`, cleanup on failure). This strengthens durability of the two weaker copies and changes no format; logged as a behaviour note. Landed (Phase 09): `fileio.py` `atomic_write_text` and `atomic_write_json`; `state.py` writes `json.dumps(data)` (no newline, byte-identical content) through `atomic_write_text`; `store.py` uses it directly; `sisyphus/runrecord.py` imports `atomic_write_json` (re-exported for `stoprecord.py`). Behaviour note (D-22): the two weaker copies now fsync and use `os.replace`; temp names changed to `.tmp-*` |
| F-DUP-6 | TOML loading: `tomllib.load` in `pr_review/config.py`, `spec_prism_flow/config.py`, `sisyphus/config.py`; `tomllib.loads` in `sisyphus/plan.py` (`read_spf_config`) and `sisyphus/orchestrator.py` (re-reads `.spec-prism-flow.toml`). Each wraps `TOMLDecodeError` into its own `ConfigError`. | apply now (Phase 10): one loader returning the dict or raising one error type; the three `ConfigError` classes stay and wrap it. `sisyphus/orchestrator.py` and `plan.py` share one spf-config read. **Landed (Phase 10)**: `fileio.load_toml(path)` (+ `TOMLDecodeError` alias) is the only `tomllib` user; each caller keeps its own missing-file handling and `ConfigError`/`OrchestratorError` wrapping and message, so no behaviour difference found. `orchestrator.py` already delegates to `plan.read_spf_config` (one read path; the two reads can still happen at different times, unchanged). |
| F-DUP-7 | Env building: `def build_subprocess_env` in `pr_review/runners/common.py`, imported by 12 modules, all under `pr_review`. Other env merges: `sisyphus/subproc.py` `{**os.environ, **env}`, `vibe_heal/git/branch_analyzer.py` `{**os.environ, "GIT_TERMINAL_PROMPT": "0"}`. | apply now (Phase 05): move `build_subprocess_env` to the shared layer; `pr_review/runners/common.py` re-exports nothing. The two inline merges are covered by F-DUP-9. |
| F-DUP-8 | Special cases kept outside the shared runner, each justified: `pr_review/scheduler.py` and `pr_review/cli.py` `subprocess.run(["crontab", ...])` and `launchctl` (fixed argv, sync, no timeout needed, setup commands run once interactively); `spec_prism_flow/handoff.py` `subprocess.run(cmd, input=text.encode(), capture_output=True, timeout=3)` (best-effort clipboard, 3 s bound); `scripts/pr-comments.py` (standalone stdlib script run as a child process, `src/dotharness/scripts/pr-comments.py`, must not import `dotharness`); `sisyphus/intake.py` `_git` (D-17) and `sisyphus/cli.py` `run_token_cmd` default `run=subprocess.run` (D-18) (D-18). `vibe_heal/git/manager.py` and `vibe_heal/git/branch_analyzer.py` are no longer special cases: converted to async and routed through `subprocess_runner.run` in Phase 12 (D-16 closed). | no action (justified), grep-allowed `subprocess.run` files: `pr_review/scheduler.py`, `pr_review/cli.py`, `spec_prism_flow/handoff.py`, `sisyphus/intake.py`, `sisyphus/cli.py`, `scripts/pr-comments.py`. |
| F-DUP-9 | Subprocess invocation outside the runner: `subprocess.run` in `git_utils.py` (`git_toplevel`), `vibe_heal/git/branch_analyzer.py` (`_run_git`, `stdin=subprocess.DEVNULL`), `vibe_heal/git/manager.py` (pre-commit hook), `sisyphus/intake.py` (`def _git`), `sisyphus/cli.py` (`run_token_cmd(... run: Callable = subprocess.run)`). `create_subprocess_exec` in `vibe_heal/review/github.py` (`_post_json`) and `vibe_heal/ai_tools/utils.py` (`async def run_command`, a third `run_command` next to `sisyphus/subproc.py` and `run_cmd`). `vibe_heal/sonarqube/analysis_runner.py` already uses `run`. | apply now: core modules and `vibe_heal` Phase 07; `pr_review`, `spec_prism_flow`, `sisyphus` Phase 08. Justified exceptions are listed in F-DUP-8. Landed (Phase 07): `git_toplevel` (sync bridge `asyncio.run(run(...))`, only called outside an event loop) and `vibe_heal/ai_tools/utils.py` `run_command` (now `await run(..., cwd=Path.cwd(), timeout=math.inf)`; `not_found` re-raised as `FileNotFoundError`; callers keep their own `asyncio.timeout`) go through `subprocess_runner.run`. `repo_guard.py` and `vibe_heal/sonarqube/analysis_runner.py` were already routed (D-14). `vibe_heal/review/github.py` `_post_json` now goes through `run(cmd, timeout=60, stdin=json.dumps(payload))`: `timed_out` maps to `GitHubTimeoutError` (same message), `not_found` re-raises `FileNotFoundError`, non-zero exit keeps the `stderr — stdout` `GitHubReviewError` text; the 12 tests that patched `asyncio.create_subprocess_exec` now patch `github.run`, with the same asserted argv, payload and error mapping. `vibe_heal/git/manager.py` and `vibe_heal/git/branch_analyzer.py` were special cases (D-16) until Phase 12 routed them through `run` (async, callers await). Behaviour differences accepted via I-3 (D-15) and logged: stdin `/dev/null` (was inherited) for `git_toplevel` and `run_command` (`_post_json` already piped stdin), 5 s SIGTERM grace before SIGKILL on cancel (was immediate kill), lenient UTF-8 decode (was strict, also `_post_json`), `PermissionError` surfaces as `FileNotFoundError`, post-exit SIGKILL sweep of the process group, timeout message text of `git_toplevel` differs, `_post_json` kill now SIGTERM then SIGKILL of the group instead of `process.kill`. Phase 08 (D-17, D-18, D-19): no call in `pr_review`, `spec_prism_flow`, `sisyphus` or the pr-comments script was routed; each stays a justified special case in F-DUP-8. Reasons: crontab/launchctl need `check=True` raising and text stdin, which `RunResult` lacks, and routing would impose the I-3 stdin and grace changes on interactive setup commands; the clipboard call needs bytes stdin, `TimeoutExpired` handling and a 3 s bound; `intake._git` returns raw bytes that feed content hashing while `RunResult.stdout` is `str` decoded with `errors="replace"`, so non-UTF-8 paths from `git status -z` would not round-trip (D-17); `run_token_cmd`'s injectable `run=subprocess.run` is a test-facing interface (D-18); `sisyphus/orchestrator.py` has no `subprocess.run` and `default_subprocess_runner` already delegates to `sisyphus/subproc.run_command` over `run` (D-19, no occurrence). `scripts/pr-comments.py` at the repo root does not exist, only `src/dotharness/scripts/pr-comments.py`: the duplicate is already gone, nothing deleted. |
| F-DUP-10 | Backend selection: validation of `claude`/`opencode` repeated in `pr_review/config.py` (`if backend not in ("opencode", "claude")`), `pr_review/backend.py` (`raise ValueError(f"Unknown backend`), `spec_prism_flow/config.py` (`def parse_backend`), `sisyphus/config.py` (`AGENT_BACKENDS = ("claude", "opencode")`); `vibe_heal/ai_tools/factory.py` selects by `AIToolType` and builds its own tools over `launch`. Dispatch itself is central in `ai_backend.py`. | apply now (Phase 10): one backend-name tuple and validator in `ai_backend.py`; `vibe_heal` factory stays (it selects tool classes, not backends): no action (justified) for the factory. **Landed (Phase 10)**: `ai_backend.BACKEND_NAMES` is the one tuple; `pr_review/config.py`, `pr_review/backend.py`, `spec_prism_flow/config.parse_backend` test membership in it and `sisyphus/config.AGENT_BACKENDS` is an alias; accepted values, defaults and messages unchanged. Messages stay local (they differ per caller), so `select_backend` is not used by them. No behaviour difference found. |

AC3 allowed files (finding IDs), per pattern over `src/dotharness`:

- `os.killpg`: `subprocess_runner.py` only (F-DUP-1).
- `fcntl`: `fileio.py` only (F-DUP-3).
- `local/share/dotharness`: `paths.py` only (F-DUP-4).
- `tomllib`: the one loader (F-DUP-6).
- `"gh"`: the `gh` wrapper (F-DUP-2) and `pr_review/cli.py` `shutil.which("gh")` precheck (kept, F-DUP-2 resolves it through the wrapper if trivial, otherwise listed here).
- `subprocess.run`: `pr_review/scheduler.py`, `pr_review/cli.py`, `spec_prism_flow/handoff.py`, `sisyphus/intake.py`, `sisyphus/cli.py` (the `run=subprocess.run` default), `scripts/pr-comments.py` (F-DUP-8, D-17, D-18).
- `create_subprocess_exec`: `subprocess_runner.py` only (F-DUP-9).
- `def _atomic_write|def atomic_write`: `fileio.py` only (F-DUP-5).
- `def build_subprocess_env`: the shared-layer module (F-DUP-7).

### b. Sync versus async

Measured sizes (lines, `src/dotharness`): `pr_review/runners` 9,591 (largest `common.py` 1,566, `regret_review.py` 973), `pr_review` core 1,535, `spec_prism_flow` 2,243, `sisyphus` 5,464 (`orchestrator.py` 1,018, `cli.py` 874), `vibe_heal` 11,588, shared layer 898 (`ai_backend.py` 455, `subprocess_runner.py` 166).

Adapters, each running a fresh event loop per call: `pr_review/backend.py` (`launch_sync`), `pr_review/runners/common.py` `run_cmd` (`asyncio.run(run(_argv(cmd, shell), ...))`), `sisyphus/subproc.py` `run_command` (`asyncio.run(`), `spec_prism_flow/prose_review.py` (`launch_sync`). `ai_backend.py` `launch_sync` raises when called inside a running loop.

Wall-clock of one adapter call path, current tree, `["true"]`, mean of 30 calls on the analysis host [I: only a before figure is possible now]:

| Path | ms per call, before | ms per call, after Phase 12 (two runs) |
| --- | --- | --- |
| `sisyphus.subproc.run_command` (new loop per call) | 13.7 | 13.7, 13.8 |
| `pr_review.runners.common.run_cmd` (new loop per call) | 13.4 | 13.6, 13.8 |
| `subprocess_runner.run` awaited in one loop | 13.0 | 13.6, 13.4 |
| `pr_review.runners.common.run_cmd`, async since Phase 13 (awaited in one loop, no adapter) | 13.6, 13.6, 13.8 (re-measured at Phase 12 HEAD, same script) | 12.9, 13.6, 13.6 |

The loop-per-call cost is about 0.5 ms, below the process spawn cost. Conversion is therefore not a speed fix. It removes the "must not be called from a running loop" restriction, the sync-to-async wrapper layer and makes concurrency possible; the owner decision is to convert (brief, open question 2). The "after" column was measured by Phase 12 with the same script (`["true"]`, 30 calls after one warm-up). The paths are unchanged in Phase 12, so the figures stay within noise (about 0.5 ms). `launch_sync`, `sisyphus/subproc.py` and `spec_prism_flow/prose_review.py` stay until their last sync callers convert in Phases 16-17 (R6); Phase 12 converted only `vibe_heal/git/*` (R10).

Phase 13 (Phases 13 and 14 merged by owner decision): `run_cmd` is now `async def`, awaited directly (measured with `bench2`, the same `["true"]` x 30 calls after one warm-up, on the same host, three runs each side). The before runs are the Phase 12 HEAD sync adapter, the after runs are the awaited coroutine: 13.6-13.8 ms versus 12.9-13.6 ms per call. This is a no-regression result within noise (about 0.5 ms), plus the removed `asyncio.run`; no regression to flag. The pr_review `Backend.run` is `async def` over `launch`. The conversion covered `runners/common.py` and all runner leaves (merge, focused_review, review_prs, ci_checks, auto_approve, regret_review, address_comments, ci_fix, review_requested, self_review), the files no leaf owned (`auto_approve_github.py`, `auto_approve_size.py`, `prune_projects.py`, `local_review/{address,review,git,prompts}.py`) and `pr_review/backend.py`, about 240 functions. Conversion is one-for-one `await`: no new `gather`, same call order. Two blocking calls became awaitable equivalents: the page-retry `time.sleep` in `_fetch_page` is `await asyncio.sleep`, and `preserve_reviewer_request` is an `asynccontextmanager` (`async with`). `pr_review/cli.py` calls each runner entry point through one `asyncio.run(...)` per command; `run_all` still has one `asyncio.run` per runner (Phase 15 merges them into a single coroutine).

| ID | Group | Evidence | Verdict |
| --- | --- | --- | --- |
| F-ASYNC-1 | shared layer | `ai_backend.py` `def launch_sync` and `asyncio.run(func(*args, **kwargs))` | apply now (Phase 12): `launch`/`run` stay async; `launch_sync` and the adapters are removed once the last sync caller is converted (kept until Phases 16-17 land; pr_review no longer uses it since Phase 13, remaining callers `sisyphus/agent.py`, `spec_prism_flow/prose_review.py`). |
| F-ASYNC-2 | `pr_review/runners` | `common.py` `asyncio.run(run(_argv(cmd, shell)` in `run_cmd`; sync `def` throughout the runners | apply now (Phases 13 common and small runners, 14 large runners). Landed (Phase 13, merged with 14): positive verdict per D-2 (no regression, adapter removed). `grep -rn 'asyncio.run' src/dotharness/pr_review/runners` has no match; the only `asyncio.run` left in `pr_review` are the boundary calls in `cli.py` (Phase 15). |
| F-ASYNC-3 | `pr_review` core | `pr_review/backend.py` `from dotharness.ai_backend import ClaudeOptions, OpencodeOptions, launch_sync`; `cli.py` calls runners synchronously | apply now (Phase 15): one `asyncio.run` at the CLI boundary. Phase 13 already moved `backend.py` to `launch` (`Backend.run` is `async def`) and gave `cli.py` a per-command `asyncio.run`; Phase 15 only has to merge the `run_all` calls. `scheduler.py` crontab/launchctl calls stay sync (F-DUP-8). |
| F-ASYNC-4 | `spec_prism_flow` | `prose_review.py` `result = launch_sync(`; `handoff.py` sync clipboard | apply now (Phase 17); `handoff.py` stays sync (F-DUP-8). |
| F-ASYNC-5 | `sisyphus` | `subproc.py` `result = asyncio.run(`; `orchestrator.py` and `cli.py` sync | apply now (Phase 16). |
| F-ASYNC-6 | `vibe_heal` | `async def` in 17 modules; `vibe_heal/cli.py` ten `asyncio.run(` calls, one per command (the boundary) | no action (justified): already async at the boundary. Residual sync `subprocess.run` is F-DUP-9. |

### c. Naming, layout and module placement

| ID | Evidence | Verdict |
| --- | --- | --- |
| F-NAME-1 | Three subprocess helpers with near-identical names: `run_cmd` (`pr_review/runners/common.py`), `run_command` (`sisyphus/subproc.py`), `run_command` (`vibe_heal/ai_tools/utils.py`). | apply now, resolved by F-DUP-9 (Phases 07, 08, 13): the adapters disappear, `subprocess_runner.run` remains. |
| F-NAME-2 | `XDG_RUNTIME`, `XDG_DATA`, `XDG_LOGS` all name `Path.home() / ".local/share/dotharness"` subdirectories (`pr_review/lock.py`, `state.py`, `cli.py`); none uses `XDG_*` environment variables. | apply now, resolved by F-DUP-4 (Phase 09). |
| F-NAME-3 | Root-level modules of one family: `ai_backend.py`, `opencode_guards.py`, `repo_guard.py`, `git_utils.py`, `subprocess_runner.py`, `paths.py` sit flat beside four tool packages; `sisyphus` keeps `lock.py`, `subproc.py`, `runrecord.py` that duplicate them (F-DUP-3, F-DUP-5, F-DUP-9). | defer: flat root is acceptable for six small modules; moving them churns imports and the ledger, which is not asked for. Revisit if the shared layer grows past about 10 modules. |
| F-NAME-4 | Hyphenated module `src/dotharness/scripts/pr-comments.py` is not importable; it is referenced by path (`PR_COMMENTS_SCRIPT_PATH`, `tests/test_package_data.py` `test_pr_comments_script_is_package_data_and_matches_runner_path`). | no action (justified): the path-run script needs no import, and renaming changes the runner path contract. |
| F-NAME-5 | Package names are underscored (`pr_review`, `spec_prism_flow`, `vibe_heal`), console scripts match (`[project.scripts]`), docs and ledger use hyphens in prose (`pr-review`, `spec-prism-flow`) and `zensical.toml` nav (`"pr-review"`, `"spec-prism-flow"`). | no action (justified): module names are Python identifiers; prose names are historical project names. Phase 20 states the mapping once in the docs. |

### d. Dependency hygiene (G5)

| ID | Evidence | Verdict |
| --- | --- | --- |
| F-DEP-1 | `uv run deptry src`: "No dependency issues found" (138 files). `uv lock --locked`: "Resolved 81 packages". `grep -rn "import click\|from click" src`: empty. | no action (justified): G5 holds on the current tree. |
| F-DEP-2 | `Makefile` `check` runs `uv run deptry src`; `.github/workflows/dotharness-main.yml` runs `uv run deptry . --extend-exclude "tools\|vendor"` (scans 140 files, including `tests` and `scripts`). Two commands, two scopes. `[tool.deptry]` has only `known_first_party`. | apply now (Phase 18): one command, `uv run deptry src`, in `make check` and CI. |
| F-DEP-3 | Dev-group `ruff==0.17.3` equals `.pre-commit-config.yaml` `rev: "v0.17.3"`; `tox.ini` `uv run --active ty check`. | no action (justified): versions agree. |
| F-DEP-4 | `tools/` and `vendor/` directories remain (`tools/pr-review`, `tools/sisyphus`, `tools/spec-prism-flow`, `tools/contracts`; `vendor/vibe-types` and others), excluded by `pyproject.toml` `[tool.ruff]` `extend-exclude`, `[tool.ty.src]` `exclude`, `.pre-commit-config.yaml` `exclude: ^(tools\|vendor)/`, `sonar-project.properties` `sonar.exclusions`. Removal of old trees belongs to I-7/I-8. | defer: record for the I-7 hand-off list (R7); not removed here. |

### e. CLI error and exit conventions

Counts per CLI module (`grep -c`): `vibe_heal/cli.py` `sys.exit(` 41, `typer.Exit` 0; `pr_review/cli.py` `sys.exit(` 6, `typer.Exit` 1, `_fail(` 3; `spec_prism_flow/cli.py` `sys.exit(` 0, `typer.Exit` 2, `_fail(` 10; `sisyphus/cli.py` `sys.exit(` 0, `typer.Exit` 6, `_fail(` 15.

Proposed convention: `def _fail(message) -> NoReturn` printing `Error: <message>` to stderr and raising `typer.Exit(1)`, as in `spec_prism_flow/cli.py`. `sisyphus/cli.py` returns the exception for `raise _fail(...)` and keeps its text.

| ID | Evidence | Verdict |
| --- | --- | --- |
| F-CLI-1 | `sisyphus/cli.py` `def _fail(message: str) -> typer.Exit` (returns) vs `spec_prism_flow/cli.py` and `pr_review/cli.py` `def _fail(message: str) -> NoReturn` (raises). Same text and exit code 1. | apply now (Phase 11): one shared `fail` in a small CLI helper module; output text unchanged. |
| F-CLI-2 | `pr_review/cli.py` `sys.exit(1)` at 6 sites next to `typer.Exit`/`_fail`. | apply now (Phase 11): convert to the convention where stdout/stderr text and exit code are unchanged. `sys.exit(code)` that forwards a runner status stays. |
| F-CLI-3 | `vibe_heal/cli.py` 41 `sys.exit(` sites, messages printed through rich helpers. `tests/vibe_heal/test_cli_surface.py` pins command names, options and defaults only, and its `exit_code` appears 3 times; `tests/vibe_heal/test_cli.py` asserts `exit_code` 44 times but does not assert the stderr text of the failing paths. Exit code and text equality is therefore not provable by those two files. | defer: convert only after those tests pin stdout/stderr text for the failing paths; list it for the I-7 hand-off. |
| F-CLI-4 | `pr_review/lock.py` `raise SystemExit(f"Another instance is already running for {lock_key}")` in a library function, documented in its docstring as escaping `except Exception` on purpose. | no action (justified) for the exit mechanism; the lock moves under F-DUP-3 with the same behaviour. |

### f. Test layout and helpers

Layout: 151 files under `tests/`; `tests/pr_review`, `tests/sisyphus`, `tests/spec_prism_flow`, `tests/vibe_heal` mirror the packages; root files `test_ai_backend.py`, `test_subprocess_runner.py`, `test_repo_guard.py`, `test_git_utils.py`, `test_paths.py`, `test_package_data.py`, `test_pr_comments_script.py`, `test_ledger_modules.py`; `tests/contracts/`. `pytest` runs with `--import-mode=importlib` and `asyncio_mode = "auto"` (`pyproject.toml`), so same-basename files across tool directories collect (`test_layout_sample.py` in two directories).

| ID | Evidence | Verdict |
| --- | --- | --- |
| F-TEST-1 | `conftest.py` files: `tests/conftest.py` (7 lines, `runner` fixture), `tests/pr_review/conftest.py` (`tmp_xdg`, `minimal_toml`), `tests/sisyphus/conftest.py` (`_isolated_home`, `fake_run_item`), `tests/vibe_heal/conftest.py` (3 lines); `tests/spec_prism_flow` has none. `tests/vibe_heal/shared_orchestrator_tests.py` is a helper module with a `_tests.py` suffix that pytest does not collect by the default pattern. Home isolation exists only for sisyphus. | defer: after F-DUP-4 lands, one autouse home isolation in `tests/conftest.py` is possible; not needed for any apply-now change. |
| F-TEST-2 | Git repo setup helper copied: `def _git(` in `tests/test_ledger_modules.py`, `tests/pr_review/test_state.py`, `tests/pr_review/runners/test_local_review.py`, `test_local_review_head_sha.py`, `test_auto_approve_size.py`, `tests/sisyphus/test_orchestrator.py`, `test_intake.py`; `def _init_repo(` in `tests/test_repo_guard.py`. Per-test `def fake_run(cmd, **kwargs)` / `def fake_run_cmd(cmd, **kwargs)` stubs are repeated in `tests/pr_review/test_scheduler.py` (13 times), `tests/pr_review/runners/test_review_prs.py` (7), `tests/vibe_heal/git/test_manager.py`, `tests/vibe_heal/cleanup_pr/test_orchestrator.py`, `tests/test_pr_comments_script.py` and others. | defer: a shared `tests/helpers` module is useful, but moving 8 copies is test churn with no behaviour effect. Revisit after Phases 13-17, which rewrite many of these fakes anyway. |
| F-TEST-3 | Each phase changes tests with its code; `tests/sisyphus/test_atomic_write.py`, `tests/sisyphus/test_lock.py`, `tests/pr_review/test_lock.py`, `tests/pr_review/test_state.py` cover the pieces F-DUP-3 and F-DUP-5 merge. | apply now (Phases 09): their assertions move to one test file for the shared helper; the old files keep behaviour tests of their callers. |

### g. Docs and ledger consistency

| ID | Evidence | Verdict |
| --- | --- | --- |
| F-DOC-1 | `docs/contributing.md`: "I-6 to I-8 target `main` after I-5 merges." `README.md`: "I-6 to I-8 target `main` after I-5 merges." For this issue the base branch is `sisyphus/164-c86e8a65` (D-5), so the rule text is misleading. | apply now (Phase 20). |
| F-DOC-2 | `docs/contributing.md` "Coverage policy": "the floor is set in I-6 ... at the level then reached"; table of per-tool figures; `codecov.yml` header repeats it and `flags: sisyphus: paths: - tools/sisyphus/`. | apply now (Phase 18): replace with the project floor of the section h and drop the stale per-tool figures. |
| F-DOC-3 | Every tool has a page: `docs/pr_review.md`, `docs/spec_prism_flow.md`, `docs/sisyphus.md`, `docs/vibe_heal.md`; nav in `zensical.toml` lists `{ "sisyphus" = "sisyphus.md" }` with no subpages while the other three have subtrees; `docs/design/shared-async-layer.md` is not in the nav. | defer: not a consistency defect; Phase 20 decides whether to add the design page to the nav. |
| F-DOC-4 | Conventions live only in `docs/contributing.md` and `docs/design/shared-async-layer.md`; no `AGENTS.md`, `CLAUDE.md` or ADR. `docs/contributing.md` "Excludes for tools and vendor" says "Later phases may remove these excludes". | apply now (Phase 20): add a conventions section (CLI convention F-CLI-1, shared-layer module list, finding-ID rule). No ADR directory is created. |
| F-DOC-5 | `docs/migration-ledger.md`: Phase 01 set the 20 `sisyphus.*` rows to `migrated`. The ledger has no value for "duplicate remaining" (brief open question 9: owner left tracking to the SonarQube figure). | defer: Phase 20 freezes the ledger as is; remaining duplicates are tracked by this document and Sonar. |

### h. CI, gates and coverage

State: `ruff` rules YTT, S, B, A, C4, T10, SIM, I, C90, E, W, F, PGH, UP, RUF, TRY with ignores E501, E731, `target-version = "py311"`, `line-length = 120`; per-file ignore `tests/*` = S101, S603; `ty` `python-version = "3.11"`. `.github/workflows/dotharness-main.yml` matrix 3.11, 3.12, 3.13, 3.14; coverage uploaded from 3.12 only.

Gates run on this tree: `uv run ruff check` all passed; `uv run ruff format --check` 355 files already formatted; `uv run ty check` all passed; `uv run deptry src` no issues; `uv lock --locked` resolved; `uv run python -m pytest` 3448 passed, 2 skipped (about 4 min 47 s).

Measured project-wide coverage (`pytest --cov --cov-config=pyproject.toml --cov-report=xml`, `branch = true`, source `dotharness`): lines 13,279 of 14,373 covered, branches 3,853 of 4,382, combined (coverage.py `TOTAL`) 91.35 %, line-rate 0.9239, branch-rate 0.8793. **Recommended `fail_under = 91`** in `[tool.coverage.report]` (integer below 91.35). The Phase 18 sibling re-measures in CI before applying; if the CI figure is below 91.0 it uses the integer below that figure, and never lowers it afterwards.

| ID | Evidence | Verdict |
| --- | --- | --- |
| F-CI-1 | `pyproject.toml` `[tool.coverage.report]` has `skip_empty = true` and no `fail_under`; `Makefile` `test` and the `Run tests` step have no threshold. | apply now (Phase 18): `fail_under = 91`, wired through the existing `--cov` commands. |
| F-CI-2 | `codecov.yml` `status: project: dotharness: informational: true` and the `sisyphus` flag at `tools/sisyphus/`. | apply now (Phase 18): header and statuses match F-CI-1; the `sisyphus` status stays until I-8 as the file says. |
| F-CI-3 | Old per-tool workflows still exist: `.github/workflows/main.yml` (`working-directory: tools/pr-review`), `sisyphus-main.yml` (`tools/sisyphus`), `spec-prism-flow-main.yml` (`tools/spec-prism-flow`), `docs.yml` (builds three `tools/*` sites). | defer: they belong to the old trees (F-DEP-4); I-7/I-8 hand-off list. |
| F-CI-4 | CI `quality` job has no explicit `ruff format --check`; `pre-commit run -a` runs `ruff-format` (`.pre-commit-config.yaml`). | no action (justified): format is enforced through pre-commit. |
| F-CI-5 | `tox.ini` runs `pytest --doctest-modules tests`, CI runs `pytest tests` without it. | no action (justified): tox is a local convenience; no doctests live under `tests`. |
| F-CI-6 | Any new `# noqa` or per-file ignore needs a finding ID. `grep -rn "noqa" src` finds 169 lines today, each with a rule code. | apply now (every phase): new suppressions cite an `F-` ID in the PR description. |

### i. Packaging and install

| ID | Evidence | Verdict |
| --- | --- | --- |
| F-PKG-1 | `pyproject.toml` `[tool.hatch.build.targets.wheel] packages = ["src/dotharness"]`; `[project.scripts]` `vibe_heal = "dotharness.cli:vibe_heal"` and three others. `Makefile` `build` runs `pyproject-build`; no test builds a wheel or installs it. | apply now (Phase 19): wheel build and clean-environment install smoke test per tool. |
| F-PKG-2 | `tests/test_package_data.py` sets `PACKAGE_DIR = REPO_ROOT / "src" / "dotharness"` and `resources.files("dotharness")`; under an editable install both resolve to the source tree, so the test proves repository layout, not installed resources. | apply now (Phase 19): the smoke test checks `knowledge/`, `skills/` and `scripts/pr-comments.py` inside the installed wheel. |
| F-PKG-3 | Root `knowledge` and `skills` are symlinks into `src/dotharness/` (`test_root_knowledge_is_symlink_to_package`). Hatch packages the real directories. | no action (justified): verified by F-PKG-2 once the smoke test exists. |

### j. Security findings (record only, NG7)

| ID | Evidence (paths only, no values) | Verdict |
| --- | --- | --- |
| F-SEC-1 | #136: `vibe_heal/ai_tools/claude_code.py` `error_message=f"Claude failed with exit code {result.returncode}: {result.stderr}"` and `vibe_heal/ai_tools/opencode.py` the same for OpenCode; `vibe_heal/sonarqube/analysis_runner.py` `error(f"    Scanner error: {combined[:500]}")` and `error_msg = f"sonar-scanner failed with exit code {returncode}: {combined}"`. Backend or scanner output reaches logs and messages unredacted. | defer: tracked by #136 (NG2). |
| F-SEC-2 | SonarQube token on the scanner argv: `vibe_heal/sonarqube/properties_handler.py` `command.append(f"-Dsonar.token={self.config.sonarqube_token}")` (and `-Dsonar.password=`), run through `analysis_runner.py` `await run(command, cwd=project_dir, ...)`. The value is visible in the process table while the scanner runs. | defer: needs a scanner properties file or environment variable instead of argv; changes behaviour, not part of this pass. |
| F-SEC-3 | `ai_backend.py` `cmd.append("--dangerously-skip-permissions")`; `sisyphus/agent.py` docstring: "accepted risk: the boundary is the dedicated clone". | no action (justified): documented, accepted risk in a dedicated clone. |

### k. Triage table and approval checkpoint (R2, D-1)

Verdict is exactly `apply now`, `defer` or `no action (justified)`. PR cites the implementing plan phase; one concern per
PR, titles and bodies cite finding IDs, base branch `sisyphus/164-c86e8a65` (D-5, PR rules: `docs/contributing.md`). #80 and
#82 are folded in as findings (D-4). The Sonar duplication figure (D-9) was not obtainable in this pass (no scanner access);
Sonar stays the duplicate tracker and no ledger status or column is added.

| Finding | Verdict | Estimated PR |
| --- | --- | --- |
| F-DUP-1 | apply now | Phase 04 |
| F-DUP-2 | apply now | Phase 06 |
| F-DUP-3 | apply now | Phase 09 |
| F-DUP-4 | apply now | Phase 09 |
| F-DUP-5 | apply now | Phase 09 |
| F-DUP-6 | apply now | Phase 10 |
| F-DUP-7 | apply now | Phase 05 |
| F-DUP-8 | no action (justified): fixed-argv setup commands, best-effort clipboard and a stdlib-only child script stay outside the runner | none |
| F-DUP-9 | apply now | Phases 07, 08 |
| F-DUP-10 | apply now | Phase 10 |
| F-ASYNC-1 | apply now | Phase 12 |
| F-ASYNC-2 | apply now | Phases 13, 14 |
| F-ASYNC-3 | apply now | Phase 15 |
| F-ASYNC-4 | apply now | Phase 17 |
| F-ASYNC-5 | apply now | Phase 16 |
| F-ASYNC-6 | no action (justified): `vibe_heal` is already async at the CLI boundary | none |
| F-NAME-1 | apply now | Phases 07, 08, 13 (via F-DUP-9) |
| F-NAME-2 | apply now | Phase 09 (via F-DUP-4) |
| F-NAME-3 | defer | none (revisit if the shared layer passes about 10 modules) |
| F-NAME-4 | no action (justified): the path-run script needs no import and a rename changes the runner path contract | none |
| F-NAME-5 | no action (justified): module names are identifiers, prose names are historical | none (mapping in Phase 20 docs) |
| F-DEP-1 | no action (justified): G5 holds on the current tree | none |
| F-DEP-2 | apply now | Phase 18 |
| F-DEP-3 | no action (justified): ruff and ty versions agree | none |
| F-DEP-4 | defer | none (I-7 hand-off, R7.1) |
| F-CLI-1 | apply now | Phase 11 |
| F-CLI-2 | apply now | Phase 11 |
| F-CLI-3 | defer | none (I-7 hand-off, R7.5) |
| F-CLI-4 | no action (justified): exit mechanism kept, lock moves under F-DUP-3 | none |
| F-TEST-1 | defer | none |
| F-TEST-2 | defer | none |
| F-TEST-3 | apply now | Phase 09 |
| F-DOC-1 | apply now | Phase 20 |
| F-DOC-2 | apply now | Phase 18 |
| F-DOC-3 | defer | none (Phase 20 may decide the nav) |
| F-DOC-4 | apply now | Phase 20 |
| F-DOC-5 | defer | none |
| F-CI-1 | apply now | Phase 18 |
| F-CI-2 | apply now | Phase 18 |
| F-CI-3 | defer | none (I-7 hand-off, R7.2) |
| F-CI-4 | no action (justified): format is enforced through pre-commit | none |
| F-CI-5 | no action (justified): tox is a local convenience and no doctests live under `tests` | none |
| F-CI-6 | apply now | every phase (rule, no own PR) |
| F-PKG-1 | apply now | Phase 19 |
| F-PKG-2 | apply now | Phase 19 |
| F-PKG-3 | no action (justified): verified by the F-PKG-2 smoke test | none |
| F-SEC-1 | defer | none (#136, NG2/NG7, D-3) |
| F-SEC-2 | defer | none (NG7, D-3) |
| F-SEC-3 | no action (justified): documented accepted risk in a dedicated clone | none |

Approval record (D-1):

- This list goes to the user. No apply-now PR merges before the user explicitly approves it.
- Approval status: **pending**. Approved by / date: not yet recorded.
- A finding added after approval needs a new approval, recorded as a D-ID from D-13 onward (R2.4).

### l. I-7 hand-off list (R7)

Facts and leftovers only. Nothing here is resolved, deleted or fixed by I-6.

- R7.1 Remaining `tools/*` references: `tools/` has 281 tracked files (`tools/contracts`, `tools/pr-review`,
  `tools/sisyphus`, `tools/spec-prism-flow`). References remain in `pyproject.toml` (`[tool.ruff]` `extend-exclude`,
  `[tool.ty.src]` `exclude`), `.pre-commit-config.yaml` (`exclude: ^(tools|vendor)/`), `sonar-project.properties`
  (`sonar.exclusions`), `codecov.yml` (`sisyphus` flag path `tools/sisyphus/`) and the CI `deptry` command (F-DEP-4, F-DEP-2).
- R7.2 Old workflows: `.github/workflows/main.yml`, `sisyphus-main.yml`, `spec-prism-flow-main.yml`, `docs.yml` (F-CI-3).
- R7.3 `.orig` files: the only tracked `*.orig` is `tools/pr-review/docs/commands/auto-approve.md.orig`; none under `src/`.
  Not migrated (NG5).
- R7.4 Install and entry-point differences. Old tools: `dotharness-pr-review` with script `harness = "harness.cli:cli"`;
  `sisyphus` with `sisyphus = "sisyphus.cli:main"`; `spec-prism-flow` with `spec-prism-flow = "spec_prism_flow.cli:cli"`
  (hyphenated command). dotharness: one distribution with `[project.scripts]` `vibe_heal`, `pr_review`, `spec_prism_flow`,
  `sisyphus`, all `dotharness.cli:<name>` (underscored commands; `harness` and `spec-prism-flow` are not provided).
  `sisyphus` declares `requires-python = ">=3.11,<4.0"` against `>=3.11` elsewhere. Wheel install is covered by Phase 19.
- R7.5 Deferred findings by ID: F-NAME-3, F-DEP-4, F-CLI-3, F-TEST-1, F-TEST-2, F-DOC-3, F-DOC-5, F-CI-3, and under D-3 and
  NG7 the security findings F-SEC-1 (#136) and F-SEC-2. Open defects #87-#92, #107-#117, #127, #136, #161 stay deferred (D-3).

### m. Fixture decision IDs (D-13 onward)

Golden tests run unmodified (G4, AC5, D-7, D-10): `tests/pr_review/test_cli.py`, `tests/spec_prism_flow/test_cli.py`,
`tests/sisyphus/test_cli.py`, `tests/vibe_heal/test_cli_surface.py`, `tests/contracts/test_gate_record_fixture.py` and
`tests/contracts/gate_record_golden.json`. Fixtures are not golden tests. No new snapshots for other tools (NG9).

Fixture patch targets that may be edited, each edit citing a D-ID: `dotharness.pr_review.state.XDG_DATA`,
`dotharness.pr_review.lock.XDG_RUNTIME`, `dotharness.ai_backend.TMP_DIR`, used in `tests/pr_review/conftest.py`,
`tests/pr_review/test_lock.py`, `tests/pr_review/test_state.py`, `tests/test_ai_backend.py`.

- D-13 (reserved for F-DUP-3, F-DUP-4, F-DUP-5, Phase 09): the data-directory and lock move re-points the three patch targets
  above. Status: no fixture edited yet at this checkpoint. Phase 09 fills in the exact edited fixtures under this ID. A
  fixture edit for any other reason takes D-14 onward.
