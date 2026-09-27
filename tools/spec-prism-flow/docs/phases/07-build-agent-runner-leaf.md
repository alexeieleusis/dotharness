# Phase 07 — Agent-runner abstraction

*Purpose: give the build pipeline one backend-agnostic way to run an agent against a phase and commit/push the agent's result.*

## Scope
- spec_prism_flow/build/agent_runner.py
- spec_prism_flow/build/claude_backend.py
- spec_prism_flow/build/opencode_backend.py
- spec_prism_flow/build/git_ops.py
- tests/build/test_agent_runner.py
- tests/build/test_git_ops.py
- tests/build/test_claude_backend.py
- tests/build/test_opencode_backend.py

## Requirements

Excerpt from chunk A-3-2's mini-requirements doc, §7 (Detailed functional requirements):

### 7.1 `AgentBackend` interface (`spec_prism_flow/build/agent_runner.py`)
```
class AgentBackend(Protocol):
    def invoke(self, instructions: str, cwd: Path) -> str: ...  # returns backend stdout; raises on failure
```

`AgentRunResult` is a frozen dataclass with fields `branch: str`, `commit_sha: str | None`, and `empty: bool`.

`build_backend(cfg: AgentConfig) -> AgentBackend` selects the backend by `cfg.backend`: `ClaudeBackend` for `"claude"`, `OpencodeBackend` for `"opencode"`. `cfg.backend` is a Phase 01 field. It is already validated at config-load time to be `"claude"` or `"opencode"`. This factory does not re-validate it.

`build_prompt(phase: PhaseFile) -> str` renders the `Scope`, `Requirements`, and `Acceptance criteria` sections of the phase file into the instructions text for `invoke`.

`run_phase(phase: PhaseFile, backend: AgentBackend, clone: Path, base_branch: str) -> AgentRunResult` calls, in order:

1. `git_ops.checkout_fresh_branch(clone, branch_name(phase), base_branch)`
2. `backend.invoke(build_prompt(phase), clone)`
3. `git_ops.commit_all(clone, commit_message(phase))`

It then handles the commit result:

- If `commit_all` returns `False`, it returns `AgentRunResult(branch, commit_sha=None, empty=True)` without pushing.
- Otherwise it calls `git_ops.push_branch(clone, branch)` and returns `AgentRunResult(branch, commit_sha=<HEAD>, empty=False)`.

`branch_name(phase) -> str` is `f"phase-{phase.number:02d}-{phase.name}"`.

`commit_message(phase: PhaseFile) -> str`, also in `agent_runner.py`, is `f"phase {phase.number:02d}: {phase.name}"` (e.g., `"phase 07: build-agent-runner-leaf"`). It mirrors `branch_name`'s convention of a zero-padded number plus a slug, since `PhaseFile` has no separate title field for a human-readable message.

### 7.2 Claude Code backend (`spec_prism_flow/build/claude_backend.py`)

`ClaudeBackend` generalizes pr-review's `harness/backend.py`:

- Writes `instructions` to a temp `.md` file under an XDG-style data dir.
- Invokes `["claude", "--dangerously-skip-permissions", "--disable-slash-commands", "-p", f"Read {tmp_path} and follow the instructions exactly."]` via `subprocess.Popen` with a configurable timeout.
- On timeout, sends `SIGKILL` via `killpg` and retries once.
- Applies a repo-identity guard: verify `cwd` matches the expected repo before and after the run.

The guard reuses pr-review's `repo_guard.py` pattern where it is importable as a shared dependency. Otherwise, it uses a narrowed reimplementation scoped to what this phase needs: an existence check plus a name match. It excludes the harness-repo-snapshot machinery, which is pr-review-specific.

A `SIGKILL`ed attempt can leave `cwd` mid-write (partially written/staged files) since the killed process gets no chance to clean up. Before the retried `invoke` call, `ClaudeBackend` must reset `cwd` back to the branch tip left by `checkout_fresh_branch`. It runs `git checkout -- .` and then `git clean -fd` in `cwd`, so the retry reasons about a clean tree instead of the first attempt's wreckage.

### 7.3 opencode backend (`spec_prism_flow/build/opencode_backend.py`)

`OpencodeBackend` generalizes `opencode_runner.py`:

- Writes `instructions` to a temp `.md` file.
- Invokes `["opencode", "run", f"Read {tmp_path} and follow the instructions exactly.", "--standalone", "--dir", str(cwd)]` via `subprocess.run` with a configurable timeout.

`--standalone` is the load-bearing per-run isolation flag. It replaces `--pure` (removed in opencode v2 with no direct one-flag substitute).

Known limitation: `--standalone` isolates this invocation's session from opencode's shared background service. It says nothing about a globally-installed opencode plugin, which can act independently of that isolation. This backend does not carry pr-review's `assert_no_opencode_plugins()` guard against that case. So a future edit should not assume `--standalone` alone rules it out.

**The opencode command must never include the "skip permissions" flag Claude Code uses.** Passing it is a hard failure on the currently-installed opencode version, confirmed live. This is a one-line but load-bearing divergence from the Claude Code backend. A future edit must not "fix" it toward symmetry.

### 7.4 Git primitives (`spec_prism_flow/build/git_ops.py`)

`git_ops.py` ports `checkout_fresh_branch`, `commit_all`, `push_branch`, `fetch_resync`, and `diff_name_only`. Each is generalized only by dropping the Neighboku-specific `review_clone`/two-clone docstring framing. Behavior is otherwise unchanged from the source.

- `commit_all` returns `bool`. `False` means a no-op commit, no raise.
- `push_branch` uses `--force-with-lease` and tolerates a retried `checkout_fresh_branch`.

`diff_stat` and `fast_forward_push` are **not** carried into this phase. Both were specific to the dropped cross-track flow.

## Acceptance criteria

- `build_backend` selects `ClaudeBackend` for `cfg.backend == "claude"` and `OpencodeBackend` for `"opencode"`.
- `build_backend` performs no re-validation of `cfg.backend`'s value.
- `run_phase` (agent-runner level) checks out a fresh branch and invokes the backend.
- When `commit_all` reports no changes, `run_phase` returns `empty=True` without pushing.
- When `commit_all` reports changes, `run_phase` pushes and returns the commit SHA.
- `ClaudeBackend.invoke` writes instructions to a temp file.
- `ClaudeBackend.invoke` invokes `claude` with `--dangerously-skip-permissions --disable-slash-commands -p "Read <path> and follow the instructions exactly."`.
- `ClaudeBackend.invoke` applies a timeout with `SIGKILL`-on-timeout and one retry.
- `ClaudeBackend.invoke` verifies repo identity before and after.
- On `SIGKILL`-on-timeout, `ClaudeBackend.invoke` resets `cwd` back to the branch tip before the retried invocation. It runs `git checkout -- .` and `git clean -fd` in `cwd`, so the retry never reasons about files left mid-write by the killed attempt.
- `OpencodeBackend.invoke` invokes `opencode run ... --standalone --dir <cwd>` and never includes `--dangerously-skip-permissions`. A regression test asserts that flag is absent from every constructed opencode command.
- `git_ops` functions (`checkout_fresh_branch`, `commit_all`, `push_branch`, `fetch_resync`, `diff_name_only`) behave exactly as specified.
- `commit_all` returns `False` (not raising) on an empty diff.
- `push_branch` uses `--force-with-lease`.
- `run_phase` does not catch or suppress any exception from `checkout_fresh_branch`, `invoke`, `commit_all`, or `push_branch`. All propagate uncaught.
- `uv run pytest` passes for all four test files (subprocess calls mocked).
- `ruff check` and `ty` pass with no new violations.

## Manual test checklist

- Run `uv run pytest tests/build/test_agent_runner.py tests/build/test_git_ops.py tests/build/test_claude_backend.py tests/build/test_opencode_backend.py -v`. Confirm all cases above pass.
- Against a scratch git repo, manually invoke `git_ops.checkout_fresh_branch`, `commit_all`, `push_branch`, `fetch_resync`, and `diff_name_only` in sequence. Confirm each behaves as documented, including `commit_all` returning `False` on a clean tree.
- Inspect the constructed command lists for both backends (e.g., via a `--dry-run`-style print). Confirm the opencode command never includes `--dangerously-skip-permissions` while the Claude Code command does.
- Simulate a backend timeout (e.g., a stubbed subprocess that sleeps past the configured timeout). Confirm the process is killed via `killpg`, that one retry occurs, and that the repo-identity guard is re-checked afterward.
- Simulate a killed subprocess that leaves stray file changes in `cwd` (e.g., write an untracked file and modify a tracked one before the mocked timeout fires). Confirm the retry starts from a clean tree matching the branch tip from `checkout_fresh_branch`.
- Confirm no unhandled exceptions appear in any of the above.

## Depends on
- Phase 06 merged.
