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
| Prompt file vs argv | File by default (`prompt_via_file=True`) under `~/.local/share/dotharness/tmp`, removed in `finally` (also on cancellation). Inline argv via `prompt_via_file=False`. | vibe-heal's file in cwd is not adopted: it litters the working tree. sisyphus's argv prompt is kept as an option for short prompts. |
| 127 / 124 vs exceptions | `RunResult` with `not_found` (127) and `timed_out` (124) flags; the launcher does not raise for these. Guard failures raise typed errors. | pr-review and spec-prism-flow raise `TimeoutExpired`/`CalledProcessError`: callers that want that can check the flags. Return codes alone are ambiguous, hence the flags. |
| 1800 s default (spec-prism-flow) | No default: `timeout` is required. | A silent default hides a decision. Per-caller values: pr-review config, spec-prism-flow 1800, vibe-heal Claude 1200 (D-6, implemented in the vibe-heal leaf). |
| opencode without a permission flag (spec-prism-flow) | Adopted: `opencode` never receives `--dangerously-skip-permissions` (opencode 2.0 dropped it); `auto` is the only approval flag. | n/a |
| Global opencode lock (pr-review `_OPENCODE_GLOBAL_LOCK_KEY`) | Dropped. | opencode 2.0 no longer needs it. |

pr-review `Backend` behaviours kept, each optional by parameter: retries (`max_retries`),
cwd-divergence monitor (`monitor_cwd`, uses `lsof`), `opencode plugin list` check
(`check_plugins`), repo-identity guard (`expected_repo_name`), harness-repo guard
(`guard_harness_repo`), `PWD` pinned to `cwd`, and the wait for the process group to exit
(with a warning when members survive, and a by-name check for escaped daemonised children
after a timeout-kill).

Changes from pr-review worth knowing: retries also cover a non-zero exit (pr-review retried
timeouts only); the plugin check runs once per `launch` call (pr-review: once per `Backend`
instance and cwd); the survivor check logs pids only, never command lines.

## Treatment of the other copies

- spec-prism-flow `claude_backend.py` / `opencode_backend.py` and sisyphus
  `cli.py` / `subproc.py`: not migrated, edited or deprecated in #164. They stay as they
  are until I-3, I-4 and I-5 move each tool onto this layer.
- pr-review `backend.py` and `runners/common.py`: not changed; I-3 replaces them.
- vibe-heal `ai_tools/`: migrated in the next leaf of #164.

## Contract for I-3, I-4 and I-5

```python
from dotharness.ai_backend import launch, launch_sync
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
    expected_repo_name: str | None = None,      # "owner/repo"
    guard_harness_repo: bool = False,
    harness_repo_root: str | Path | None = None,
    # claude only
    print_mode: bool = True, print_flag: str = "-p",
    skip_permissions: bool = False, permission_mode: str | None = None,
    disable_slash_commands: bool = False, output_format: str | None = None,
    allowed_tools: str | None = None,
    # opencode only
    standalone: bool = False, auto: bool = False,
    opencode_dir: str | Path | None = None, model: str | None = None,
    monitor_cwd: bool = False, check_plugins: bool = False,
) -> RunResult
```

`launch_sync` has the same signature and calls `launch` through `asyncio.run`; it must not
be called from a running event loop. It is documented for the sync tools (I-3, I-4, I-5)
and is not wired into any of them yet.

- Result: `RunResult(returncode, stdout, stderr, timed_out=False, not_found=False)`, the
  last attempt's. It never carries argv or env.
- Plain non-zero exit, timeout (`timed_out`, 124) and missing executable (`not_found`, 127)
  are returned, not raised. Retries (`max_retries`) re-run after a timeout or non-zero exit,
  not after `not_found`; the last result is returned.
- Raised: `ValueError` (unknown backend, option of the other backend), `OpencodePluginError`,
  `BackendCwdDivergedError` (not retried), `RepoIdentityError`.
- A caller must pass `backend`, `prompt`, `cwd` and `timeout`, and choose every permission
  and isolation flag itself. Reference mappings: pr-review claude =
  `skip_permissions=True, disable_slash_commands=True`; pr-review opencode =
  `standalone=True, auto=True, monitor_cwd=True, check_plugins=True`; vibe-heal claude =
  `print_flag="--print", output_format="json", permission_mode="acceptEdits",
  allowed_tools="Edit,Read"`.
- Cancelling the task kills the process group and removes the prompt file.
- Neither argv nor env is logged or placed in exception text. Callers must keep to this.
- Not provided: lock files, `gh`, config loading, `build_subprocess_env`.
- Timeout for Claude in vibe-heal: D-6 sets 1200 s; implemented in the next leaf.
