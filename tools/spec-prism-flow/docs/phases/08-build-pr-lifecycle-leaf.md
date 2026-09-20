# Phase 08 — PR lifecycle

*Purpose: wrap GitHub PR operations and resume state so a phase's PR lifecycle survives a crash or restart.*

## Scope
- spec_prism_flow/build/gh_ops.py
- spec_prism_flow/build/resume_state.py
- tests/build/test_gh_ops.py
- tests/build/test_resume_state.py

## Requirements

Excerpt, chunk A-3-3-1's mini-requirements doc §7 (Detailed functional requirements), quoted verbatim:

### 7.1 `spec_prism_flow/build/gh_ops.py`
- `pr_create(cwd: Path, branch: str, title: str, body: str) -> PRHandle` — invokes `gh pr create --head {branch} --title {title} --body {body}` as an argv list (`subprocess.run([...], shell=False)`) — `title`/`body` are passed as individual argv elements, never interpolated into a shell string, so shell metacharacters in either (backticks, `$(...)`, quotes, newlines) are inert. Parses the returned PR URL/number into a frozen `PRHandle(number, url)`.
- `pr_view(pr_number: int) -> PRStatus` — wraps `gh pr view {pr_number} --json state,mergeable,reviewDecision`.
- `unresolved_thread_count(pr_number: int) -> int` — paginated GraphQL query (`gh api graphql`) over the PR's `reviewThreads`, counting entries with `isResolved: false`; loops until `pageInfo.hasNextPage` is false. Pagination must not silently truncate at one page — every caller depends on this being the exact count, since it gates whether address-comments (Phase 11) loops again.
- `pr_merge(pr_number: int) -> None` — `gh pr merge {pr_number} --squash` (or config-driven merge strategy if config exposes one; otherwise squash as the fixed default); raises `PRNotMergeableError` (a `CommandError`-family error, `next_command` hint = `gh pr view {pr_number}`) on a non-zero exit rather than retrying internally.

### 7.2 `spec_prism_flow/build/resume_state.py`
- `ResumeState` frozen dataclass: `repo: str`, `branch: str`, `pr_number: int`, `pr_url: str`.
- `resume_state_path(config, repo: str, branch: str) -> Path` — `config.build.state_dir / repo.replace("/", "-") / branch / "resume_state.json"` (`state_dir` is Phase 01's `BuildConfig.state_dir` field, mirroring pr-review's `XDG_DATA`-style convention), keyed by repo slug + branch. Callers pass `branch_name(phase)` (Phase 07's `agent_runner.py`) for `branch`.
- `load_resume_state(path) -> ResumeState | None` / `save_resume_state(path, state) -> None` — plain JSON round-trip; `load` returns `None` (not an error) when no record exists — the normal case for a phase's first run.
- `clear_resume_state(path) -> None` — deletes the resume-state file if present; a no-op (not an error) when no file exists at `path`.
- Callers check `load_resume_state` before calling `pr_create`; call `save_resume_state` immediately after a successful `pr_create`; the record stays in place across the iterate loop so a crash mid-loop still resumes against the same open PR. Once Phase 11 completes a successful `pr_merge`, it calls `clear_resume_state` on that same path — a merged phase must not leave a resume record behind, since a later `--resume` run would otherwise find a stale record for an already-merged PR and wrongly treat the phase as still open.

## Acceptance criteria
- `pr_create` returns a `PRHandle` with the correct number/URL parsed from `gh pr create`'s output.
- `pr_create` invokes `gh` via an argv list (`shell=False`); a title/body containing shell metacharacters (backticks, `$(...)`, quotes, newlines) is passed through as literal text and never executed.
- `unresolved_thread_count` correctly paginates a multi-page GraphQL response (verified with a mocked multi-page fixture) and returns the exact count of `isResolved: false` threads.
- `pr_merge` raises a named `CommandError`-family exception with a `next_command` hint on a non-zero `gh pr merge` exit, and never retries internally.
- `load_resume_state` returns `None` (not an exception) when no resume file exists at the given path; `save_resume_state`/`load_resume_state` round-trip a `ResumeState` correctly.
- `clear_resume_state` removes an existing resume-state file and is a no-op when the file is already absent; after clearing, `load_resume_state` on that path returns `None`.
- `resume_state_path` is keyed by repo slug + branch, so two different phases' resume records never collide.
- `uv run pytest` passes for both test files (subprocess/`gh` calls mocked); `ruff check`/`ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/build/test_gh_ops.py tests/build/test_resume_state.py -v` and confirm all cases above pass.
- Against a mocked multi-page GraphQL fixture (at least 2 pages, some threads resolved and some not), confirm `unresolved_thread_count` returns the correct total across both pages.
- Simulate a `gh pr merge` non-zero exit and confirm the raised error's message and `next_command` hint are both present and correctly formed.
- Manually write and then read back a `ResumeState` via `save_resume_state`/`load_resume_state` against a scratch path and confirm round-trip correctness.
- Confirm no unhandled exceptions appear in any of the above.

## Depends on
- Phase 07 merged.
