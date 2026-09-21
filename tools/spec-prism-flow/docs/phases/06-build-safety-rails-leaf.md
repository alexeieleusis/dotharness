# Phase 06 — Safety rails: scope guard, merge gates, escalation hierarchy

*Purpose: give every build phase the same typed, mechanical guards against scope creep, failing gates, and unbounded retry loops.*

## Scope
- spec_prism_flow/build/scope_guard.py
- spec_prism_flow/build/merge_gates.py
- spec_prism_flow/build/retry_budget.py
- spec_prism_flow/build/errors.py
- tests/build/test_scope_guard.py
- tests/build/test_merge_gates.py
- tests/build/test_retry_budget.py
- tests/build/test_errors.py

## Requirements

Excerpt, `requirements.md` §9 (Non-functional requirements), item 8, quoted verbatim — the policy `RetryBudget` (§7.3 below) is the mechanical enforcement point for:

> **Bounded retries (G4, §8).** `address-comments`-style cycles per phase are capped; on exhaustion, escalate to a human rather than loop indefinitely.

Excerpt, chunk A-3-1's mini-requirements doc §7 (Detailed functional requirements), quoted verbatim:

### 7.1 `spec_prism_flow/build/scope_guard.py`
`matches_any(path: str, globs: list[str]) -> bool` — `fnmatch(path, glob)` over the list, `any()`. `check(changed_paths: list[str], scope_globs: list[str], base_ref: str) -> None` — raises `ScopeViolation(offending, scope_globs, base_ref)` where `offending = [p for p in changed_paths if not matches_any(p, scope_globs)]`; returns `None` if `offending` is empty. `base_ref` is used only to format `ScopeViolation.next_command`'s hint text (see §7.4) — it is not resolved, fetched, or validated by this function. Takes an already-computed `changed_paths` list (the diff-vs-base-ref computation is a git-ops concern, Phase 07) rather than a repo path — keeps this module free of subprocess calls. **Must use `fnmatch`, never `Path.match`** — the latter anchors from the right unless the pattern is absolute, so `Path("evil/src/App.tsx").match("src/*")` is `True`, a real bypass for a check whose entire job is exclusion. No built-in exceptions for any file type — scope is exactly the phase file's declared `Scope` list (Phase 01's `PhaseFile.scope`), nothing implicit.

### 7.2 `spec_prism_flow/build/merge_gates.py`
`run_merge_gates(cwd: Path, commands: list[str], *, tail_lines: int = 40) -> None` — for each command string in order (shell-split, run via `subprocess.run(shlex.split(cmd), cwd=cwd, capture_output=True, text=True)`), raise `MergeGateFailure(cmd, tail)` on first non-zero return code or `OSError`, where `tail` is the last `tail_lines` lines of combined stdout+stderr. An empty `commands` list returns immediately (no-op, not an error) — a brand-new project with no configured commands must not be blocked from merging. Commands come from config (Phase 01's `BuildConfig.commands`), never hardcoded to any one toolchain.

### 7.3 `spec_prism_flow/build/retry_budget.py`
`@dataclass(frozen=True) class RetryBudget: max_cycles: int`. `def check(self, cycles_used: int, *, pr_url: str | None, unresolved_thread_count: int) -> None` — raises `RetryBudgetExhausted(cycles_used, pr_url, unresolved_thread_count)` iff `cycles_used > self.max_cycles`, else returns `None`. Stateless and side-effect-free by design: the caller owns the counter, this only judges it (per the bounded-retries requirement quoted above).

### 7.4 `spec_prism_flow/build/errors.py`
`CommandError(RuntimeError)`: `__init__(self, args: list[str], returncode: int, stderr: str)`, message format `` `{' '.join(args)}` exited {returncode}: {stderr.strip()} ``. `OrchestrationError(Exception)`: `exit_code: ClassVar[int] = 1`; `__init__(message, *, next_command=None)`; mutable `phase_number`/`phase_name` (both `None` initially); `with_context(*, phase_number=None, phase_name=None) -> OrchestrationError` sets and returns self. Subclasses: `EmptyImplementationError` (exit 10, no args), `ScopeViolation` (exit 11, `offending_files`/`allowed_globs`/`base_ref`, `next_command=f"git diff --name-only {base_ref}...HEAD"`), `MergeGateFailure` (exit 12, `gate_name`/`output_tail`, `next_command=f"re-run '{gate_name}' in the clone to reproduce"`), `RetryBudgetExhausted` (exit 13, `cycles`/`pr_url`/`unresolved_thread_count`), `ManualTestFailed` (exit 14, optional `notes`).

## Acceptance criteria
- `scope_guard.matches_any`/`check` use `fnmatch`, not `Path.match` — verified by a regression test asserting `Path("evil/src/App.tsx").match("src/*")`-style bypasses are rejected.
- `scope_guard.check` raises `ScopeViolation` listing exactly the offending paths when any changed path fails every glob, and returns `None` (no exception) when all paths match at least one glob.
- `run_merge_gates` runs each command in order, raises `MergeGateFailure` on the first non-zero exit or `OSError` with the last 40 lines of combined output, and is a no-op (no exception, no subprocess call) for an empty command list.
- `RetryBudget.check` raises `RetryBudgetExhausted` iff `cycles_used > max_cycles`, is pure (no I/O, no mutation), and the caller — not this module — owns the cycle counter.
- `OrchestrationError` and each of its five subclasses carry the correct `exit_code` and (where specified) a populated `next_command`; `with_context` sets `phase_number`/`phase_name` and returns `self` for chaining.
- `CommandError`'s message format matches exactly: `` `{args joined}` exited {returncode}: {stderr stripped} ``.
- `uv run pytest` passes for all four test files; `ruff check`/`ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/build/test_scope_guard.py tests/build/test_merge_gates.py tests/build/test_retry_budget.py tests/build/test_errors.py -v` and confirm all cases above pass, including the `fnmatch`-vs-`Path.match` bypass regression test.
- Construct a fixture directory, run `run_merge_gates` against a passing command list and confirm no exception; against a failing command and confirm `MergeGateFailure` carries the correct tail output.
- Call `RetryBudget(max_cycles=2).check(cycles_used=3, ...)` and confirm `RetryBudgetExhausted` is raised with the correct cycle/PR/thread-count fields.
- Raise each of the five `OrchestrationError` subclasses in a scratch script and confirm `with_context(phase_number=5, phase_name="foo")` populates both fields and `exit_code` matches the documented value.
- Confirm no unhandled exceptions or stack traces appear in any of the above — every raised error is one of the named, typed exceptions.

## Depends on
- Phase 05 merged.
