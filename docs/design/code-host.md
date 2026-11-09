# Code host: capability interfaces and direct gh user inventory

Status: Phase 0 design for #192, not implemented. Planned location of the code: `src/dotharness/code_host/`.

This page is built section by section. This file currently holds the **Inventory** section (A-1.1): every direct gh user in
`src/`, one stable row id each, one owner capability, one removing phase. The page header, glossary, phase split and rename
plan are owned by sibling sections and cite the row ids below.

## Inventory

### Terms used here

- **Direct gh user**: a function, class or script in `src/` or either `pr-comments.py` copy that builds a gh argv, runs gh,
  reads `GITHUB_TOKEN`/`GH_TOKEN`, or runs `gh_token_cmd`/`token_cmd`.
- **Owner**: the one capability interface an inventoried operation belongs to. Starting set: Session (Ses), Repository
  (Repo), ChangeRequests (CR), Discussion (Disc), Checks (Chk), WorkItems (WI).
- **Change request (CR)**: a pull request. Existing code keeps "PR"; new names use "change request".
- Operation names are neutral ("list change requests", not "pr list"). The "today's gh call" column may quote gh.

### Audit method

Audited at commit `bb55976` (the branch base; the code inventoried is unchanged since `f293b99`, the commit the starting list
was drafted against). Run from the repo root. Every hit must map to an inventory row, to "Not inventoried" below, or to the
"Non-call hits" list.

```
grep -rnE "gh_argv\(|['\"]gh['\"]|\b_gh\(|run_gh\(|\bgh [a-z]" src
grep -rnE "GITHUB_TOKEN|GH_TOKEN|gh_token_cmd|token_cmd" src
grep -rnE "shutil\.which\(.gh|get-url origin|PR_COMMENTS_SCRIPT_PATH|build_subprocess_env" src
grep -nE "\"gh\"|gh " docs/pr_review/scripts/pr-comments.py
```

Rules:

- Hits inside comments, docstrings, log or error text, or `src/dotharness/knowledge/` prose are not call sites. They are
  listed under "Non-call hits" by file so a reviewer can see they were examined.
- Row ids have the form `INV-<group>-<nn>` and are never renumbered. A deleted row keeps its id with the word "withdrawn".
- The first grep also hits the `ci_checks.gh` function and its `_gh` alias (`ci_fix.py`, `merge.py`), and the `run_gh`
  parameter of `auto_approve_github.py` `list_own_open_prs`; that is why those patterns are listed.
- Paths below are under `src/dotharness/` unless stated. Abbreviations: `runners/` is `pr_review/runners/`.

### Table shape and phases

Columns, in order: row id, group, operation, today's gh call, file and function, owner capability, removing phase. The group
and phase are constant per table, so they appear once per table heading instead of per row. Each row has exactly one owner and
exactly one phase. A row touching two capabilities names one owner and is justified under "Owner justifications".

| Group | Tables | Removing phase |
| --- | --- | --- |
| `sisyphus` | `INV-SY-*` | Phase 1 |
| `vibe-heal` | `INV-VH-*` | Phase 2 |
| `pr-review` | `INV-PR-*` | Phase 3 |
| `leftovers` (shared helper `gh.py`, both `pr-comments.py` copies) | `INV-LO-*` | Phase 4 |

Check: the number of rows whose phase is not one of 1..4, or whose owner is not exactly one of Ses, Repo, CR, Disc, Chk, WI,
must be zero. A row moves between phases only by editing its table.

### Group `sisyphus` (Phase 1), `sisyphus/`

| Row id | Operation | Today's gh call | File and function | Owner |
| --- | --- | --- | --- | --- |
| INV-SY-01 | gh runner with `GH_TOKEN`, no `--repo`, cwd = dedicated clone | `gh_argv(*args)` with env `{"GH_TOKEN": token}` | `pick.py` `GhCli` (built in `cli.py` `default_services`) | Ses |
| INV-SY-02 | remove label from work item | `issue edit N --remove-label L` (`LabelRemovalError`) | `pick.py` `GhLabelRemover.remove_label`; `cli.py` `GhTimeboundLabelRemover` (10 s subclass) | WI |
| INV-SY-03 | list candidate work items | `issue list --state open --assignee A --label L --limit 100 --json ...` | `pick.py` `GitHubIssueTracker.list_candidates` | WI |
| INV-SY-04 | find CRs that close an issue (reference lookup) | `issue view N --json closedByPullRequestsReferences`, then per reference `pr view R --json number,state` | `pick.py` `GhPullRequestLookup.linked_prs` | WI (see Owner justifications 5, R-INV-2) |
| INV-SY-05 | find CRs that mention an issue (body scan) | repo-wide `pr list --state open --limit 200 --json body` | `pick.py` `GhPullRequestLookup.open_pr_bodies` (`limit=200`) | WI (see Owner justifications 5, R-INV-2) |
| INV-SY-06 | find open CR for a branch | `pr list --head B --state open --json number,url` | `cli.py` `GhPullRequestClient.find_pr` | CR |
| INV-SY-07 | read CR state (number, state, headRefOid) | `pr view <number or branch> --json number,state,headRefOid` | `cli.py` `GhPullRequestClient.pr_state`; consumed by `cleanup.py` `PrInfo` | CR |
| INV-SY-08 | open a draft CR | `pr create --draft --head B --base BASE --title T --body B` (`GitHubError`) | `cli.py` `GhPullRequestClient.open_pr` | CR |
| INV-SY-09 | CR state by number | `pr view N --json state`; `PR_STATE_TIMEOUT = 10.0`; `PickError` on empty | `cli.py` `GhPrStateLookup` | CR |
| INV-SY-10 | read default branch | `repo view --json defaultBranchRef` | `cli.py` `default_services` (nested default-branch reader) | Repo |
| INV-SY-11 | resolve token via command | `token_cmd`, `shlex.split`, no shell, `TOKEN_CMD_TIMEOUT = 30`, `ConfigError`, no default | `cli.py` `run_token_cmd` (called in `default_services`) | Ses |
| INV-SY-12 | config field `[source.host].token_cmd` | parsed only, never executed | `sisyphus/config.py` `HostSource.token_cmd` | Ses (declares the INV-SY-11 input) |

Context, not rows: `sisyphus/orchestrator.py` `PullRequestClient` protocol is an existing seam, not itself a gh caller; it
defines `GitHubError`, the error surface of INV-SY-06..08. `cleanup.py` consumes `PrInfo` (number, state, headRefOid).
`prbody.py` `MAX_BODY_CHARS = 60000` is a host limit the contract must expose, not a gh call.

### Group `vibe-heal` (Phase 2), `vibe_heal/review/github.py` class `GitHubReviewClient`

Consumers: `vibe_heal/review/orchestrator.py`, `vibe_heal/cli.py`.

| Row id | Operation | Today's gh call | File and function | Owner |
| --- | --- | --- | --- | --- |
| INV-VH-01 | check host tool installed | `gh --version` (10 s) | `validate_installed` | Ses |
| INV-VH-02 | read field of current-branch CR | `pr view --json FIELD` (30 s); `NoOpenPrError` | `_view_pr_field` | CR |
| INV-VH-03 | post review with inline comments | `api --method POST /repos/OWNER_REPO/pulls/N/reviews --input -`; `_MAX_INLINE_COMMENTS = 50`, `_MAX_REVIEW_BODY_CHARS = 60_000`; 60 s | `post_review` | Disc |
| INV-VH-04 | fallback top-level comment when review post fails | second `gh_argv` call (line ~204) | `post_review` fallback | Disc |
| INV-VH-05 | resolve owner/repo | `repo view --json owner,name --jq ...` | `_get_owner_repo` | Repo |
| INV-VH-06 | resolve owner/repo fallback | `git remote get-url origin` through `_run_gh`, parsed by `_parse_remote_url` | `_get_owner_repo` | Repo (the git primitive stays in git code; the identity it yields becomes a RepositoryRef) |
| INV-VH-07 | `_run_gh` runner adapter and error types | `GitHubReviewError`, `NoOpenPrError`, `GitHubTimeoutError` | `_run_gh` | Ses (transport; folded into the interface error taxonomy) |

### Group `pr-review` (Phase 3), `pr_review/runners/` unless stated

| Row id | Operation | Today's gh call | File and function | Owner |
| --- | --- | --- | --- | --- |
| INV-PR-01 | resolve token via command | `gh_token_cmd` (default `gh auth token`), `shell=True`, `TIMEOUT_GH = 30`; `RuntimeError` on timeout or empty | `common.py` `get_gh_token` | Ses |
| INV-PR-02 | read CR state | `pr view N --repo R --json state --jq .state` | `common.py` `is_pr_open` | CR |
| INV-PR-03 | resolve acting user | `api user --jq .login` (fails open, returns "") | `common.py` `get_current_user` | Ses |
| INV-PR-04 | read CR by number, caller-chosen fields | `pr view N --repo R --json <fields>` | `common.py` `pr_from_url` | CR |
| INV-PR-05 | read requested reviewers | `pr view --json reviewRequests` | `common.py` `get_requested_reviewers` | CR |
| INV-PR-06 | request a reviewer | `pr edit N --add-reviewer L` | `common.py` `add_reviewer` | CR |
| INV-PR-07 | page a list endpoint (transport) | `api --method GET PATH -F per_page=100 -F page=P` | `common.py` `_fetch_page` (used by `fetch_all_pages`) | CR (shared transport; each caller's row owns its semantics) |
| INV-PR-08 | read work item (title, body) | `issue view N --repo R --json number,title,body` (`_IssueFetchError`) | `common.py` `_fetch_issue` | WI |
| INV-PR-09 | resolve CR's linked work items | per-reference `issue view` over `closingIssuesReferences` | `common.py` `_resolve_native_linked_tickets` (calls `_fetch_issue`) | WI |
| INV-PR-10 | post CR-level comment ("no linked ticket") | `pr comment N --repo R --body B` | `common.py` `post_no_linked_ticket_comment` | Disc |
| INV-PR-11 | list open CRs (author filter applied after) | `pr list --repo R --state open --json ... --limit 500` | `common.py` `list_open_prs_matching_authors` | CR |
| INV-PR-12 | list CRs by flag (author, assignee, reviewer) `@me` | `pr list --repo R <flag> @me --state open --json ...` | `common.py` `_list_prs_by_flag` | CR |
| INV-PR-13 | list review-requested CRs | `search prs user-review-requested:@me`, then per stub `pr view --json number,headRefName,isDraft` | `common.py` `_list_review_requested_prs` | CR |
| INV-PR-14 | read CR base branch | `pr view --json baseRefName --jq` | `common.py` `get_pr_base_branch` | CR |
| INV-PR-15 | read CR head revision | `pr view --json headRefOid --jq` | `common.py` `get_pr_head_sha` | CR |
| INV-PR-16 | read CR description | `pr view --json title,body --jq` | `common.py` `get_pr_description` | CR |
| INV-PR-17 | read review-thread state | `api graphql` (`GRAPHQL_QUERY_REVIEW_THREAD_STATE`, cursor paging) | `common.py` `_fetch_review_threads_page` | Disc |
| INV-PR-18 | fetch CR comments via child process | runs `scripts/pr-comments.py fetch` (`PR_COMMENTS_SCRIPT_PATH`); the script's gh calls are INV-LO-02..03 | `common.py` `fetch_pr_comments`; callers `focused_review.py`, `address_comments.py`, `regret_review.py` | Disc |
| INV-PR-19 | run a gh command with timeout mapping (transport) | `gh_argv(*args)` via `run_cmd`; timeout becomes exit 124 | `ci_checks.py` `gh` (aliased `_gh` in `ci_fix.py`, `merge.py`; imported as `gh` in `auto_approve_github.py`) | Chk (shared transport; rows below own semantics) |
| INV-PR-20 | read check buckets of a CR | `pr checks N --repo R --json ... [--required]` | `ci_checks.py` `_read_buckets` | Chk |
| INV-PR-21 | list open CRs by head branch | `pr list --head B` | `ci_checks.py` `_open_prs_with_head` | CR |
| INV-PR-22 | read required check names of stack root | `pr checks ROOT --repo R --required --json name` | `ci_checks.py` `_read_root_required_names` | Chk |
| INV-PR-23 | read default branch | `repo view REPO --json defaultBranchRef` | `ci_checks.py` `_read_default_branch` | Repo |
| INV-PR-24 | list workflows | `api repos/R/actions/workflows?per_page=100` | `ci_checks.py` `repo_has_workflows` | Chk |
| INV-PR-25 | read failing-run log | `run view --job ID` or `run view RUN --repo R --log-failed` | `ci_fix.py` `_read_failed_log` (`CiFixError`) | Chk |
| INV-PR-26 | read failing job (step name) | `api repos/R/actions/jobs/ID` | `ci_fix.py` `_failed_step_from_api` | Chk |
| INV-PR-27 | list own open CRs (`run_gh` seam) | `pr list` / `pr view` via the `run_gh` parameter; plus `pr list --search user-review-requested:@me --limit 500` | `auto_approve_github.py` `list_own_open_prs`; `auto_approve.py` `_list_prs` (`AutoApproveError`) | CR |
| INV-PR-28 | read CR commits (commit clock) | `pr view N --repo R --json commits` via `gh_argv` + `run_cmd` | `auto_approve_github.py` `_fetch_pr_commits` | CR |
| INV-PR-29 | read per-file viewed state | `api graphql` (`_FILES_QUERY`, paged) | `auto_approve_github.py` `read_viewed_states` / `_read_files_page` | CR (see Owner justifications 2) |
| INV-PR-30 | mark file viewed (write) | `api graphql` `markFileAsViewed` mutation | `auto_approve_github.py` `_mark_one` | CR (see Owner justifications 2) |
| INV-PR-31 | approve a CR at a head revision | `api --method POST repos/R/pulls/N/reviews -f event=APPROVE -f commit_id=SHA` | `auto_approve_github.py` `_post_approval` | CR (see Owner justifications 1) |
| INV-PR-32 | read CR merge state; read acting user's approval state | `pr view --json mergeable,autoMergeRequest,isDraft,state,headRefOid,baseRefName`; approvals via `fetch_all_pages` reviews (INV-PR-07) | `auto_approve_github.py` `_read_pr_state`; `_read_approval_state`; `_check_approval_safety` | CR (see Owner justifications 3) |
| INV-PR-33 | read changed-line counts | `pr view --json additions,deletions` via `ci_checks.gh` | `merge.py` `_read_changed_lines` | CR |
| INV-PR-34 | merge a CR (squash, head-pinned) | `pr merge N --repo R --squash --match-head-commit SHA` (`MergeError`) | `merge.py` `_process_pr` | CR |
| INV-PR-35 | commit to CRs lookup | `api repos/R/commits/SHA/pulls` | `regret_review.py` `_resolve_commit_prs` | CR (see Owner justifications 4) |
| INV-PR-36 | post regret comment | `pr comment N --repo R --body B` | `regret_review.py` `post_regret_comment` | Disc |
| INV-PR-37 | list all open CR numbers | `pr list --state open --json number --limit 1000` | `address_comments.py` `_list_all_open_pr_numbers` | CR |
| INV-PR-38 | list CRs by `@me` filter | `pr list <flag> @me --state open --json` | `address_comments.py` `_list_prs` | CR |
| INV-PR-39 | count unresolved review threads | `api graphql` (`GRAPHQL_QUERY`, cursor) | `address_comments.py` `_check_gql_unresolved_threads` | Disc |
| INV-PR-40 | read human issue comments | `api repos/R/issues/N/comments --paginate` | `address_comments.py` `_check_human_issue_comments` | Disc |
| INV-PR-41 | list review-requested CRs (author loop) | `pr list --search user-review-requested:@me --json ... --limit` | `review_requested.py` `_get_prs` | CR |
| INV-PR-42 | list reviews, has user approved | `api --method GET repos/R/pulls/N/reviews -F per_page=100 -F page=P` loop | `review_requested.py` `_has_user_approved` | CR (see Owner justifications 3) |
| INV-PR-43 | list own CRs by flag | `pr list <flag> @me --json` (returns None on failure) | `self_review.py` `_list_prs_by_flag` | CR |
| INV-PR-44 | build subprocess env with `GITHUB_TOKEN` | `build_subprocess_env(..., gh_token)` | `focused_review.py`, `review_prs.py`, `auto_approve.py`, `merge.py`, `ci_fix.py`, `self_review.py`, `review_requested.py`, `address_comments.py` (all `_run_locked`); `local_review/review.py`, `local_review/address.py`, `local_review/git.py` (no token passed) | Ses |
| INV-PR-45 | hand-built `{**env, "GITHUB_TOKEN": ...}` dict | env literal | `focused_review.py`, `review_requested.py`, `self_review.py` `_build_backend` (reads `env.get("GITHUB_TOKEN")`, `RuntimeError` if empty), `ci_fix.py` `_agent_fix_and_push`, `address_comments.py` | Ses |
| INV-PR-46 | check gh installed | `shutil.which("gh")` | `pr_review/cli.py` `cmd_validate` | Ses |
| INV-PR-47 | config default / template | `gh_token_cmd = "gh auth token"` | `pr_review/cli.py` `TEMPLATE`; `pr_review/config.py` `gh_token_cmd` field and TOML default | Ses |

Error types (error surface of the rows): `_IssueFetchError` (INV-PR-08, `common.py`), `MergeError` (INV-PR-34),
`AutoApproveError` (INV-PR-27, raised when the `pr list` fails or a PR fails), `CiFixError` (INV-PR-25, INV-PR-26). Each class
exists at the audited commit. Consumers of INV-PR-07 that are not direct gh callers: `_check_timing_gate` and
`_check_approval_safety` in `auto_approve_github.py` (via `fetch_all_pages`).

### Group `leftovers` (Phase 4)

| Row id | Operation | Today's gh call | File and function | Owner |
| --- | --- | --- | --- | --- |
| INV-LO-01 | shared argv/env helpers | `GH = "gh"`, `gh_argv`, `build_subprocess_env` (sets `GITHUB_TOKEN`) | `gh.py` | Ses |
| INV-LO-02 | script: resolve CR for current branch | `run(["gh","pr","view","--json","number"])` | `scripts/pr-comments.py` `get_pr_number` | CR |
| INV-LO-03 | script: read comments, paginated | `run(["gh", *args])` in `gh_json` / `gh_json_paginated` | `scripts/pr-comments.py` | Disc |
| INV-LO-04 | script: post comment / reply | `gh api PATH -X POST -F body=@-` | `scripts/pr-comments.py` `_post_reply` (line ~243) | Disc |
| INV-LO-05 | script: add reviewer | `gh pr edit N --add-reviewer a,b` | `scripts/pr-comments.py` `_request_re_review` (line ~305) | CR |
| INV-LO-06 | docs copy: same operations, `gh_paginated(endpoint)` | as INV-LO-02..05 | `docs/pr_review/scripts/pr-comments.py` (outside `src/dotharness/`; first-line note: "Historical: describes the pre-migration tools/pr-review (harness) tool. Not maintained.") | Disc (dominant operation; the copy is not migrated) |

Both script copies live under `src/dotharness/scripts/` (413 lines) and `docs/pr_review/scripts/` (379 lines). The `src`
script is stdlib only and must not import dotharness (F-DUP-8 in `docs/design/shared-async-layer.md`). The two copies differ
(359 lines of `diff` output at the audited commit). This page RECORDS the divergence and does not reconcile it. The docs copy
is inventoried only, not migrated; deleting it needs an issue that is not in the repo: to be filed.

Phase column rule: Phase 1 = every `INV-SY-*` row; Phase 2 = every `INV-VH-*` row; Phase 3 = every `INV-PR-*` row;
Phase 4 = every `INV-LO-*` row except INV-LO-06, whose removing phase reads "none: not migrated" (see Phase split). INV-PR-46 and INV-PR-47 are Phase 3 here; if the Phase split assigns them to Phase 4,
edit the table, not the rule.

### Owner justifications

Stated once; cited by row.

1. **Approve a CR (INV-PR-31): ChangeRequests.** An approval is a verdict on the CR bound to a head revision, not a
   conversation entry; it gates merge, which is also ChangeRequests (INV-PR-34). Discussion owns free text and thread content only.
2. **Mark file viewed, write and read state (INV-PR-30, INV-PR-29): ChangeRequests.** Viewed state is per-user review
   progress on the CR's changed files and is paired with approval in one gate (`auto_approve_github.py`). Both halves go to
   one owner so write and read cannot split. Discussion is rejected because viewed state has no comment body or thread.
3. **List reviews / has-user-approved (INV-PR-42, INV-PR-32): ChangeRequests,** the same owner as approve, so the check and
   the act share a capability.
4. **Commit-to-CRs lookup (INV-PR-35): ChangeRequests.** Input is a commit, output is CRs; no Repository state is changed or
   read beyond identity.
5. **CR-closing-issue lookup (INV-SY-04, INV-SY-05): WorkItems.** The question is asked from the issue's side ("which CRs
   close this issue"); the answer returns CR identifiers only, and WorkItems already owns issue state.
6. **Shared transport rows (INV-PR-07, INV-PR-19, INV-VH-07):** the owner names the capability whose operations dominate use;
   the transport itself disappears into the host implementation, so this is a classification for the removing-phase column only.
7. **Straddler notes:** INV-PR-09 (CR to issue hop) goes to WorkItems because the payload is the issue. INV-VH-06 goes to
   Repository because its output is repository identity. INV-PR-44 and INV-PR-45 go to Session because they carry the
   credential, not a repository operation. INV-PR-32 names ChangeRequests for both halves for the reason in item 3.

### R-INV-2: what "find the CRs that close an issue" does today

The actual behavior in `sisyphus/pick.py` is two different questions, not one:

- (a) a reference lookup, `issue view N --json closedByPullRequestsReferences` (INV-SY-04), and (b) one `pr view` per
  returned reference, also INV-SY-04. Together they answer "CRs the host records as closing this issue".
- (c) a separate repo-wide `pr list --json body` scan (`open_pr_bodies`, INV-SY-05) that matches issue references in open CR
  bodies. It answers "open CRs whose text mentions this issue", which the host does not record. It is not a host-side relation.

The operations are named after these semantics, for example `closing_change_requests(work_item)` and
`open_change_request_bodies()`. The design does not collapse them into one operation. Interface sections must cite INV-SY-04
and INV-SY-05.

### Not inventoried

- Git code that stays OUT of the interface: `repo_guard.py` (origin URL check via `git remote get-url origin`);
  `git fetch origin refs/pull/{n}/head` in `pr_review/runners/auto_approve.py`.
- Not code-host calls: crontab/launchctl in `pr_review/scheduler.py`; the clipboard call in `spec_prism_flow/handoff.py`;
  `sisyphus/intake.py` `_git`; claude/opencode launches (`ai_backend.py`).
- `sisyphus/config.py` states it "never executes token_cmd and never reads the environment"; it is kept only as INV-SY-12.
- Linear (`LINEAR_API_URL` in `sisyphus/pick.py`, `LinearTracker`, `LINEAR_API_KEY` read in `sisyphus/cli.py`
  `default_services`) is another tracker, not gh, and is out of this inventory. Confirm the WorkItems section agrees.
- The other `shutil.which(...)` hits (`backend_bin` and `pr_review` in `pr_review/cli.py`; `vibe_heal/ai_tools/*`,
  `vibe_heal/config/scanner_discovery.py`, `vibe_heal/git/manager.py`, `vibe_heal/sonarqube/analysis_runner.py`) look up
  other tools, not gh.

### Non-call hits

Examined and not call sites (docstrings, comments, log or error text, prose). By file under `src/dotharness/`:

- `pr_review/runners/`: `self_review.py`, `auto_approve.py` (error text, docstring), `ci_fix.py` (`UNKNOWN_STEP`, docstrings),
  `auto_approve_github.py` (module docstring, failure strings, comments), `review_requested.py` (comments, docstring),
  `ci_checks.py` (log text, docstrings, `_ZERO_YEAR` comment), `merge.py` (docstring, error text), `regret_review.py`
  (docstrings, the `gh api commits/...` log label), `common.py` (docstrings, log text, `get_gh_token` docstring).
- `vibe_heal/`: `review/github.py` (docstrings), `review/orchestrator.py` (docstrings).
- `sisyphus/`: `cleanup.py` (docstrings, failure text), `orchestrator.py` (`PR_NUMBER_KEY` comment, protocol docstring),
  `cli.py` (docstrings, error text, `--check-prs` help), `pick.py` (docstrings, error text), `config.py` (docstring).
- `knowledge/` prose (never executed by dotharness): `pr-review/` (`review-summary.md`, `address-comment.md`,
  `review-design.md`, `review-traceability.md`, `review-file.md`, `focused-review.md`, `local-review-file.md`,
  `local-review-design.md`), `spec-prism-flow/` (`build-execution-prompt.md`, `plan-generation-prompt.md`,
  `requirements-doc-drafting-prompt.md`, `lifecycle-cheat-sheet.md`), `sisyphus/linear-feed-prompt.md`,
  `patterns/react/` (`COMPONENT_ORGANIZATION_CONVENTION.md`, `QUICK_START_MIGRATION.md`, `QUICK_START_NEW_COMPONENT.md`).
  Knowledge files are not edited in Phase 0.

### Corrections to the starting list

The audit re-ran and re-read the cited sites. Differences from the ticket's starting list:

- INV-PR-11 is `pr list` only (no `search prs`); `search prs` belongs to INV-PR-13.
- INV-PR-24 is `repo_has_workflows` (the "line ~317" caller); INV-PR-23 is `_read_default_branch`; INV-PR-25 is
  `_read_failed_log`; INV-PR-26 is `_failed_step_from_api`.
- INV-PR-31 is `api --method POST .../reviews` (event APPROVE), not a separate review tool.
- INV-PR-32 splits into a `pr view` merge-state read (`_read_pr_state`) and an approval read through `fetch_all_pages`; it
  is not "a review read" alone.
- INV-SY-06 is `pr list --head` only; the `pr view` read is INV-SY-07.
- INV-SY-05 limit 200 is on `open_pr_bodies`; `linked_prs` has no limit.
- INV-PR-44 also covers `ci_fix.py`, `self_review.py`, `review_requested.py` and `address_comments.py`, and the three
  `local_review/` callers pass no token.
- Rows INV-PR-22, 27, 29, 31, 32, 42, INV-SY-03 and 06 were confirmed against the code at the audited commit.

## Phase split

Partition of the Inventory into four ordered phases. Row ids are those of the Inventory section; no row is restated here. The
rename aliases that Phase 4 removes are listed in the alias section of this page, not here. Issue numbers for Phases 2 to 4 are
not in the repo: "to be filed".

### Phase lists

Each inventory row is in exactly one list. Lists are given by row id (ranges are inclusive and contiguous at the audited
commit: 12 + 7 + 47 + 6 = 72 rows).

| Phase | Issue | Rows | Users (file and symbol) |
| --- | --- | --- | --- |
| 1 | #193 | `INV-SY-01` .. `INV-SY-12` | `sisyphus/pick.py` (`GhCli`, `GhLabelRemover`, `GhPullRequestLookup`, candidate lister); `sisyphus/cli.py` (`GhPullRequestClient`, `GhPrStateLookup`, `run_token_cmd` and its env); `sisyphus/config.py` `HostSource.token_cmd` (INV-SY-12, same package as `run_token_cmd`) |
| 2 | #194 | `INV-VH-01` .. `INV-VH-07` | `vibe_heal/review/github.py` (`GitHubReviewClient`) |
| 3 | #195 | `INV-PR-01` .. `INV-PR-47` | `pr_review/runners/*` (`common.py`, `ci_checks.py`, `ci_fix.py`, `auto_approve.py`, `auto_approve_github.py`, `merge.py`, `regret_review.py`, `address_comments.py`, `review_requested.py`, `self_review.py`, `focused_review.py`, `review_prs.py`), `pr_review/cli.py`, `pr_review/config.py` |
| 4 | #196 | `INV-LO-01` .. `INV-LO-05` | `gh.py` (`GH`, `gh_argv`, `build_subprocess_env`); `src/dotharness/scripts/pr-comments.py`; removal of the rename aliases; final grep check |
| none | to be filed in #196 (OD-4) | `INV-LO-06` | `docs/pr_review/scripts/pr-comments.py`: inventoried only, not migrated or deleted. Its Inventory cell reads "none: not migrated"; this is the single allowed exception to "one removing phase per row". |

INV-LO-06 sits in the `leftovers` table of the Inventory for grouping only; its removing phase is "none", so it is excluded
from the four lists. Confirmed decisions: Linear is out of the inventory; INV-PR-46 and INV-PR-47 are Phase 3;
INV-LO-06 is grouped with Phase 4 although it is never migrated. Git code (`repo_guard.py` origin check, `git fetch origin
refs/pull/{n}/head` in `auto_approve.py`) is in no list.

Not users, named so nobody adds them: `sisyphus/orchestrator.py` `PullRequestClient` (existing seam, a Consumer contract that
Phase 1 may re-point at the interface); `sisyphus/cleanup.py` (consumes `PrInfo`); `vibe_heal/review/orchestrator.py` and
`vibe_heal/cli.py` (consume `GitHubReviewClient`, updated in Phase 2 with the client).

### Shared users

- `gh.py` (INV-LO-01): Phase 4. Every group above calls it; it can be deleted only after the last caller has moved, so earlier
  phases keep calling it and it is the last thing to go.
- `pr_review/runners/ci_checks.py` `gh` (INV-PR-07 transport): Phase 3, together with both of its other callers, `merge.py`
  (squash merge write path) and `ci_fix.py` (as `_gh`). All three sit in `pr_review/runners/`; moving the helper alone would
  leave two callers on a deleted function.
- INV-PR-18 (runner side of the pr-comments child process, `common.py` `fetch_pr_comments`) is Phase 3; the script's own gh
  calls (INV-LO-02..05) are Phase 4. The script keeps working unchanged in between because the runners spawn it by path.

### Check method (reviewer-run, no code added)

(a) Extract the id column of the Inventory tables and the ids of the four lists. (b) The sorted concatenation of the four lists
has no duplicate. (c) The set equals the Inventory id set minus `INV-LO-06`, which is listed separately above. (d) For each id,
the table it sits in (its removing phase) equals the list it appears in. Phase 4 ends with the final grep check (the first
three greps of the Audit method must return only non-call hits), so a direct user missed by (c) still fails there.

### Package introduction (R-SPLIT-2)

Phase 1 (#193) creates `src/dotharness/code_host/` (interface, reference implementation, factory, fake) and migrates sisyphus.
This is decision PS-OD-3, confirmed by a human: Phase 1 of #193 creates the `code_host` package.

### Alias removal (R-SPLIT-3)

Phase 4 removes the rename aliases. Any phase that renames what agents see must update the `KNOWLEDGE_SHA256` pins in
`tests/test_package_data.py` deliberately; Phase 0 edits none.

### `scripts/pr-comments.py` (R-SPLIT-4)

Constraint: the script is stdlib only and must not import `dotharness` (F-DUP-8, `docs/design/shared-async-layer.md`). It runs
as a child process through `PR_COMMENTS_SCRIPT_PATH`, used by `focused_review.py`, `address_comments.py` and
`regret_review.py`.

Fact (PS-OD-6, answered): the script never runs the installed dotharness console entry point, so there is no command name to
define. Every child process it starts goes through its local `run()` helper (a wrapper over `subprocess.run`) and invokes only
`gh` (`gh pr view`, `gh api ... -X POST`, `gh pr edit --add-reviewer`) or `git` (`git rev-parse`). It does not use
`sys.executable` or `-m dotharness`, and calls none of the `[project.scripts]` console scripts (`vibe_heal`, `pr_review`,
`spec_prism_flow`, `sisyphus`). (Checked by grep and by reading the `run(` call sites; the rest of the file was not read line by line.)

Consequence: its gh calls are INV-LO-02..05, owned by Phase 4. The earlier assumption that Phase 4 makes the script call a
dotharness entry point is withdrawn. How the script reaches the code-host interface (for example a new neutral entry point, or
receiving already-fetched data from the runners) is a Phase 4 design question for #196. Phase 0 does not define a command name.
Until then the script keeps its `run(argv)` seam so `patch.object(mod, "run", ...)` still works, and `parse_paginated` stays.

Rejected alternative, kept for Phase 4: the script receives already-fetched data. It works only if the three runners fetch
first, which moves the fetch into Phase 3 and changes the script's CLI.

Tests: `tests/test_pr_comments_script.py` passes unchanged through Phase 3. In Phase 4 only the argv asserted in the post-reply
test changes, and that edit is part of Phase 4.

### pr-review is one phase (R-SPLIT-5, PS-OD-5)

pr-review is not split: all of group `pr-review` is Phase 3. In particular `merge.py`, the only squash-merge write path, is not
its own phase. Reason: `ci_checks.gh` is shared by `merge.py` and `ci_fix.py`, so a split would need a temporary adapter that
Phase 0 does not design. Risk: Phase 3 is the largest phase (about fourteen modules). Fallback if reviewers find it too large:
runners versus `cli.py`/`config.py`, with `ci_checks.py`, `merge.py` and `ci_fix.py` together in the first part.

### Open decisions (Phase split)

- PS-OD-3: decided. Phase 1 (#193) creates `src/dotharness/code_host/`.
- OD-4: Which issue deletes `docs/pr_review/scripts/pr-comments.py`? To be filed as part of #196; not blocking.
- PS-OD-5: pr-review is one phase (default stands; no split was requested; reopen only if Phase 3 review is too large).
- PS-OD-6: decided. The script runs no dotharness entry point; no command name. How it reaches the interface is decided in
  Phase 4 (#196).
- OD-8: Issue numbers for Phases 2 to 4 are #194, #195, #196. Remaining "to be filed" items are handled as part of #196.

(The `PS-` prefix avoids clashing with other decision ids used in the Inventory handoff. `sisyphus/config.py` as a user, the
former OD-7, is settled: it is INV-SY-12, Phase 1.)

## Rename plan

Rename plan for `GitHub*`, `Gh*`, `gh_*`, `GH*`, `GITHUB_*` identifiers and configuration keys. It is the alias section that
the Phase split section refers to. Facts below were read from the code at commit `93fa633` (the audit grep counts are
unchanged since `f293b99`). The new names are proposals; they are placeholders until the phase that introduces them. Phase
numbers are those of the Phase split section: Phase 1 (#193) creates `src/dotharness/code_host/` and migrates `sisyphus`;
Phase 2 (#194) migrates `vibe_heal`; Phase 3 (#195) migrates `pr_review`; Phase 4 (#196) removes `gh.py`, migrates both
`pr-comments.py` copies' gh calls and removes every alias. No code, `pyproject.toml`, `src/`, `tests/`, `zensical.toml` or
`src/dotharness/knowledge/` file changes in Phase 0. Examples and new names never name a third-party host; the "Old name"
column quotes existing names.

### Naming rules

- **R1.** New class and function names say "change request", never "PR" (`NoOpenPrError` becomes
  `NoOpenChangeRequestError`). A name for the reference implementation's tool appears only inside the reference implementation
  module and never as a shipped class name (`ReferenceCli` is rejected).
- **R2.** A rename introduced in phase N keeps the old name importable until Phase 4. An alias is a plain re-export
  (`OldName = NewName`) in the old module: no behavior change, no `DeprecationWarning` (a warning would change stderr and test
  output; decision RN-OD-2).
- **R3.** Removal condition for every alias, at Phase 4: `grep -rnE '\b<OldName>\b' src tests` finds no match outside the
  alias definition lines, and `uv run pytest` passes. Rows cite this rule by number and do not restate it.
- **R4.** The "Intro" column is the phase that introduces the new name (the phase of the first user of the old name, or Phase 1
  for names that live in the new package). A consumer stops using the old name in the phase of its own inventory group.
- **R5 (test patching).** Tests patch by module path (for example `patch(p + "get_gh_token")`,
  `patch(p + "build_subprocess_env")` in `tests/pr_review/runners/test_auto_approve.py`). An alias in the old module does not
  intercept a patch aimed at the new name, so a phase that migrates a caller moves that caller's patch targets in the same
  change.

### Identifiers

"Alias" = keep the old name importable until Phase 4 (R2, R3). Ids `REN-nn` are never renumbered.

| Id | Old name | Where | New name | Intro | Alias / removal | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| REN-01 | module `dotharness.gh` | `src/dotharness/gh.py` | `dotharness.code_host.reference` (exact module path set by Phase 1) | 1 | Old module stays a re-export shim until Phase 4 (R3); INV-LO-01 | Package location is `src/dotharness/code_host/`. |
| REN-02 | `GH = "gh"` | `gh.py` | `REFERENCE_CLI = "gh"` in the reference module | 1 | Alias `GH` (R3) | The string `"gh"` is the executable name and does not change. The `GH` in `tests/sisyphus/test_pick.py` is a different object (REN-25). |
| REN-03 | `gh_argv(*args)` | `gh.py`; callers in `vibe_heal/review/github.py`, `sisyphus/pick.py`, `pr_review/runners/*` and tests (49 grep hits) | `reference_argv(*args)` (reference module only) | 1 (defined); consumers stop in 1 (sisyphus), 2 (vibe-heal), 3 (pr-review) | Alias `gh_argv` (R3) | Returns `[REFERENCE_CLI, *args]`; semantics unchanged. Consumers call capability interfaces and stop building argv. |
| REN-04 | `build_subprocess_env(path_prepend, env_vars, gh_token=None)` | `gh.py` | Function name KEEPS. Parameter `gh_token` becomes `host_token`. | 3 | During transition the function accepts both `gh_token=` and `host_token=`; if both are given, `host_token` wins. Removal of the `gh_token=` keyword: R3 on `gh_token=` | Sets `env["GITHUB_TOKEN"]` when the token is truthy (variable name: Child-agent environment). All `pr_review` runners pass it positionally; `local_review/review.py`, `local_review/address.py` and `local_review/git.py` pass no token. Only the parameter name changes. |
| REN-05 | local variable `gh_token` | `pr_review/runners/*.py` (`focused_review`, `ci_fix`, `self_review`, `auto_approve`, `review_requested`, `address_comments`, `review_prs`, `merge`) | `host_token` | 3 | none (local name, not importable) | Internal rename. |
| REN-06 | `get_gh_token(gh_token_cmd)` | `pr_review/runners/common.py` | `get_host_token(token_cmd)` | 3 | Alias `get_gh_token` (R3); R5 applies | Runs the command with `shell=True`. The default string `gh auth token` is not moved or changed (REN-31). Imported by 8 runner modules and patched in tests. |
| REN-07 | `PR_COMMENTS_SCRIPT_PATH` | `pr_review/runners/common.py`; used by `focused_review`, `address_comments`, `regret_review` | KEEP | n/a | n/a | It names a script path, not a host. The script `src/dotharness/scripts/pr-comments.py` is stdlib only, runs as a child process and must not import `dotharness` (F-DUP-8 in `docs/design/shared-async-layer.md`). "PR" stays per the glossary. How the script reaches the interface is a Phase 4 design question (see `scripts/pr-comments.py` in Phase split); this row does not decide it (RN-OD-4). |
| REN-08 | `gh_json`, `gh_json_paginated` | `scripts/pr-comments.py` (both copies) | KEEP | n/a | n/a | Script-local helpers, never imported by `dotharness` code (the file name is not a valid module name). The file must stay dependency-free. Their gh calls are INV-LO-02..06. |
| REN-09 | `ci_checks.gh` (`async def gh(args, env)`), imported as `_gh` by `merge.py` and `ci_fix.py` | `pr_review/runners/ci_checks.py` | `run_host_cli(args, env)` in `ci_checks.py`; consumers move to the Checks capability | 3 | Alias `gh`; the `_gh` import alias is local to each importer and disappears when that importer is migrated (R3 covers `ci_checks.gh`) | 14 `_gh` grep hits. `merge.py`, `ci_fix.py` and `ci_checks.py` move together (R-SPLIT-5). |
| REN-10 | `GitHubReviewClient` | `vibe_heal/review/github.py`; used in `vibe_heal/review/orchestrator.py`, `vibe_heal/cli.py` and tests (55 hits) | `ChangeRequestReviewClient` (reference module); consumers depend on the ChangeRequests and Discussion interfaces | 2 | Alias `GitHubReviewClient` (R3) | `vibe_heal/review/github.py` becomes a shim under the same alias rule. |
| REN-11 | `GitHubReviewError` | `vibe_heal/review/github.py` | `ChangeRequestReviewError` (in `code_host`) | 2 | Alias `GitHubReviewError` (R3). Subclass relation preserved: the aliases of REN-12 and REN-13 stay subclasses, so `except GitHubReviewError` still catches them | Existing catches must keep working without edit. |
| REN-12 | `NoOpenPrError(GitHubReviewError)` | same | `NoOpenChangeRequestError` | 2 | Alias `NoOpenPrError` (R3) | R1. Also re-exported from `vibe_heal/review/__init__.py` and imported by `vibe_heal/cli.py`; both keep working through the alias. |
| REN-13 | `GitHubTimeoutError(GitHubReviewError)` | same | `HostTimeoutError` | 2 | Alias `GitHubTimeoutError` (R3) | |
| REN-14 | `GitHubError` | `sisyphus/orchestrator.py`, imported by `sisyphus/cli.py` | `HostError` (in `code_host`) | 1 | Alias `GitHubError` (R3) | Raised at the sisyphus orchestration boundary; messages unchanged. |
| REN-15 | `GhCli` | `sisyphus/pick.py` (27 hits; constructed as `GhCli(cwd, token, executor)`) | `HostCli` (generic command runner, reference module only) | 1 | Alias `GhCli` (R3); constructor signature unchanged | Sets `GH_TOKEN` in the child env (Child-agent environment). |
| REN-16 | `GhLabelRemover` | `sisyphus/pick.py` | `WorkItemLabelRemover` (WorkItems capability) | 1 | Alias (R3) | `LabelRemovalError(PickError)` is REN-26. |
| REN-17 | `GhTimeboundLabelRemover(GhLabelRemover)` | `sisyphus/cli.py` | `TimeboundWorkItemLabelRemover` | 1 | Alias (R3) | Subclass of REN-16; its alias must point at the renamed subclass of the renamed base. |
| REN-18 | `GhPullRequestLookup` | `sisyphus/pick.py` | `ChangeRequestLookup` | 1 | Alias (R3) | R1. |
| REN-19 | `GhPullRequestClient` | `sisyphus/cli.py` | `ChangeRequestClient` | 1 | Alias (R3) | |
| REN-20 | `GhPrStateLookup` | `sisyphus/cli.py` | `ChangeRequestStateLookup` | 1 | Alias (R3) | |
| REN-21 | `GitHubIssueTracker` | `sisyphus/pick.py`, `sisyphus/cli.py` | `WorkItemTracker` (WorkItems implementation for the reference host) | 1 | Alias (R3) | The config value `kind = "github"` is not renamed (REN-33). |
| REN-22 | `GITHUB_OPEN_STATE = "OPEN"` | `sisyphus/pick.py` | `CHANGE_REQUEST_OPEN_STATE` (value `"OPEN"` unchanged) | 1 | Alias (R3). If the neutral model exposes an enum, the constant is deleted at Phase 4 under the same rule | The value is the reference host's state string, compared case-insensitively. |
| REN-23 | `GITHUB_FOOTER_LINE` | `vibe_heal/cli.py` | KEEP | n/a | n/a | It is this project's own repository link, not a host dependency. Its text is user-visible output. |
| REN-24 | `GITHUB_SOURCE` | `tests/sisyphus/test_abandon.py` (TOML snippet with `kind = "github"`) | KEEP in Phase 0; the Phase 1 test edit may rename it | 1 (optional) | none (test constant, not importable by `src`) | Listed for completeness of the grep. |
| REN-25 | `GH` (test fixture `WorkItemSource(kind="github", ...)`) | `tests/sisyphus/test_pick.py` | KEEP in Phase 0; the Phase 1 test edit may rename it | 1 (optional) | none | Not the `GH` of `gh.py` (REN-02); the 13 `GH` grep hits are 1 definition in `gh.py`, 1 in `gh.py`'s `gh_argv` and 11 in this test file. |
| REN-26 | `PickError`, `LabelRemovalError(PickError)` | `sisyphus/pick.py` | KEEP | n/a | n/a | They describe the operation, not a host. They do not match the grep patterns; checked on purpose. |
| REN-27 | `MergeError`, `AutoApproveError`, `CiFixError` | `pr_review/runners/merge.py`, `auto_approve.py`, `ci_fix.py` | KEEP | n/a | n/a | Named for the command. Whether Phase 3 wraps `HostError` into them is a Phase 3 design point (RN-OD-5). |
| REN-28 | `_IssueFetchError` | `pr_review/runners/common.py` | KEEP | n/a | n/a | Private (underscore), no host pattern, not importable by external code. |
| REN-29 | `gh_ops` | only the ledger row `spec_prism_flow.build.gh_ops` in `tests/data/ledger_modules.tsv` (path `tools/spec-prism-flow/spec_prism_flow/build/gh_ops.py`, not under `src/dotharness`) | KEEP | n/a | n/a | The ledger is frozen. |
| REN-30 | `GitHub` alone (about 151 hits) and the footer text | prose in docstrings, comments, user-facing text | KEEP | n/a | n/a | Not an identifier; no import or config contract depends on it. |

### Configuration keys and values

Keys are renamed only additively. "Gh" in the config key column is quoted from existing files.

| Id | Old key | File / section | New key | Precedence and behavior | Alias / removal |
| --- | --- | --- | --- | --- | --- |
| REN-31 | `gh_token_cmd` | `[harness]` of the `pr_review` config; field `gh_token_cmd: str = "gh auth token"` in `pr_review/config.py`; the template written by `pr_review/cli.py`; docs `docs/pr_review/configuration.md`, `commands/address-comments.md`, `commands/self-review.md`, `commands/local-review.md` | `host_token_cmd` (additive) | If only `gh_token_cmd` is set, behavior is exactly today's. If only `host_token_cmd` is set, its value is used with the same semantics. If both are set, `host_token_cmd` wins, the old key is ignored and no error is raised (RN-OD-6). Neither key set: the effective value is `gh auth token`, same as today. The command string runs with `shell=True` exactly as in `get_gh_token`. | Introduced in Phase 3. The old key is accepted until Phase 4. Removal: R3 on `gh_token_cmd`, docs mention only the new key. Users' config files that still use the old key break at Phase 4, so the Phase 4 release notes MUST say so. |
| REN-32 | `token_cmd` | `[source.host]` of the sisyphus config (`token_cmd: str \| None = None` in `sisyphus/config.py`; `run_token_cmd` in `sisyphus/cli.py`) | KEEP, no new key | Unchanged: default `None`, split with `shlex.split`, no shell, no default command. The key is already neutral. | n/a |
| REN-33 | `kind = "github"` | `[source.work_item]` of the sisyphus config; `TRACKER_KINDS` in `sisyphus/config.py` | KEEP the value `"github"`. A neutral spelling would be additive and is chosen when the WorkItems capability is designed. | Existing configs must load unchanged. User-visible error messages (for example "mixed tracker") are unchanged. | n/a (RN-OD-7) |
| REN-34 | `[harness] env` passthrough (`config.harness.env`) | `pr_review/config.py` | KEEP | Used as `{**config.harness.env, "GITHUB_TOKEN": token}` in `focused_review.py`, `address_comments.py`, `review_requested.py`, `self_review.py` and `ci_fix.py`: the explicit variable overrides a same-named `env` entry. This order is preserved. | n/a |

Loader constraint (binds REN-32, REN-33 and any future sisyphus key): `src/dotharness/sisyphus/config.py` is stdlib only,
never executes `token_cmd` and never reads the environment. A neutral sisyphus key is a plain parsed value; resolving it
(running a command, reading a variable) belongs to `sisyphus/cli.py` and Session, not the loader.

### Child-agent environment

Owned by Session. This section states the rule and the table only.

| Id | Variable | Set by | Decision |
| --- | --- | --- | --- |
| REN-35 | `GITHUB_TOKEN` | `gh.py` `build_subprocess_env` (when the token is truthy); hand-built dicts in `focused_review.py`, `address_comments.py`, `review_requested.py`, `self_review.py`, `ci_fix.py` (the last three read it back from `env`) | KEEP the name for the reference implementation. |
| REN-36 | `GH_TOKEN` | `sisyphus/pick.py` `GhCli` | KEEP the name for the reference implementation. |

Rule (RN-OD-1): with the default config the child process receives exactly the same variable names and values as today. A
neutral variable name is allowed only if behavior does not change. Proposed design: Session exposes the variable names as
data (a mapping returned for the reference implementation containing `GITHUB_TOKEN` and `GH_TOKEN` as applicable per call
site), and hand-built env dicts migrate to that data in Phase 3. Knowledge files under `src/dotharness/knowledge/` that
mention `gh` or `GITHUB_TOKEN` are pinned by `KNOWLEDGE_SHA256` in `tests/test_package_data.py`; a phase that changes
anything agents see updates the hashes deliberately in the same change.

### Rename audit grep

Run from the repository root (result at `93fa633`, identical to the count at `f293b99`):

```
grep -rhoE "\b(GitHub[A-Za-z_]*|Gh[A-Z][A-Za-z_]*|gh_[a-z_]+|GH_[A-Z_]+|GH\b|GITHUB_[A-Z_]+|_gh\b|token_cmd)" src tests | sort | uniq -c | sort -rn
```

Counts (prose included): `GitHub` 151, `GitHubReviewClient` 55, `gh_argv` 49, `GhCli` 27, `gh_token_cmd` 26, `gh_token` 25,
`GITHUB_TOKEN` 23, `GitHubReviewError` 20, `GitHubError` 20, `token_cmd` 16, `_gh` 14, `GH` 13, `GitHubTimeoutError` 8,
`GhPullRequestLookup` 8, `GhLabelRemover` 8, `GhPullRequestClient` 6, `GitHubIssueTracker` 5, `gh_json` 5, `GITHUB_SOURCE` 4,
`GITHUB_FOOTER_LINE` 4, `GH_TOKEN` 4, `gh_json_paginated` 4, `GhTimeboundLabelRemover` 3, `GITHUB_OPEN_STATE` 2,
`GhPrStateLookup` 2, `gh_ops` 2.

Coverage check for reviewers: every name above maps to a row. `GitHub` REN-30; `GitHubReviewClient` REN-10; `gh_argv`
REN-03; `GhCli` REN-15; `gh_token_cmd` REN-31 (also REN-06 parameter); `gh_token` REN-04, REN-05; `GITHUB_TOKEN` REN-35;
`GitHubReviewError` REN-11; `GitHubError` REN-14; `token_cmd` REN-32 (and REN-31, REN-06); `_gh` REN-09; `GH` REN-02, REN-25;
`GitHubTimeoutError` REN-13; `GhPullRequestLookup` REN-18; `GhLabelRemover` REN-16; `GhPullRequestClient` REN-19;
`GitHubIssueTracker` REN-21; `gh_json`, `gh_json_paginated` REN-08; `GITHUB_SOURCE` REN-24; `GITHUB_FOOTER_LINE` REN-23;
`GH_TOKEN` REN-36; `GhTimeboundLabelRemover` REN-17; `GITHUB_OPEN_STATE` REN-22; `GhPrStateLookup` REN-20; `gh_ops` REN-29.
Companion queries for names the pattern cannot see: `grep -rnE "^class \w*Error" src` (REN-11..14, 26..28) and
`grep -rnE "PR_COMMENTS_SCRIPT_PATH|def get_gh_token|def gh\b|_IssueFetchError" src` (REN-06, 07, 09, 28).
`NoOpenPrError` (REN-12) is not matched by the pattern; it is found by the first companion query.

### Open decisions (Rename plan)

- RN-OD-1: Session exposes variable names as data and keeps `GITHUB_TOKEN` and `GH_TOKEN` (proposed default), or a neutral
  variable name is wanted. Open unless the interface section closes it.
- RN-OD-2: aliases emit no `DeprecationWarning` (proposed default; a warning changes stderr and test output).
- RN-OD-4: `PR_COMMENTS_SCRIPT_PATH` may later be re-pointed to a host-declared script (default: keep).
- RN-OD-5: whether `MergeError`, `AutoApproveError`, `CiFixError` wrap `HostError` in Phase 3 (default: yes, by wrapping at
  raise sites only; deferred to Phase 3).
- RN-OD-6: warn when both `gh_token_cmd` and `host_token_cmd` are set (default: no warning, `host_token_cmd` wins).
- RN-OD-7: neutral spelling for `kind = "github"` (default: none in Phases 1 to 3).
- Phase numbers: reconciled with the Phase split section (the leaf's draft numbering is replaced).
- A neutral `[harness]` key (`host_token_cmd`) is included as additive-only; no further decision pending.

(The `RN-` prefix avoids clashing with the Inventory and Phase split decision ids.)

## Interfaces

Capability interfaces of the code host (A-2.1). Each is a `typing.Protocol` in the planned package
`src/dotharness/code_host/`. This section owns method names, parameters and which interface owns which inventory row. It does
not define model types (Models section), error types (Error model section) or limits, anchor rules and capability
declarations (Declarations section); it only names them. Those sections and the factory section are written by other
slices; until they land, the names below are the contract the other sections must accept or rename here. The Inventory
section is the one place that assigns each operation to an owner; the "Rows" column below lists the ids so the coverage can be
checked, and does not restate the assignment.

### Rules for every interface

1. Every method is `async def`.
2. Every method returns a host-neutral model (Models section), a `Sequence` of models, `str`, `bool` or `None`. No method
   returns a `dict` from a host tool and there is no `api(...)`-style pass-through that takes a path or a query.
3. Pagination is an implementation detail. No parameter, return type or error in this section mentions a page, a cursor or a
   paginate flag. A method that lists returns the complete result up to the host's declared limit. Where a caller needs a bound
   today (`--limit 1000`, `per_page=100`, `limit=200`), the bound is a declared limit owned by the Declarations section.
4. Repository-scoped methods take `repo: RepositoryRef` as the first argument after `self`. A `RepositoryRef` is host-defined
   and opaque: consumers never parse, split or compare its parts and never read a remote URL to build one.
5. Failures raise subclasses of `HostError` (Error model section). A name used below is `HostError` or one of
   `HostAuthError`, `HostNotFoundError`, `HostTimeoutError`, `HostConflictError`, `HostUnsupportedError`; the Error model
   section decides the final names and when each applies.
6. Naming: new names use "change request"; existing code keeps "PR" until the phase that migrates it (Rename plan).

### Repository resolution: explicit and cwd-based (D-12)

Two consumers resolve the repository differently today: `pr-review` passes an explicit identity to every call; `vibe-heal`
and `sisyphus` run in a clone and pass nothing. One method signature serves both because identity is a value, not an
ambient setting:

- `Repository.resolve(identity)` (explicit mode) turns the host-defined identity string the user supplied (for example the
  `--repo` value) into a `RepositoryRef`. The consumer passes the string through unchanged; only the host interprets it.
- `Repository.current()` (cwd mode) returns the `RepositoryRef` of the clone the host was created for. No identity string
  appears in the call, and the consumer never reads a remote URL.
- Every other method takes the resulting `RepositoryRef`. A cwd-based consumer calls `current()` once and passes the result;
  an explicit consumer calls `resolve()` once per repository. The implementation of a cwd-mode ref may omit any repository
  argument from its host calls (this is what sisyphus does today).

The factory's return value exposes both modes: it returns the capability objects together with a `RepositoryRef` source that
supports `current()` and `resolve()` (factory section). A host that cannot do one mode raises `HostUnsupportedError` from that
method and declares so (Declarations section).

### Session

Credential, environment, availability and current user. Not repository-scoped.

```python
from collections.abc import Mapping, Sequence
from typing import Protocol


class Session(Protocol):
    async def available(self) -> bool:
        """True when the host tool or service can be used. Never raises."""
        ...

    async def credential(self) -> Credential:
        """Resolve the token (configured command or default). Raises HostAuthError, HostTimeoutError."""
        ...

    async def current_user(self) -> str:
        """Login of the acting user. Raises HostAuthError, HostTimeoutError."""
        ...

    async def agent_environment_names(self) -> Sequence[str]:
        """Names of the variables a child agent process sees for this host, as data."""
        ...

    async def agent_environment(self, base: Mapping[str, str] | None = None) -> Mapping[str, str]:
        """base plus the credential variables, base entries first so credential variables win. Raises HostAuthError."""
        ...
```

| Method | Replaces | Rows |
| --- | --- | --- |
| `available` | `gh --version`, `shutil.which("gh")` | INV-VH-01, INV-PR-46 |
| `credential` | `get_gh_token`, `run_token_cmd` | INV-PR-01, INV-SY-11, INV-SY-12 (config input) |
| `current_user` | `api user --jq .login`; today fails open and returns `""`, here it raises and the consumer decides | INV-PR-03 |
| `agent_environment_names`, `agent_environment` | `build_subprocess_env`, hand-built env dicts, `GhCli` env | INV-LO-01, INV-PR-44, INV-PR-45, INV-SY-01, INV-PR-47 (config default) |
| (transport errors) | `_run_gh` error adapter | INV-VH-07 |

Environment (D-3, OD-1): for the reference implementation `agent_environment_names()` returns `("GITHUB_TOKEN", "GH_TOKEN")` and
`agent_environment()` sets the variables per call site exactly as today (Rename plan REN-35, REN-36). The names are data
returned by Session, never constants in consumers. Examples in docs write the value as `<token>`. OD-1 default accepted per
human instruction: keep `GITHUB_TOKEN` and `GH_TOKEN` for the reference implementation, no neutral variable name.

### Repository

```python
class Repository(Protocol):
    async def current(self) -> RepositoryRef:
        """cwd mode: the repository of the clone. Raises HostNotFoundError."""
        ...

    async def resolve(self, identity: str) -> RepositoryRef:
        """explicit mode: host-defined identity string to a ref. Raises HostNotFoundError."""
        ...

    async def default_branch(self, repo: RepositoryRef) -> str:
        """Name of the default branch. Raises HostNotFoundError."""
        ...

    async def display_name(self, repo: RepositoryRef) -> str:
        """Human-readable identity for logs and messages only. Consumers must not parse it."""
        ...
```

| Method | Replaces | Rows |
| --- | --- | --- |
| `current` | `_get_owner_repo` with `git remote get-url origin` fallback and `_parse_remote_url` | INV-VH-05, INV-VH-06 |
| `resolve` | explicit `--repo` string plus `repo.split("/")` | INV-VH-05 |
| `default_branch` | `repo view --json defaultBranchRef` | INV-SY-10, INV-PR-23 |

### ChangeRequests

Read and write on change requests, including approval, merge and per-file viewed state (viewed state is owned here by the
Inventory, Owner justifications 2). `ChangeRequest` carries state, draft flag, head and base reference, head revision,
description, additions and deletions, requested reviewers, merge state and author (Models section).

```python
class ChangeRequests(Protocol):
    async def get(self, repo: RepositoryRef, number: int) -> ChangeRequest:
        """Raises HostNotFoundError."""
        ...

    async def find_by_branch(
        self, repo: RepositoryRef, branch: str, *, state: ChangeRequestState = ChangeRequestState.OPEN
    ) -> Sequence[ChangeRequest]:
        """Change requests whose head is `branch`. Empty when none."""
        ...

    async def list(
        self,
        repo: RepositoryRef,
        *,
        state: ChangeRequestState = ChangeRequestState.OPEN,
        author: str | None = None,
        assignee: str | None = None,
        requested_reviewer: str | None = None,
    ) -> Sequence[ChangeRequest]:
        """Filters are logins; the consumer resolves "me" with Session.current_user()."""
        ...

    async def find_by_commit(self, repo: RepositoryRef, revision: str) -> Sequence[ChangeRequest]:
        """Change requests that contain the commit."""
        ...

    async def commits(self, repo: RepositoryRef, number: int) -> Sequence[CommitInfo]:
        """Commits with their dates, oldest first."""
        ...

    async def reviews(self, repo: RepositoryRef, number: int) -> Sequence[Review]:
        """All reviews (author, verdict, commit reviewed)."""
        ...

    async def create_draft(
        self, repo: RepositoryRef, *, head: str, base: str, title: str, body: str
    ) -> ChangeRequest:
        """Raises HostConflictError, HostError."""
        ...

    async def squash_merge(self, repo: RepositoryRef, number: int, *, expected_head: str) -> None:
        """Squash-merge only if the head revision equals `expected_head`. Raises HostConflictError on mismatch."""
        ...

    async def request_reviewer(self, repo: RepositoryRef, number: int, login: str) -> None: ...

    async def approve(self, repo: RepositoryRef, number: int, *, head_revision: str) -> None:
        """Approve the change request at `head_revision`."""
        ...

    async def viewed_files(self, repo: RepositoryRef, number: int) -> Mapping[str, bool]:
        """Path to viewed flag for the acting user."""
        ...

    async def mark_viewed(self, repo: RepositoryRef, number: int, path: str) -> None: ...
```

| Method | Replaces | Rows |
| --- | --- | --- |
| `get` | `pr view` with chosen fields: state, base, head revision, description, line counts, requested reviewers, merge state | INV-SY-07, INV-SY-09, INV-VH-02, INV-PR-02, INV-PR-04, INV-PR-05, INV-PR-14, INV-PR-15, INV-PR-16, INV-PR-32 (merge state), INV-PR-33, INV-LO-02 |
| `find_by_branch` | `pr list --head` | INV-SY-06, INV-PR-21 |
| `list` | `pr list` with author, assignee, reviewer, state; review-requested search | INV-PR-11, INV-PR-12, INV-PR-13, INV-PR-27, INV-PR-37, INV-PR-38, INV-PR-41, INV-PR-43 |
| `find_by_commit` | `commits/SHA/pulls` | INV-PR-35 |
| `commits` | `pr view --json commits` | INV-PR-28 |
| `reviews` | reviews listing, has-user-approved | INV-PR-07 (transport), INV-PR-32 (approval half), INV-PR-42 |
| `create_draft` | `pr create --draft` | INV-SY-08 |
| `squash_merge` | `pr merge --squash --match-head-commit` | INV-PR-34 |
| `request_reviewer` | `pr edit --add-reviewer` | INV-PR-06, INV-LO-05 |
| `approve` | POST review `event=APPROVE` with commit id | INV-PR-31 |
| `viewed_files`, `mark_viewed` | viewed-state query, `markFileAsViewed` | INV-PR-29, INV-PR-30 |

Notes:

- The leaf lists "by head" next to "by branch". Both mean the head branch name; no inventory row looks a change request up by
  head revision, so there is one method, `find_by_branch`. "Current branch" (INV-VH-02) is `find_by_branch` with the branch
  the consumer read from git; there is no ambient-branch method.
- "Is the user's approval current" (INV-PR-32, INV-PR-42) is computed by the consumer from `reviews()` and the
  `ChangeRequest` head revision.
- The viewed-state owner (R-INV-3) was decided in the Inventory as ChangeRequests; Discussion does not repeat it.

### Discussion

```python
class Discussion(Protocol):
    async def comments(self, repo: RepositoryRef, number: int) -> Sequence[Comment]:
        """Change-request-level comments."""
        ...

    async def review_threads(self, repo: RepositoryRef, number: int) -> Sequence[ReviewThread]:
        """Inline review comments grouped in threads, each with its resolved state."""
        ...

    async def post_comment(self, repo: RepositoryRef, number: int, body: str) -> Comment: ...

    async def post_review(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        body: str,
        inline_comments: Sequence[InlineComment],
        head_revision: str | None = None,
    ) -> ReviewPostResult:
        """Post a review. If the host rejects the review or its anchors, post `body` as a top-level comment.

        `ReviewPostResult.fell_back` tells the caller which happened. Raises HostError only if both fail.
        Size limits and anchor rules are declared (Declarations section), not parameters.
        """
        ...

    async def reply(self, repo: RepositoryRef, number: int, thread_id: str, body: str) -> Comment:
        """Reply in a review thread; `thread_id` is the opaque id from ReviewThread."""
        ...
```

| Method | Replaces | Rows |
| --- | --- | --- |
| `comments` | human issue comments, CR comment fetch | INV-PR-40, INV-PR-18 (the child-process fetch), INV-LO-03 |
| `review_threads` | GraphQL review-thread state and unresolved count | INV-PR-17, INV-PR-39, INV-PR-18, INV-LO-03 |
| `post_comment` | `pr comment` | INV-PR-10, INV-PR-36 |
| `post_review` | POST review with inline comments, and the fallback comment | INV-VH-03, INV-VH-04 |
| `reply` | POST reply in a thread | INV-LO-04 |

INV-LO-06 (documentation copy, not migrated) is not covered by any method. How `scripts/pr-comments.py` reaches this
interface is a Phase 4 design question (Phase split).

### Checks

```python
class Checks(Protocol):
    async def list_checks(
        self, repo: RepositoryRef, number: int, *, required_only: bool = False
    ) -> Sequence[Check]:
        """Checks of a change request, optionally only those the host requires."""
        ...

    async def list_workflows(self, repo: RepositoryRef) -> Sequence[Workflow]:
        """Empty when the repository has none."""
        ...

    async def failed_log(self, repo: RepositoryRef, check: Check) -> str:
        """Log of the failing part of `check`. Raises HostNotFoundError."""
        ...

    async def get_job(self, repo: RepositoryRef, job_id: str) -> Job:
        """A job with its failing step name; `job_id` is the opaque id from Check."""
        ...
```

| Method | Replaces | Rows |
| --- | --- | --- |
| `list_checks` | `pr checks --json`, `--required`, required names of the stack root | INV-PR-20, INV-PR-22 |
| `list_workflows` | workflows listing | INV-PR-24 |
| `failed_log` | `run view --log-failed` | INV-PR-25 |
| `get_job` | job read for the failed step | INV-PR-26 |
| (transport) | `ci_checks.gh` timeout mapping | INV-PR-19 |

### WorkItems

Not every host supports work items. The capability is declared (Declarations section); a host without it raises
`HostUnsupportedError` from every method here.

```python
class WorkItems(Protocol):
    async def get(self, repo: RepositoryRef, number: int) -> WorkItem:
        """Title and body. Raises HostNotFoundError."""
        ...

    async def list_open(
        self, repo: RepositoryRef, *, assignee: str | None = None, label: str | None = None
    ) -> Sequence[WorkItem]: ...

    async def remove_label(self, repo: RepositoryRef, number: int, label: str) -> None: ...

    async def closing_change_requests(
        self, repo: RepositoryRef, number: int
    ) -> Sequence[ChangeRequest]:
        """Change requests the host records as closing the work item (reference lookup plus per-reference view)."""
        ...

    async def open_change_request_bodies(self, repo: RepositoryRef) -> Sequence[ChangeRequest]:
        """Open change requests with their descriptions, for a consumer-side scan for mentions. Bounded by a declared limit."""
        ...

    async def linked_work_items(self, repo: RepositoryRef, number: int) -> Sequence[WorkItem]:
        """Work items the host records as closed by the change request."""
        ...
```

| Method | Replaces | Rows |
| --- | --- | --- |
| `get` | `issue view` | INV-PR-08 |
| `list_open` | `issue list --state open --assignee --label --limit 100` | INV-SY-03 |
| `remove_label` | `issue edit --remove-label` | INV-SY-02 |
| `closing_change_requests` | reference lookup, R-INV-2 (a) and (b) | INV-SY-04 |
| `open_change_request_bodies` | repo-wide body scan, R-INV-2 (c); stays a separate operation | INV-SY-05 |
| `linked_work_items` | closing-issue references of a change request | INV-PR-09 |

Per R-INV-2 the two lookups are not merged: the first returns what the host records, the second returns text for the
consumer to match, which is not a host relation. The `limit=200` of the second is a declared limit (Declarations section).

### Row coverage

Every inventory row other than INV-LO-06 appears in exactly one Rows cell above or is named as transport or config. Check:
extract the `INV-` ids from the Rows cells of this section and compare with the Inventory id set minus INV-LO-06; the
difference must be empty. Rows listed under two interfaces (INV-PR-18, INV-LO-03, INV-PR-07, INV-PR-32) are shared reads whose
owner in the Inventory is the first interface named there.

### Open decisions (Interfaces)

- OD-1: neutral agent-visible variable name. Default accepted per human instruction: keep `GITHUB_TOKEN` and `GH_TOKEN` for
  the reference implementation and expose the names as data from Session (same as RN-OD-1).
- Error names in rule 5 and model names are proposals until the Error model and Models sections exist; they are reconciled
  there, not here.
