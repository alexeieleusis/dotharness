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
