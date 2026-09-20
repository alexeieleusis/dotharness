# Phase 07 — Agent-runner abstraction

*Purpose: give the build pipeline one backend-agnostic way to run an agent against a phase and commit/push its result.*

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

Excerpt, chunk A-3-2's mini-requirements doc §7 (Detailed functional requirements), quoted verbatim:

### 7.1 `AgentBackend` interface (`spec_prism_flow/build/agent_runner.py`)
```
class AgentBackend(Protocol):
    def invoke(self, instructions: str, cwd: Path) -> str: ...  # returns backend stdout; raises on failure
```
`AgentRunResult` frozen dataclass: `branch: str`, `commit_sha: str | None`, `empty: bool`. `build_backend(cfg: AgentConfig) -> AgentBackend` selects `ClaudeBackend`/`OpencodeBackend` by `cfg.backend` (Phase 01's field; already validated to `"claude"`/`"opencode"` at config-load time — this factory does not re-validate). `build_prompt(phase: PhaseFile) -> str` renders the phase file's `Scope`/`Requirements`/`Acceptance criteria` sections into the instructions text handed to `invoke`. `run_phase(phase: PhaseFile, backend: AgentBackend, clone: Path, base_branch: str) -> AgentRunResult`: calls `git_ops.checkout_fresh_branch(clone, branch_name(phase), base_branch)`, `backend.invoke(build_prompt(phase), clone)`, `git_ops.commit_all(clone, commit_message(phase))`; if `commit_all` returns `False`, returns `AgentRunResult(branch, commit_sha=None, empty=True)` without pushing; otherwise `git_ops.push_branch(clone, branch)` and returns `AgentRunResult(branch, commit_sha=<HEAD>, empty=False)`. `branch_name(phase) -> str` is `f"phase-{phase.number:02d}-{phase.name}"`. `commit_message(phase: PhaseFile) -> str`, also in `agent_runner.py`, is `f"phase {phase.number:02d}: {phase.name}"` (e.g. `"phase 07: build-agent-runner-leaf"`) — mirroring `branch_name`'s zero-padded-number-plus-slug convention, since `PhaseFile` has no separate title field to draw a human-readable message from.

### 7.2 Claude Code backend (`spec_prism_flow/build/claude_backend.py`)
Generalizes pr-review's `harness/backend.py`: writes `instructions` to a temp `.md` file under an XDG-style data dir, invokes `["claude", "--dangerously-skip-permissions", "--disable-slash-commands", "-p", f"Read {tmp_path} and follow the instructions exactly."]` via `subprocess.Popen` with a configurable timeout, `SIGKILL`-on-timeout via `killpg`, one bounded retry on timeout, and a repo-identity guard (verify `cwd` matches the expected repo before and after the run) — reusing pr-review's `repo_guard.py` pattern where importable as a shared dependency; otherwise a narrowed reimplementation scoped to what this phase needs (existence check + name match, not the harness-repo-snapshot machinery, which is pr-review-specific). A `SIGKILL`ed attempt can leave `cwd` mid-write (partially written/staged files) since the killed process gets no chance to clean up; before the retried `invoke` call, `ClaudeBackend` must reset `cwd` back to the branch tip left by `checkout_fresh_branch` (`git checkout -- .` followed by `git clean -fd` run in `cwd`) so the retry reasons about a clean tree instead of the first attempt's wreckage.

### 7.3 opencode backend (`spec_prism_flow/build/opencode_backend.py`)
Generalizes `opencode_runner.py`: writes `instructions` to a temp `.md` file, invokes `["opencode", "run", f"Read {tmp_path} and follow the instructions exactly.", "--pure", "--dir", str(cwd)]` via `subprocess.run` with a configurable timeout. **Must never include the "skip permissions" flag Claude Code uses** — passing it is a hard failure on the currently-installed opencode version, confirmed live; this is a one-line but load-bearing divergence from the Claude Code backend and must not be "fixed" toward symmetry by a future edit.

### 7.4 Git primitives (`spec_prism_flow/build/git_ops.py`)
Ports `checkout_fresh_branch`, `commit_all` (returns `bool`, `False` = no-op commit, no raise), `push_branch` (`--force-with-lease`, tolerating a retried `checkout_fresh_branch`), `fetch_resync`, `diff_name_only` — each generalized only by dropping the Neighboku-specific `review_clone`/two-clone docstring framing; behavior is otherwise unchanged from the source. `diff_stat`/`fast_forward_push` are **not** carried into this phase — both were specific to the dropped cross-track flow.

## Acceptance criteria
- `build_backend` selects `ClaudeBackend` for `cfg.backend == "claude"` and `OpencodeBackend` for `"opencode"`, with no re-validation of `cfg.backend`'s value.
- `run_phase` (agent-runner level) checks out a fresh branch, invokes the backend, and either returns `empty=True` with no push when `commit_all` reports no changes, or pushes and returns the commit SHA when it does.
- `ClaudeBackend.invoke` writes instructions to a temp file, invokes `claude` with `--dangerously-skip-permissions --disable-slash-commands -p "Read <path> and follow the instructions exactly."`, applies a timeout with `SIGKILL`-on-timeout and one retry, and verifies repo identity before and after.
- On `SIGKILL`-on-timeout, `ClaudeBackend.invoke` resets `cwd` (`git checkout -- .` + `git clean -fd`) back to the branch tip before the retried invocation, so the retry never reasons about files left mid-write by the killed attempt.
- `OpencodeBackend.invoke` invokes `opencode run ... --pure --dir <cwd>` and never includes `--dangerously-skip-permissions` — verified by a regression test asserting that flag is absent from every constructed opencode command.
- `git_ops` functions (`checkout_fresh_branch`, `commit_all`, `push_branch`, `fetch_resync`, `diff_name_only`) behave exactly as specified, with `commit_all` returning `False` (not raising) on an empty diff and `push_branch` using `--force-with-lease`.
- No exception from `checkout_fresh_branch`/`invoke`/`commit_all`/`push_branch` is caught and suppressed inside `run_phase` — all propagate uncaught.
- `uv run pytest` passes for all four test files (subprocess calls mocked); `ruff check`/`ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/build/test_agent_runner.py tests/build/test_git_ops.py tests/build/test_claude_backend.py tests/build/test_opencode_backend.py -v` and confirm all cases above pass.
- Against a scratch git repo, manually invoke `git_ops.checkout_fresh_branch`/`commit_all`/`push_branch`/`fetch_resync`/`diff_name_only` in sequence and confirm each behaves as documented, including `commit_all` returning `False` on a clean tree.
- Inspect the constructed command lists for both backends (e.g. via a `--dry-run`-style print) and manually confirm the opencode command never includes `--dangerously-skip-permissions` while the Claude Code command does.
- Simulate a backend timeout (e.g. a stubbed subprocess that sleeps past the configured timeout) and confirm the process is killed via `killpg`, one retry occurs, and the repo-identity guard is re-checked afterward.
- Simulate a killed subprocess that leaves stray file changes in `cwd` (e.g. write an untracked file and modify a tracked one before the mocked timeout fires) and confirm the retry starts from a clean tree matching the branch tip from `checkout_fresh_branch`.
- Confirm no unhandled exceptions appear in any of the above.

## Depends on
- Phase 06 merged.
