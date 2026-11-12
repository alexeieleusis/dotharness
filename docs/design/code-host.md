# Code host: capability interfaces and direct gh user inventory

Status: Phase 0 design for #192. The package `src/dotharness/code_host/` exists and the vibe-heal review flow is implemented (Phase 1, #193). The sisyphus flow is implemented (Phase 2, #194). The interface version is 2. The pr-review and leftover users are not migrated yet.

This page is the contract for Phases 1 to 4 (#193 to #196). Sections: Inventory, Phase split, Rename plan, Interfaces,
Models, Error model, Declarations, Factory and selection, Conformance suite and in-memory fake, and the Decision log
below. The Inventory section lists every direct gh user in `src/` with one stable row id, one owner capability, and one
removing phase each. Decisions are cited by `DEC-n`. Row ids (`INV-`, `REN-`, `B-`) cite the tables.

## Decision log

One id namespace. Every entry is decided. The entry gives the rationale.

| Id | Decision | Rationale |
|---|---|---|
| DEC-1 | Phase 1 (#193) creates `src/dotharness/code_host/` and migrates vibe-heal (`INV-VH-*`). Phase 2 (#194) migrates sisyphus (`INV-SY-*`). | Confirmed by a human. Vibe-heal is the first migrating tool: its review flow needs the fewest capabilities. |
| DEC-2 | The issue that deletes `docs/pr_review/scripts/pr-comments.py` is filed as part of #196. | Not blocking earlier phases. |
| DEC-3 | pr-review is one phase (Phase 3). | No split was requested. Reopen only if Phase 3 review is too large. |
| DEC-4 | `scripts/pr-comments.py` runs no dotharness entry point, so there is no command name. How it reaches the interface is decided in Phase 4 (#196). | The script only starts `gh` and `git` through its local `run()` helper. |
| DEC-5 | Issue numbers for Phases 2 to 4 are #194, #195, #196. Remaining "to be filed" items are handled in #196. | Fixed by the ticket breakdown. |
| DEC-6 | Session exposes the agent-visible variable names as data and keeps `GITHUB_TOKEN` and `GH_TOKEN` for the reference implementation. No neutral name. | With the default config the child process must receive the same variable names and values as today. |
| DEC-7 | Rename aliases emit no `DeprecationWarning`. | A warning changes stderr and test output. |
| DEC-8 | `PR_COMMENTS_SCRIPT_PATH` is kept and may later be re-pointed to a host-declared script. | It names a script path, not a host. |
| DEC-9 | `MergeError`, `AutoApproveError`, `CiFixError` keep their names and stay consumer-local. Phase 3 wraps `CodeHostError` in them at raise sites only (`raise ... from`). | They are named for the command. Only the cause chain changes. |
| DEC-10 | No warning when both `gh_token_cmd` and `host_token_cmd` are set. `host_token_cmd` wins. | Additive key. The old key is accepted until Phase 4. |
| DEC-11 | `kind = "github"` stays. No neutral spelling in Phases 1 to 3. | Existing configs and error messages must load unchanged. |
| DEC-12 | A neutral `[harness]` key `host_token_cmd` is added, additive only. | Same semantics as `gh_token_cmd`. |
| DEC-13 | Display label is `Repository.display_name(repo)`. | Display is a method, not an attribute consumers read. |
| DEC-14 | `WorkflowRun` is omitted. | No consumer in the audit. |
| DEC-15 | Default branch is `Repository.default_branch(repo)`. | There is no `Repository` model. |
| DEC-16 | The REST and GraphQL fields read by inventory rows are all listed in the mapping table. | Coverage check requires every field a row reads. |
| DEC-17 | `ChangeRequest` has no `commits` or `linked_work_items` field. Consumers call `ChangeRequests.commits()` and `WorkItems.linked_work_items()`. | A host read that returns them with the change request for free, if a consumer needs it. |
| DEC-18 | Model names are `CommitInfo`, `ReviewThread`, `Check`. Error names are `CodeHostError`, `AuthenticationError`, `CodeHostTimeoutError`, `UnsupportedCapabilityError`, `ConfigurationError`, `NoChangeRequestError`. | The Interfaces section is the naming authority. |
| DEC-19 | `PickError` covers gh failures. The Linear path keeps a consumer-local error. | The Linear path is not a code host. |
| DEC-20 | No type or discriminator for rate limiting (such as `retry_after`) until a consumer needs it. | No current consumer. |
| DEC-21 | At F1 the host skips the fallback post on `AuthenticationError` and `UnsupportedCapabilityError`. | A plain post would fail the same way. |
| DEC-22 | The unsupported-capability policy table stands. | No current code path raises it. |
| DEC-23 | The three distinctions are subclasses, not a single `kind` discriminator. | Consumers catch by type. |
| DEC-24 | Review body limit and CR body limit are two fields with equal reference values. | Hosts may differ. |
| DEC-25 | The `limit=200` default is `GhPullRequestLookup.__init__` in `sisyphus/pick.py` (`open_pr_bodies`, INV-SY-05). The `limit=100` default is `GitHubIssueTracker.__init__` (INV-SY-03). The caller's pick count stays a consumer parameter. | Found in code. |
| DEC-26 | `Capability` values are the interface names verbatim. | One name per concept. |
| DEC-27 | Anchor values are derived from the review posting path and its tests, as in the Declarations table. | No value is a guess. |
| DEC-28 | `per_operation` keys are `Interface.method`. | Interface names trace to inventory rows. |
| DEC-29 | The 10 s `validate_installed` and 10.0 s `PR_STATE_TIMEOUT` stay separate entries. | Different operations. |
| DEC-30 | The 10.0 s of INV-SY-09 belongs to the narrow `ChangeRequests.state`. `ChangeRequests.get` keeps the 30 s default that INV-PR-02/04/05/14/15/16/32/33 run under today. The 30 s of INV-VH-02 belongs to `ChangeRequests.find_by_branch`. | `get` fetches the heavier field set. A 10 s key on it would make slow reads fail in pr-review, against the "invisible with the default configuration" requirement. |
| DEC-31 | The `--limit 500` listings are declared under the 1000 bound of `ChangeRequests.list`. Methods that fetch every page today (INV-PR-07 and the `--paginate` and cursor loops) declare `None` (unbounded). A host that cannot return everything raises `CodeHostError` rather than truncate. Only reads with a bound today keep a numeric bound. | `per_page=100` is a page size, not a bound. Declaring 100 would silently truncate what the safety gates read in full (`_has_user_approved`, `_check_approval_safety`, `_check_human_issue_comments`). |
| DEC-32 | New sisyphus key `name` under `[source.host]`. `kind` unchanged. | Existing configs keep loading. |
| DEC-33 | Version match is exact, no min/max window. | A window needs a second constant and can be added by a later breaking change. |
| DEC-34 | Undeclared capabilities are substituted by objects that raise `UnsupportedCapabilityError`, not `None`. | Consumers fail with a typed error. |
| DEC-35 | The built-in reference name is `reference`. | Neutral to any host. |
| DEC-36 | Sibling sections are cited by name. The Interfaces section wins where capability tables differ. | One authority per fact. |
| DEC-37 | The fake and conformance base live in the single file `code_host/testing.py`. If it outgrows one file it becomes the package `code_host/testing/` with the same import path. | Simplest layout. |
| DEC-38 | The conformance suite is a base class, not a pytest fixture. `pytest` is a dev-group dependency only. | A pytest import would break `import dotharness.code_host.testing` in an installed wheel. |
| DEC-39 | `_run_gh` is a further call site found by search, not in the ticket's list, and not mapped as a seam. | Its operations are covered by inventory rows. |
| DEC-40 | The conformance section has no target size. | Comparable to Interfaces and Error model. |
| DEC-41 | B-ERR-2 asserts the error type and `write` flag, not retry-safety. | The model defines no idempotency promise. |
| DEC-42 | Removing an absent label is a no-op. | The Interfaces section names no error for it. |

## Inventory

### Terms used here

- **Direct gh user**: a function, class or script in `src/` or either `pr-comments.py` copy that builds a gh argv, runs gh,
  reads `GITHUB_TOKEN`/`GH_TOKEN`, or runs `gh_token_cmd`/`token_cmd`.
- **Owner**: the one capability interface an inventoried operation belongs to. Starting set: Session (Ses), Repository
  (Repo), ChangeRequests (CR), Discussion (Disc), Checks (Chk), WorkItems (WI).
- **Change request (CR)**: a pull request. Existing code keeps "PR". New names use "change request".
- Operation names are neutral ("list change requests", not "pr list"). The "today's gh call" column may quote gh.

### Audit method

Audited at commit `bb55976`, the branch base. The code inventoried is unchanged since `f293b99`, the commit the starting
list was drafted against. Run the greps from the repo root. Every hit must map to an inventory row, to "Not inventoried"
below, or to the "Non-call hits" list.

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
  parameter of `auto_approve_github.py` `list_own_open_prs`. That is why those patterns are listed.
- Paths below are under `src/dotharness/` unless stated. Abbreviations: `runners/` is `pr_review/runners/`.

### Table shape and phases

Columns, in order: row id, group, operation, today's gh call, file and function, owner capability, removing phase. The group
and phase are constant per table, so they appear once per table heading instead of per row. Each row has exactly one owner and
exactly one phase. A row touching two capabilities names one owner. Its justification is under "Owner justifications".

| Group | Tables | Removing phase |
| --- | --- | --- |
| `vibe-heal` | `INV-VH-*` | Phase 1 |
| `sisyphus` | `INV-SY-*` | Phase 2 |
| `pr-review` | `INV-PR-*` | Phase 3 |
| `leftovers` (shared helper `gh.py`, both `pr-comments.py` copies) | `INV-LO-*` | Phase 4 |

Check: the number of rows whose phase is not one of 1..4, or whose owner is not exactly one of Ses, Repo, CR, Disc, Chk, WI,
must be zero. A row moves between phases only by editing its table.

### Group `sisyphus` (Phase 2, migrated), `sisyphus/`

| Row id | Operation | Today's gh call | File and function | Owner | Status |
| --- | --- | --- | --- | --- | --- |
| INV-SY-01 | gh runner with `GH_TOKEN`, no `--repo`, cwd = dedicated clone | `gh_argv(*args)` with env `{"GH_TOKEN": token}` | `pick.py` `GhCli` (built in `cli.py` `default_services`) | Ses | migrated (Phase 2) |
| INV-SY-02 | remove label from work item | `issue edit N --remove-label L` (`LabelRemovalError`) | `pick.py` `GhLabelRemover.remove_label`; `cli.py` `GhTimeboundLabelRemover` (10 s subclass) | WI | migrated (Phase 2) |
| INV-SY-03 | list candidate work items | `issue list --state open --assignee A --label L --limit 100 --json ...` | `pick.py` `GitHubIssueTracker.list_candidates` | WI | migrated (Phase 2) |
| INV-SY-04 | find CRs that close an issue (reference lookup) | `issue view N --json closedByPullRequestsReferences`, then per reference `pr view R --json number,state` | `pick.py` `GhPullRequestLookup.linked_prs` | WI (see Owner justifications 5, R-INV-2) | migrated (Phase 2) |
| INV-SY-05 | find CRs that mention an issue (body scan) | repo-wide `pr list --state open --limit 200 --json body` | `pick.py` `GhPullRequestLookup.open_pr_bodies` (`limit=200`) | WI (see Owner justifications 5, R-INV-2) | migrated (Phase 2) |
| INV-SY-06 | find open CR for a branch | `pr list --head B --state open --json number,url` | `cli.py` `GhPullRequestClient.find_pr` | CR | migrated (Phase 2) |
| INV-SY-07 | read CR state (number, state, headRefOid) | `pr view <number or branch> --json number,state,headRefOid` | `cli.py` `GhPullRequestClient.pr_state`; consumed by `cleanup.py` `PrInfo` | CR | migrated (Phase 2) |
| INV-SY-08 | open a draft CR | `pr create --draft --head B --base BASE --title T --body B` (`GitHubError`) | `cli.py` `GhPullRequestClient.open_pr` | CR | migrated (Phase 2) |
| INV-SY-09 | CR state by number | `pr view N --json state`; `PR_STATE_TIMEOUT = 10.0`; `PickError` on empty | `cli.py` `GhPrStateLookup` | CR | migrated (Phase 2) |
| INV-SY-10 | read default branch | `repo view --json defaultBranchRef` | `cli.py` `default_services` (nested default-branch reader) | Repo | migrated (Phase 2) |
| INV-SY-11 | resolve token via command | `token_cmd`, `shlex.split`, no shell, `TOKEN_CMD_TIMEOUT = 30`, `ConfigError`, no default | `cli.py` `run_token_cmd` (called in `default_services`) | Ses | migrated (Phase 2) |
| INV-SY-12 | config field `[source.host].token_cmd` | parsed only, never executed | `sisyphus/config.py` `HostSource.token_cmd` | Ses (declares the INV-SY-11 input) | migrated (Phase 2) |

All twelve rows are migrated (Phase 2). Sisyphus no longer runs `gh` itself. The reference host runs it behind the
capability interfaces. The "Today's gh call" and "File and function" cells describe the code before Phase 2.

Context, not rows: the `sisyphus/orchestrator.py` `PullRequestClient` protocol is an existing seam, not itself a gh caller.
Its error surface (INV-SY-06..08) is `CodeHostError` (`GitHubError = CodeHostError`, REN-14). `cleanup.py` consumes `PrInfo` (number, state, headRefOid).
`prbody.py` `MAX_BODY_CHARS = 60000` is a host limit the contract must expose, not a gh call.

### Group `vibe-heal` (Phase 1, migrated), `vibe_heal/review/github.py` class `GitHubReviewClient`

Consumers: `vibe_heal/review/orchestrator.py`, `vibe_heal/cli.py`.

| Row id | Operation | Today's gh call | File and function | Owner | Status |
| --- | --- | --- | --- | --- | --- |
| INV-VH-01 | check host tool installed | `gh --version` (10 s) | `validate_installed` | Ses | migrated (Phase 1) |
| INV-VH-02 | read field of current-branch CR | `pr view --json FIELD` (30 s); `NoOpenPrError` | `_view_pr_field` | CR | migrated (Phase 1) |
| INV-VH-03 | post review with inline comments | `api --method POST /repos/OWNER_REPO/pulls/N/reviews --input -`; `_MAX_INLINE_COMMENTS = 50`, `_MAX_REVIEW_BODY_CHARS = 60_000`; 60 s | `post_review` | Disc | migrated (Phase 1) |
| INV-VH-04 | fallback top-level comment when review post fails | second `gh_argv` call (line ~204) | `post_review` fallback | Disc | migrated (Phase 1) |
| INV-VH-05 | resolve owner/repo | `repo view --json owner,name --jq ...` | `_get_owner_repo` | Repo | migrated (Phase 1) |
| INV-VH-06 | resolve owner/repo fallback | `git remote get-url origin` through `_run_gh`, parsed by `_parse_remote_url` | `_get_owner_repo` | Repo (the git primitive stays in git code, and the identity it yields becomes a RepositoryRef) | migrated (Phase 1) |
| INV-VH-07 | `_run_gh` runner adapter and error types | `GitHubReviewError`, `NoOpenPrError`, `GitHubTimeoutError` | `_run_gh` | Ses (transport, folded into the interface error taxonomy) | migrated (Phase 1) |

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
| INV-LO-06 | docs copy: same operations, `gh_paginated(endpoint)` | as INV-LO-02..05 | `docs/pr_review/scripts/pr-comments.py` (outside `src/dotharness/`. First-line note: "Historical: describes the pre-migration tools/pr-review (harness) tool. Not maintained.") | Disc (dominant operation, and the copy is not migrated) |

Both script copies live under `src/dotharness/scripts/` (413 lines) and `docs/pr_review/scripts/` (379 lines). The `src`
script is stdlib only and must not import dotharness (F-DUP-8 in `docs/design/shared-async-layer.md`). The two copies differ
(359 lines of `diff` output at the audited commit). This page RECORDS the divergence and does not reconcile it. The docs copy
is inventoried only, not migrated. Deleting it is filed in #196 (DEC-2).

Phase column rule:

- Phase 1 = every `INV-VH-*` row.
- Phase 2 = every `INV-SY-*` row.
- Phase 3 = every `INV-PR-*` row.
- Phase 4 = every `INV-LO-*` row, except INV-LO-06, whose removing phase reads "none: not migrated" (see Phase split).

INV-PR-46 and INV-PR-47 are Phase 3 here. If the Phase split assigns them to Phase 4,
edit the table, not the rule.

### Owner justifications

Stated once. Cited by row.

1. **Approve a CR (INV-PR-31): ChangeRequests.** An approval is a verdict on the CR bound to a head revision, not a
   conversation entry. It gates merge, which is also ChangeRequests (INV-PR-34). Discussion owns free text and thread content only.
2. **Mark file viewed, write and read state (INV-PR-30, INV-PR-29): ChangeRequests.** Viewed state is per-user review
   progress on the CR's changed files and is paired with approval in one gate (`auto_approve_github.py`). Both halves go to
   one owner so write and read cannot split. Discussion is rejected because viewed state has no comment body or thread.
3. **List reviews / has-user-approved (INV-PR-42, INV-PR-32): ChangeRequests,** the same owner as approve, so the check and
   the act share a capability.
4. **Commit-to-CRs lookup (INV-PR-35): ChangeRequests.** Input is a commit, output is CRs. No Repository state is changed or
   read beyond identity.
5. **CR-closing-issue lookup (INV-SY-04, INV-SY-05): WorkItems.** The question is asked from the issue's side ("which CRs
   close this issue"). The answer returns CR identifiers only, and WorkItems already owns issue state.
6. **Shared transport rows (INV-PR-07, INV-PR-19, INV-VH-07):** the owner names the capability whose operations dominate use.
   The transport itself disappears into the host implementation, so this is a classification for the removing-phase column only.
7. **Straddler notes:** INV-PR-09 (CR to issue hop) goes to WorkItems because the payload is the issue. INV-VH-06 goes to
   Repository because its output is repository identity. INV-PR-44 and INV-PR-45 go to Session because they carry the
   credential, not a repository operation. INV-PR-32 names ChangeRequests for both halves for the reason in item 3.

### R-INV-2: what "find the CRs that close an issue" does today

The actual behavior in `sisyphus/pick.py` is two different questions, not one:

- (a) a reference lookup: `issue view N --json closedByPullRequestsReferences` (INV-SY-04).
- (b) one `pr view` per returned reference, also INV-SY-04. Together they answer "CRs the host records as closing this
  issue".
- (c) a separate repo-wide `pr list --json body` scan (`open_pr_bodies`, INV-SY-05) that matches issue references in open CR
  bodies. It answers "open CRs whose text mentions this issue", which the host does not record. It is not a host-side relation.

The operations are named after these semantics, for example `closing_change_requests(work_item)` and
`open_change_request_bodies()`. The design does not collapse them into one operation. Interface sections must cite INV-SY-04
and INV-SY-05.

### Not inventoried

- Git code that stays OUT of the interface: the origin URL check in `repo_guard.py` (`git remote get-url origin`), and
  `git fetch origin refs/pull/{n}/head` in `pr_review/runners/auto_approve.py`.
- Not code-host calls: crontab/launchctl in `pr_review/scheduler.py`, the clipboard call in `spec_prism_flow/handoff.py`,
  `sisyphus/intake.py` `_git`, and claude/opencode launches (`ai_backend.py`).
- `sisyphus/config.py` states it "never executes token_cmd and never reads the environment". It is kept only as INV-SY-12.
- Linear (`LINEAR_API_URL` in `sisyphus/pick.py`, `LinearTracker`, `LINEAR_API_KEY` read in `sisyphus/cli.py`
  `default_services`) is another tracker, not gh, and is out of this inventory. Confirm the WorkItems section agrees.
- The other `shutil.which(...)` hits (the `backend_bin` and `pr_review` ones in `pr_review/cli.py`, plus `vibe_heal/ai_tools/*`,
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

- INV-PR-11 is `pr list` only (no `search prs`). `search prs` belongs to INV-PR-13.
- INV-PR-24 is `repo_has_workflows` (the "line ~317" caller), INV-PR-23 is `_read_default_branch`, INV-PR-25 is
  `_read_failed_log`, and INV-PR-26 is `_failed_step_from_api`.
- INV-PR-31 is `api --method POST .../reviews` (event APPROVE), not a separate review tool.
- INV-PR-32 splits into a `pr view` merge-state read (`_read_pr_state`) and an approval read through `fetch_all_pages`. It
  is not "a review read" alone.
- INV-SY-06 is `pr list --head` only. The `pr view` read is INV-SY-07.
- INV-SY-05 limit 200 is on `open_pr_bodies`. `linked_prs` has no limit.
- INV-PR-44 also covers `ci_fix.py`, `self_review.py`, `review_requested.py` and `address_comments.py`, and the three
  `local_review/` callers pass no token.
- Rows INV-PR-22, 27, 29, 31, 32, 42, INV-SY-03 and 06 were confirmed against the code at the audited commit.

## Phase split

Partition of the Inventory into four ordered phases. Row ids are those of the Inventory section. No row is restated here. The
rename aliases that Phase 4 removes are listed in the alias section of this page, not here. Issue numbers for Phases 2 to 4 are in DEC-5.

### Phase lists

Each inventory row is in exactly one list. Lists are given by row id (ranges are inclusive and contiguous at the audited
commit: 12 + 7 + 47 + 6 = 72 rows).

| Phase | Issue | Rows | Users (file and symbol) |
| --- | --- | --- | --- |
| 1 | #193 | `INV-VH-01` .. `INV-VH-07` | `vibe_heal/review/github.py` (`GitHubReviewClient`) |
| 2 | #194 | `INV-SY-01` .. `INV-SY-12` | `sisyphus/pick.py` (`GhCli`, `GhLabelRemover`, `GhPullRequestLookup`, candidate lister); `sisyphus/cli.py` (`GhPullRequestClient`, `GhPrStateLookup`, `run_token_cmd` and its env); `sisyphus/config.py` `HostSource.token_cmd` (INV-SY-12, same package as `run_token_cmd`) |
| 3 | #195 | `INV-PR-01` .. `INV-PR-47` | `pr_review/runners/*` (`common.py`, `ci_checks.py`, `ci_fix.py`, `auto_approve.py`, `auto_approve_github.py`, `merge.py`, `regret_review.py`, `address_comments.py`, `review_requested.py`, `self_review.py`, `focused_review.py`, `review_prs.py`), `pr_review/cli.py`, `pr_review/config.py` |
| 4 | #196 | `INV-LO-01` .. `INV-LO-05` | `gh.py` (`GH`, `gh_argv`, `build_subprocess_env`); `src/dotharness/scripts/pr-comments.py`; removal of the rename aliases; final grep check |
| none | filed in #196 (DEC-2) | `INV-LO-06` | `docs/pr_review/scripts/pr-comments.py`: inventoried only, not migrated or deleted. Its Inventory cell reads "none: not migrated". This is the single allowed exception to "one removing phase per row". |

INV-LO-06 sits in the `leftovers` table of the Inventory for grouping only. Its removing phase is "none", so it is excluded
from the four lists. Confirmed decisions:

- Linear is out of the inventory.
- INV-PR-46 and INV-PR-47 are Phase 3.
- INV-LO-06 is grouped with Phase 4 although it is never migrated.

Remaining direct gh users after Phase 2: the 7 vibe-heal rows (`INV-VH-01` .. `INV-VH-07`) and the 12 sisyphus rows
(`INV-SY-01` .. `INV-SY-12`) are migrated (marked in their tables). A search of `src/` finds `gh` users only in `gh.py`,
`pr_review/` and `scripts/pr-comments.py`. 53 of the 72 inventory rows are still direct users: 47 pr-review rows
(`INV-PR-*`, Phase 3) and 6 leftover rows (`INV-LO-*`, Phase 4; `INV-LO-06` is never migrated). `vibe_heal/review/github.py`
keeps the old names as aliases of the `code_host` errors until Phase 4 (R3).

Git code (`repo_guard.py` origin check, `git fetch origin refs/pull/{n}/head` in `auto_approve.py`) is in no list.

Not users, named so nobody adds them: `sisyphus/orchestrator.py` `PullRequestClient` (existing seam, a Consumer contract; Phase 2 kept it and the CLI adapter `HostPullRequestClient` implements it over the interface), `sisyphus/cleanup.py` (consumes `PrInfo`), and `vibe_heal/review/orchestrator.py`
and `vibe_heal/cli.py` (consume `GitHubReviewClient`, updated in Phase 1 with the client).

### Shared users

- `gh.py` (INV-LO-01): Phase 4. Every group above calls it. It can be deleted only after the last caller has moved, so
  earlier phases keep calling it and it is the last thing to go.
- `pr_review/runners/ci_checks.py` `gh` (INV-PR-07 transport): Phase 3, together with both of its other callers, `merge.py`
  (squash merge write path) and `ci_fix.py` (as `_gh`). All three sit in `pr_review/runners/`. Moving the helper alone would
  leave two callers on a deleted function.
- INV-PR-18 (runner side of the pr-comments child process, `common.py` `fetch_pr_comments`) is Phase 3. The script's own gh
  calls (INV-LO-02..05) are Phase 4. The script keeps working unchanged in between because the runners spawn it by path.

### Check method (reviewer-run, no code added)

1. Extract the id column of the Inventory tables and the ids of the four lists.
2. The sorted concatenation of the four lists has no duplicate.
3. The set equals the Inventory id set minus `INV-LO-06`, which is listed separately above.
4. For each id, the table it sits in (its removing phase) equals the list it appears in.

Phase 4 ends with the final grep check (the first three greps of the Audit method must return only non-call hits). A direct
user missed by step 3 still fails there.

### Package introduction (R-SPLIT-2)

Phase 1 (#193) creates `src/dotharness/code_host/` (interface, reference implementation, factory, fake) and migrates vibe-heal
(`INV-VH-*`). Phase 2 (#194) migrates sisyphus (`INV-SY-*`). See DEC-1.

### Alias removal (R-SPLIT-3)

Phase 4 removes the rename aliases. Any phase that renames what agents see must update the `KNOWLEDGE_SHA256` pins in
`tests/test_package_data.py` deliberately. Phase 0 edits none.

### `scripts/pr-comments.py` (R-SPLIT-4)

Constraint: the script is stdlib only and must not import `dotharness` (F-DUP-8, `docs/design/shared-async-layer.md`). It runs
as a child process through `PR_COMMENTS_SCRIPT_PATH`, used by `focused_review.py`, `address_comments.py` and
`regret_review.py`.

Fact (DEC-4): the script never runs the installed dotharness console entry point, so there is no command name to
define. Every child process it starts goes through its local `run()` helper (a wrapper over `subprocess.run`) and invokes only
`gh` (`gh pr view`, `gh api ... -X POST`, `gh pr edit --add-reviewer`) or `git` (`git rev-parse`). It does not use
`sys.executable` or `-m dotharness`, and calls none of the `[project.scripts]` console scripts (`vibe_heal`, `pr_review`,
`spec_prism_flow`, `sisyphus`). Checked by grep and by reading the `run(` call sites. The rest of the file was not read line
by line.

Consequence: its gh calls are INV-LO-02..05, owned by Phase 4. The earlier assumption that Phase 4 makes the script call a
dotharness entry point is withdrawn. How the script reaches the code-host interface (for example a new neutral entry point, or
receiving already-fetched data from the runners) is a Phase 4 design question for #196. Phase 0 does not define a command name.
Until then the script keeps its `run(argv)` seam so `patch.object(mod, "run", ...)` still works, and `parse_paginated` stays.

Rejected alternative, kept for Phase 4: the script receives already-fetched data. It works only if the three runners fetch
first, which moves the fetch into Phase 3 and changes the script's CLI.

Tests: `tests/test_pr_comments_script.py` passes unchanged through Phase 3. In Phase 4 only the argv asserted in the post-reply
test changes, and that edit is part of Phase 4.

### pr-review is one phase (R-SPLIT-5, DEC-3)

pr-review is not split: all of group `pr-review` is Phase 3. In particular `merge.py`, the only squash-merge write path, is not
its own phase. Reason: `ci_checks.gh` is shared by `merge.py` and `ci_fix.py`, so a split would need a temporary adapter that
Phase 0 does not design. Risk: Phase 3 is the largest phase (about fourteen modules). Fallback if reviewers find it too large:
runners versus `cli.py`/`config.py`, with `ci_checks.py`, `merge.py` and `ci_fix.py` together in the first part.

## Rename plan

Rename plan for `GitHub*`, `Gh*`, `gh_*`, `GH*`, `GITHUB_*` identifiers and configuration keys. It is the alias section that
the Phase split section refers to. Facts below were read from the code at commit `93fa633` (the audit grep counts are
unchanged since `f293b99`). The new names are proposals. They are placeholders until the phase that introduces them. Phase
numbers are those of the Phase split section:

- Phase 1 (#193) creates `src/dotharness/code_host/` and migrates `vibe_heal`.
- Phase 2 (#194) migrates `sisyphus`.
- Phase 3 (#195) migrates `pr_review`.
- Phase 4 (#196) removes `gh.py`, migrates both `pr-comments.py` copies' gh calls, and removes every alias.

Phase 0 changes no code, `pyproject.toml`, `src/`, `tests/`, `zensical.toml`, or `src/dotharness/knowledge/` file. Examples
and new names never name a third-party host. The "Old name" column quotes existing names.

### Naming rules

- **R1.** New class and function names say "change request", never "PR" (`NoOpenPrError` becomes
  `NoChangeRequestError`). A name for the reference implementation's tool appears only inside the reference implementation
  module and never as a shipped class name (`ReferenceCli` is rejected).
- **R2.** A rename introduced in phase N keeps the old name importable until Phase 4. An alias is a plain re-export
  (`OldName = NewName`) in the old module: no behavior change, no `DeprecationWarning` (a warning would change stderr and test
  output, DEC-7).
- **R3.** Removal condition for every alias, at Phase 4: `grep -rnE '\b<OldName>\b' src tests` finds no match outside the
  alias definition lines, and `uv run pytest` passes. Rows cite this rule by number and do not restate it.
- **R4.** The "Intro" column is the phase that introduces the new name (the phase of the first user of the old name, or Phase 1
  for names that live in the new package). A consumer stops using the old name in the phase of its own inventory group.
- **R5 (test patching).** Tests patch by module path (for example `patch(p + "get_gh_token")`,
  `patch(p + "build_subprocess_env")` in `tests/pr_review/runners/test_auto_approve.py`). An alias in the old module does not
  intercept a patch aimed at the new name. A phase that migrates a caller moves that caller's patch targets in the same
  change.

### Identifiers

"Alias" = keep the old name importable until Phase 4 (R2, R3). Ids `REN-nn` are never renumbered.

| Id | Old name | Where | New name | Intro | Alias / removal | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| REN-01 | module `dotharness.gh` | `src/dotharness/gh.py` | `dotharness.code_host.reference` (module path fixed by Phase 1) | 1 | Old module stays a re-export shim until Phase 4 (R3). INV-LO-01 | Package location is `src/dotharness/code_host/`. |
| REN-02 | `GH = "gh"` | `gh.py` | `REFERENCE_CLI = "gh"` in the reference module | 1 | Alias `GH` (R3) | The string `"gh"` is the executable name and does not change. The `GH` in `tests/sisyphus/test_pick.py` is a different object (REN-25). |
| REN-03 | `gh_argv(*args)` | `gh.py`; callers in `vibe_heal/review/github.py`, `sisyphus/pick.py`, `pr_review/runners/*` and tests (49 grep hits) | `reference_argv(*args)` (reference module only) | 1 (defined). Consumers stop in 1 (vibe-heal), 2 (sisyphus), 3 (pr-review) | Alias `gh_argv` (R3) | Returns `[REFERENCE_CLI, *args]`. Semantics unchanged. Consumers call capability interfaces and stop building argv. |
| REN-04 | `build_subprocess_env(path_prepend, env_vars, gh_token=None)` | `gh.py` | Function name KEEPS. Parameter `gh_token` becomes `host_token`. | 3 | During transition the function accepts both `gh_token=` and `host_token=`. If both are given, `host_token` wins. Removal of the `gh_token=` keyword: R3 on `gh_token=` | Sets `env["GITHUB_TOKEN"]` when the token is truthy (variable name: Child-agent environment). All `pr_review` runners pass it positionally. `local_review/review.py`, `local_review/address.py` and `local_review/git.py` pass no token. Only the parameter name changes. |
| REN-05 | local variable `gh_token` | `pr_review/runners/*.py` (`focused_review`, `ci_fix`, `self_review`, `auto_approve`, `review_requested`, `address_comments`, `review_prs`, `merge`) | `host_token` | 3 | none (local name, not importable) | Internal rename. |
| REN-06 | `get_gh_token(gh_token_cmd)` | `pr_review/runners/common.py` | `get_host_token(token_cmd)` | 3 | Alias `get_gh_token` (R3). R5 applies | Runs the command with `shell=True`. The default string `gh auth token` is not moved or changed (REN-31). Imported by 8 runner modules and patched in tests. |
| REN-07 | `PR_COMMENTS_SCRIPT_PATH` | `pr_review/runners/common.py`; used by `focused_review`, `address_comments`, `regret_review` | KEEP | n/a | n/a | It names a script path, not a host. The script `src/dotharness/scripts/pr-comments.py` is stdlib only, runs as a child process and must not import `dotharness` (F-DUP-8 in `docs/design/shared-async-layer.md`). "PR" stays per the glossary. How the script reaches the interface is a Phase 4 design question (see `scripts/pr-comments.py` in Phase split). This row does not decide it (DEC-8). |
| REN-08 | `gh_json`, `gh_json_paginated` | `scripts/pr-comments.py` (both copies) | KEEP | n/a | n/a | Script-local helpers, never imported by `dotharness` code (the file name is not a valid module name). The file must stay dependency-free. Their gh calls are INV-LO-02..06. |
| REN-09 | `ci_checks.gh` (`async def gh(args, env)`), imported as `_gh` by `merge.py` and `ci_fix.py` | `pr_review/runners/ci_checks.py` | `run_host_cli(args, env)` in `ci_checks.py`. Consumers move to the Checks capability | 3 | Alias `gh`; the `_gh` import alias is local to each importer and disappears when that importer is migrated (R3 covers `ci_checks.gh`) | 14 `_gh` grep hits. `merge.py`, `ci_fix.py` and `ci_checks.py` move together (R-SPLIT-5). |
| REN-10 | `GitHubReviewClient` | `vibe_heal/review/github.py`; used in `vibe_heal/review/orchestrator.py`, `vibe_heal/cli.py` and tests (55 hits) | `ChangeRequestReviewClient` (reference module); consumers depend on the ChangeRequests and Discussion interfaces | 1 | Alias `GitHubReviewClient` (R3) | `vibe_heal/review/github.py` becomes a shim under the same alias rule. |
| REN-11 | `GitHubReviewError` | `vibe_heal/review/github.py` | `CodeHostError` (in `code_host`. Error model, 6.1 note 1) | 1 | Alias `GitHubReviewError` (R3). Subclass relation preserved: the aliases of REN-12 and REN-13 stay subclasses, so `except GitHubReviewError` still catches them | Existing catches must keep working without edit. |
| REN-12 | `NoOpenPrError(GitHubReviewError)` | same | `NoChangeRequestError` | 1 | Alias `NoOpenPrError` (R3) | R1. Also re-exported from `vibe_heal/review/__init__.py` and imported by `vibe_heal/cli.py`. Both keep working through the alias. |
| REN-13 | `GitHubTimeoutError(GitHubReviewError)` | same | `CodeHostTimeoutError` | 1 | Alias `GitHubTimeoutError` (R3) | |
| REN-14 | `GitHubError` | `sisyphus/orchestrator.py`, imported by `sisyphus/cli.py` | `CodeHostError` (in `code_host`) | 2 | `GitHubError = CodeHostError` stays in `sisyphus/orchestrator.py` (it imports `code_host.errors` only) | Raised at the sisyphus orchestration boundary. The failure text is new (exit code and a short reason, D-11). |
| REN-15 | `GhCli` | `sisyphus/pick.py` (27 hits. Constructed as `GhCli(cwd, token, executor)`) | Removed. The reference host runs `gh` through its private `_Gh` context | 2 | None (D-8, no alias) | Env `GH_TOKEN` only. The host runs in `working_directory`. |
| REN-16 | `GhLabelRemover` | `sisyphus/pick.py` | `HostLabelRemover` in `sisyphus/pick.py` (WorkItems `remove_label`) | 2 | None (D-8, no alias) | The `LabelRemover` protocol stays. |
| REN-17 | `GhTimeboundLabelRemover(GhLabelRemover)` | `sisyphus/cli.py` | Removed. The declared `WorkItems.remove_label` timeout (10.0 s) replaces it | 2 | None (D-8, no alias) | The base class is gone too. |
| REN-18 | `GhPullRequestLookup` | `sisyphus/pick.py` | `HostPullRequestLookup` in `sisyphus/pick.py` | 2 | None (D-8, no alias) | Uses `WorkItems.closing_change_requests` and `open_change_request_bodies`. |
| REN-19 | `GhPullRequestClient` | `sisyphus/cli.py` | `HostPullRequestClient` in `sisyphus/cli.py` | 2 | None (D-8, no alias) | Uses `ChangeRequests.find_by_branch`, `create_draft` and `get`. |
| REN-20 | `GhPrStateLookup` | `sisyphus/cli.py` | Removed. `ChangeRequests.state` is used directly | 2 | None (D-8, no alias) | The 10.0 s limit is declared by the host. |
| REN-21 | `GitHubIssueTracker` | `sisyphus/pick.py`, `sisyphus/cli.py` | `HostIssueTracker` in `sisyphus/pick.py` (WorkItems `list_open`) | 2 | None (D-8, no alias) | The config value `kind = "github"` is not renamed (REN-33). |
| REN-22 | `GITHUB_OPEN_STATE = "OPEN"` | `sisyphus/pick.py` | Kept as `GITHUB_OPEN_STATE` in `sisyphus/pick.py` | 2 | None (D-8, no alias) | No alias. The constant stays local to the consumer. |
| REN-23 | `GITHUB_FOOTER_LINE` | `vibe_heal/cli.py` | KEEP | n/a | n/a | It is this project's own repository link, not a host dependency. Its text is user-visible output. |
| REN-24 | `GITHUB_SOURCE` | `tests/sisyphus/test_abandon.py` (TOML snippet with `kind = "github"`) | KEEP in Phase 0. The Phase 2 test edit may rename it | 2 (optional) | none (test constant, not importable by `src`) | Listed for completeness of the grep. |
| REN-25 | `GH` (test fixture `WorkItemSource(kind="github", ...)`) | `tests/sisyphus/test_pick.py` | KEEP in Phase 0. The Phase 2 test edit may rename it | 2 (optional) | none | Not the `GH` of `gh.py` (REN-02). The 13 `GH` grep hits are 1 definition in `gh.py`, 1 in `gh.py`'s `gh_argv`, and 11 in this test file. |
| REN-26 | `PickError`, `LabelRemovalError(PickError)` | `sisyphus/pick.py` | KEEP | n/a | n/a | They describe the operation, not a host. They do not match the grep patterns. I checked on purpose. |
| REN-27 | `MergeError`, `AutoApproveError`, `CiFixError` | `pr_review/runners/merge.py`, `auto_approve.py`, `ci_fix.py` | KEEP | n/a | n/a | Named for the command. Whether Phase 3 wraps `CodeHostError` into them is a Phase 3 design point (DEC-9). |
| REN-28 | `_IssueFetchError` | `pr_review/runners/common.py` | KEEP | n/a | n/a | Private (underscore), no host pattern, not importable by external code. |
| REN-29 | `gh_ops` | only the ledger row `spec_prism_flow.build.gh_ops` in `tests/data/ledger_modules.tsv` (path `tools/spec-prism-flow/spec_prism_flow/build/gh_ops.py`, not under `src/dotharness`) | KEEP | n/a | n/a | The ledger is frozen. |
| REN-30 | `GitHub` alone (about 151 hits) and the footer text | prose in docstrings, comments, user-facing text | KEEP | n/a | n/a | Not an identifier; no import or config contract depends on it. |

### Configuration keys and values

Keys are renamed only additively. "Gh" in the config key column is quoted from existing files.

| Id | Old key | File / section | New key | Precedence and behavior | Alias / removal |
| --- | --- | --- | --- | --- | --- |
| REN-31 | `gh_token_cmd` | `[harness]` of the `pr_review` config; field `gh_token_cmd: str = "gh auth token"` in `pr_review/config.py`; the template written by `pr_review/cli.py`; docs `docs/pr_review/configuration.md`, `commands/address-comments.md`, `commands/self-review.md`, `commands/local-review.md` | `host_token_cmd` (additive) | If only `gh_token_cmd` is set, behavior is exactly today's. If only `host_token_cmd` is set, its value is used with the same semantics. If both are set, `host_token_cmd` wins, the old key is ignored and no error is raised (DEC-10). Neither key set: the effective value is `gh auth token`, same as today. The command string runs with `shell=True` exactly as in `get_gh_token`. | Introduced in Phase 3. The old key is accepted until Phase 4. Removal: R3 on `gh_token_cmd`, docs mention only the new key. Users' config files that still use the old key break at Phase 4, so the Phase 4 release notes MUST say so. |
| REN-32 | `token_cmd` | `[source.host]` of the sisyphus config (`token_cmd: str \| None = None` in `sisyphus/config.py`, `run_token_cmd` in `sisyphus/cli.py`, removed in Phase 2: the reference host `Session.credential()` runs the command at startup) | KEEP, no new key | Unchanged: default `None`, split with `shlex.split`, no shell, no default command. The key is already neutral. | n/a |
| REN-33 | `kind = "github"` | `[source.work_item]` of the sisyphus config, `TRACKER_KINDS` in `sisyphus/config.py` | KEEP the value `"github"`. A neutral spelling would be additive and is chosen when the WorkItems capability is designed. | Existing configs must load unchanged. User-visible error messages (for example "mixed tracker") are unchanged. | n/a (DEC-11) |
| REN-34 | `[harness] env` passthrough (`config.harness.env`) | `pr_review/config.py` | KEEP | Used as `{**config.harness.env, "GITHUB_TOKEN": token}` in `focused_review.py`, `address_comments.py`, `review_requested.py`, `self_review.py` and `ci_fix.py`: the explicit variable overrides a same-named `env` entry. This order is preserved. | n/a |

Loader constraint (binds REN-32, REN-33 and any future sisyphus key): `src/dotharness/sisyphus/config.py` is stdlib only,
never executes `token_cmd` and never reads the environment. A neutral sisyphus key is a plain parsed value. Resolving it
(running a command, reading a variable) belongs to `sisyphus/cli.py` and Session, not the loader.

### Child-agent environment

Owned by Session. This section states the rule and the table only.

| Id | Variable | Set by | Decision |
| --- | --- | --- | --- |
| REN-35 | `GITHUB_TOKEN` | `gh.py` `build_subprocess_env` (when the token is truthy); hand-built dicts in `focused_review.py`, `address_comments.py`, `review_requested.py`, `self_review.py`, `ci_fix.py` (the last three read it back from `env`) | KEEP the name for the reference implementation. |
| REN-36 | `GH_TOKEN` | `sisyphus/pick.py` `GhCli` | KEEP the name for the reference implementation. |

Rule (DEC-6): with the default config the child process receives exactly the same variable names and values as today. A
neutral variable name is allowed only if behavior does not change. Session exposes the variable names as
data (a mapping returned for the reference implementation, containing `GITHUB_TOKEN` and `GH_TOKEN` as applicable per call
site). Hand-built env dicts migrate to that data in Phase 3. Knowledge files under `src/dotharness/knowledge/` that
mention `gh` or `GITHUB_TOKEN` are pinned by `KNOWLEDGE_SHA256` in `tests/test_package_data.py`. A phase that changes
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

Coverage check for reviewers: every name above maps to a row.

| Name | Row |
| --- | --- |
| `GitHub` | REN-30 |
| `GitHubReviewClient` | REN-10 |
| `gh_argv` | REN-03 |
| `GhCli` | REN-15 |
| `gh_token_cmd` | REN-31 (also the REN-06 parameter) |
| `gh_token` | REN-04, REN-05 |
| `GITHUB_TOKEN` | REN-35 |
| `GitHubReviewError` | REN-11 |
| `GitHubError` | REN-14 |
| `token_cmd` | REN-32 (and REN-31, REN-06) |
| `_gh` | REN-09 |
| `GH` | REN-02, REN-25 |
| `GitHubTimeoutError` | REN-13 |
| `GhPullRequestLookup` | REN-18 |
| `GhLabelRemover` | REN-16 |
| `GhPullRequestClient` | REN-19 |
| `GitHubIssueTracker` | REN-21 |
| `gh_json`, `gh_json_paginated` | REN-08 |
| `GITHUB_SOURCE` | REN-24 |
| `GITHUB_FOOTER_LINE` | REN-23 |
| `GH_TOKEN` | REN-36 |
| `GhTimeboundLabelRemover` | REN-17 |
| `GITHUB_OPEN_STATE` | REN-22 |
| `GhPrStateLookup` | REN-20 |
| `gh_ops` | REN-29 |

Companion queries for names the pattern cannot see: `grep -rnE "^class \w*Error" src` (REN-11..14, 26..28) and
`grep -rnE "PR_COMMENTS_SCRIPT_PATH|def get_gh_token|def gh\b|_IssueFetchError" src` (REN-06, 07, 09, 28).
`NoOpenPrError` (REN-12) is not matched by the pattern. It is found by the first companion query.

## Interfaces

Capability interfaces of the code host (A-2.1). Each is a `typing.Protocol` in the planned package
`src/dotharness/code_host/`. This section owns method names, parameters and which interface owns which inventory row. It does
not define model types (Models section), error types (Error model section) or limits, anchor rules and capability
declarations (Declarations section). It only names them. Those sections and the factory section are written by other
slices. Until they land, the names below are the contract the other sections must accept or rename here. The Inventory
section is the one place that assigns each operation to an owner. The "Rows" column below lists the ids so the coverage can be
checked, and does not restate the assignment.

### Rules for every interface

1. Every method is `async def`.
2. Every method returns a host-neutral model (Models section), a `Sequence` of models, `str`, `bool` or `None`. No method
   returns a `dict` from a host tool and there is no `api(...)`-style pass-through that takes a path or a query.
3. Pagination is an implementation detail. No parameter, return type or error in this section mentions a page, a cursor or a
   paginate flag. A method that lists returns the complete result up to the host's declared limit, or all of it where the
   declared limit is `None`. Where a caller has a bound today (`--limit 1000`, `limit=200`, the single-call `per_page=100` of
   `ci_checks.py`), the bound is a declared limit. The Declarations section owns it. Paged loops that read to exhaustion
   (`per_page=100` in `common.py`, `review_requested.py`) are `None`: `per_page` there is a page size, not a bound.
4. Repository-scoped methods take `repo: RepositoryRef` as the first argument after `self`. A `RepositoryRef` is host-defined
   and opaque: consumers never parse, split or compare its parts and never read a remote URL to build one.
5. Failures raise subclasses of `CodeHostError` (Error model section). A name used below is `CodeHostError` or one of
   `AuthenticationError`, `CodeHostTimeoutError`, `UnsupportedCapabilityError`, `ConfigurationError`,
   `NoChangeRequestError`. A method may also raise `OSError` when the host client is not installed or cannot be started
   (contract change of Phase 1: `for_current_branch` and `post_review` do so for an unavailable client; `Session.available()`
   never raises and reports the same fact as `False`). This section is the one place that says which method raises which type. The Error model section
   defines the types only. A confirmed absence is returned as `None` or an empty sequence where the signature says so (no
   not-found type exists, DEC-20). A consumer that needs exactly one change request for a branch (INV-VH-02)
   raises `NoChangeRequestError` when `find_by_branch` returns an empty sequence, or calls `for_current_branch`, which raises it itself.
6. Naming: new names use "change request". Existing code keeps "PR" until the phase that migrates it (Rename plan).

### Repository resolution: explicit and cwd-based

Two consumers resolve the repository differently today. `pr-review` passes an explicit identity to every call. `vibe-heal`
and `sisyphus` run in a clone and pass nothing. One method signature serves both because identity is a value, not an
ambient setting:

- `Repository.resolve(identity)` (explicit mode) turns the host-defined identity string the user supplied (for example the
  `--repo` value) into a `RepositoryRef`. The consumer passes the string through unchanged. Only the host interprets it.
- `Repository.current()` (cwd mode) returns the `RepositoryRef` of the clone the host was created for. No identity string
  appears in the call, and the consumer never reads a remote URL.
- Every other method takes the resulting `RepositoryRef`. A cwd-based consumer calls `current()` once and passes the result.
  An explicit consumer calls `resolve()` once per repository. The implementation of a cwd-mode ref may omit any repository
  argument from its host calls (this is what sisyphus does today).

The factory's return value exposes both modes: it returns the capability objects together with a `RepositoryRef` source that
supports `current()` and `resolve()` (factory section). A host that cannot do one mode raises `UnsupportedCapabilityError` from that
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
        """Resolve the token (configured command or default). Raises AuthenticationError, CodeHostTimeoutError."""
        ...

    async def current_user(self) -> str:
        """Login of the acting user. Raises AuthenticationError, CodeHostTimeoutError."""
        ...

    async def agent_environment_names(self) -> Sequence[str]:
        """Names of the variables a child agent process sees for this host, as data."""
        ...

    async def agent_environment(self, base: Mapping[str, str] | None = None) -> Mapping[str, str]:
        """base plus the credential variables, base entries first so credential variables win. Raises AuthenticationError."""
        ...
```

| Method | Replaces | Rows |
| --- | --- | --- |
| `available` | `gh --version`, `shutil.which("gh")` | INV-VH-01, INV-PR-46 |
| `credential` | `get_gh_token`, `run_token_cmd` | INV-PR-01, INV-SY-11, INV-SY-12 (config input) |
| `current_user` | `api user --jq .login`. Today fails open and returns `""`. Here it raises, and the consumer decides | INV-PR-03 |
| `agent_environment_names`, `agent_environment` | `build_subprocess_env`, hand-built env dicts, `GhCli` env | INV-LO-01, INV-PR-44, INV-PR-45, INV-SY-01, INV-PR-47 (config default) |
| (transport errors) | `_run_gh` error adapter | INV-VH-07 |

Environment (DEC-6): for the reference implementation, `agent_environment_names()` returns `("GITHUB_TOKEN", "GH_TOKEN")`.
`agent_environment()` sets the variables per call site exactly as today (Rename plan REN-35, REN-36). The names are data
returned by Session, never constants in consumers. Examples in docs write the value as `<token>`.

### Repository

```python
class Repository(Protocol):
    async def current(self) -> RepositoryRef:
        """cwd mode: the repository of the clone. Raises ConfigurationError when the clone has no usable repository identity."""
        ...

    async def resolve(self, identity: str) -> RepositoryRef:
        """explicit mode: host-defined identity string to a ref. Raises ConfigurationError when the identity cannot be resolved."""
        ...

    async def default_branch(self, repo: RepositoryRef) -> str:
        """Name of the default branch. Raises CodeHostError."""
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
        """Raises CodeHostError when the change request does not exist."""
        ...

    async def state(self, repo: RepositoryRef, number: int) -> ChangeRequestState:
        """Lifecycle state only (one narrow read). Raises CodeHostError when the change request does not exist."""
        ...

    async def find_by_branch(
        self, repo: RepositoryRef, branch: str, *, state: ChangeRequestState | None = ChangeRequestState.OPEN
    ) -> Sequence[ChangeRequest]:
        """Change requests whose head is `branch`. Empty when none.

        `state=None` means any state, most recent match first (interface version 2)."""
        ...

    async def for_current_branch(self, repo: RepositoryRef) -> ChangeRequest:
        """The open change request of the checked-out branch, read from the host client without a branch argument.

        Raises NoChangeRequestError when there is none, CodeHostError for any other failure, and OSError when the
        host client is unavailable.
        """
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

    async def create_draft(self, repo: RepositoryRef, *, head: str, base: str, title: str, body: str) -> ChangeRequest:
        """Raises CodeHostError (including a conflicting existing change request)."""
        ...

    async def squash_merge(self, repo: RepositoryRef, number: int, *, expected_head: str) -> None:
        """Squash-merge only if the head revision equals `expected_head`. Raises CodeHostError on mismatch."""
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
| `get` | `pr view` with chosen fields: state, base, head revision, description, line counts, requested reviewers, merge state | INV-SY-07, INV-VH-02, INV-PR-02, INV-PR-04, INV-PR-05, INV-PR-14, INV-PR-15, INV-PR-16, INV-PR-32 (merge state), INV-PR-33, INV-LO-02 |
| `state` | `pr view N --json state` (state only, no heavier fields) | INV-SY-09 |
| `find_by_branch` | `pr list --head` | INV-SY-06, INV-PR-21 |
| `for_current_branch` | `pr view --json ...` without a branch argument | INV-VH-02 |
| `list` | `pr list` with author, assignee, reviewer, state, and review-requested search | INV-PR-11, INV-PR-12, INV-PR-13, INV-PR-27, INV-PR-37, INV-PR-38, INV-PR-41, INV-PR-43 |
| `find_by_commit` | `commits/SHA/pulls` | INV-PR-35 |
| `commits` | `pr view --json commits` | INV-PR-28 |
| `reviews` | reviews listing, has-user-approved | INV-PR-07 (transport), INV-PR-32 (approval half), INV-PR-42 |
| `create_draft` | `pr create --draft` | INV-SY-08 |
| `squash_merge` | `pr merge --squash --match-head-commit` | INV-PR-34 |
| `request_reviewer` | `pr edit --add-reviewer` | INV-PR-06, INV-LO-05 |
| `approve` | POST review `event=APPROVE` with commit id | INV-PR-31 |
| `viewed_files`, `mark_viewed` | viewed-state query, `markFileAsViewed` | INV-PR-29, INV-PR-30 |

Notes:

- "By head" and "by branch" both mean the head branch name. No inventory row looks a change request up by head revision, so
  there is one lookup by branch name, `find_by_branch`. "Current branch" (INV-VH-02) is `for_current_branch`, the one
  ambient-branch method (Phase 1 contract change): the host client reads the checked-out branch itself, so the consumer
  passes no branch. It returns `ChangeRequest` (number, base reference and the other fields) or raises
  `NoChangeRequestError`.
- "Is the user's approval current" (INV-PR-32, INV-PR-42) is computed by the consumer from `reviews()` and the
  `ChangeRequest` head revision.
- The viewed-state owner (R-INV-3) was decided in the Inventory as ChangeRequests. Discussion does not repeat it.

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
        fallback_body: str | None = None,
    ) -> ReviewPostResult:
        """Post a review. If the host rejects the review or its anchors, post a top-level comment instead.

        The host owns this single fallback; the consumer does not catch and re-post, it reads `ReviewPostResult.fell_back`
        and `ReviewPostResult.reason`. The caller renders all text: `body`, the inline comments and `fallback_body`. The host
        applies only its declared size limits (it truncates the bodies with a notice and drops inline comments over the cap),
        the anchor rules and the fallback. The fallback comment is `fallback_body`, or `body` when that is None.
        The host never falls back after `CodeHostTimeoutError` (the review may already exist), `AuthenticationError` or
        `UnsupportedCapabilityError`; those propagate as raised. It falls back at most once: if the top-level post also
        fails, that error propagates (`CodeHostError`) and nothing else is posted.
        Size limits and anchor rules are declared (Declarations section), not parameters.
        May raise OSError when the host client is unavailable.
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
    async def list_checks(self, repo: RepositoryRef, number: int, *, required_only: bool = False) -> Sequence[Check]:
        """Checks of a change request, optionally only those the host requires."""
        ...

    async def list_workflows(self, repo: RepositoryRef) -> Sequence[Workflow]:
        """Empty when the repository has none."""
        ...

    async def failed_log(self, repo: RepositoryRef, check: Check) -> str:
        """Log of the failing part of `check`. Raises CodeHostError."""
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

Not every host supports work items. The capability is declared (Declarations section). A host without it raises
`UnsupportedCapabilityError` from every method here.

```python
class WorkItems(Protocol):
    async def get(self, repo: RepositoryRef, number: int) -> WorkItem | None:
        """Title and body. None when the work item is confirmed absent (deleted, transferred); raises CodeHostError
        (`transient=True`) only for an inconclusive failure."""
        ...

    async def list_open(
        self, repo: RepositoryRef, *, assignee: str | None = None, label: str | None = None
    ) -> Sequence[WorkItem]: ...

    async def remove_label(self, repo: RepositoryRef, number: int, label: str) -> None: ...

    async def closing_change_requests(self, repo: RepositoryRef, number: int) -> Sequence[ChangeRequest]:
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
| `open_change_request_bodies` | repo-wide body scan, R-INV-2 (c). Stays a separate operation | INV-SY-05 |
| `linked_work_items` | closing-issue references of a change request | INV-PR-09 |

Per R-INV-2 the two lookups are not merged: the first returns what the host records, the second returns text for the
consumer to match, which is not a host relation. The `limit=200` of the second is a declared limit (Declarations section).

### Row coverage

Every inventory row other than INV-LO-06 appears in exactly one Rows cell above or is named as transport or config. Check:
extract the `INV-` ids from the Rows cells of this section and compare with the Inventory id set minus INV-LO-06. The
difference must be empty. Rows listed under two interfaces (INV-PR-18, INV-LO-03, INV-PR-07, INV-PR-32) are shared reads whose
owner in the Inventory is the first interface named there.

## Models

Host-neutral value types that the capability interfaces (Interfaces section) accept and return (A-2.2). This section owns the
model names, fields, types and enums, and the mapping from every gh `--json` field (and every gh REST or GraphQL field the
inventory rows read) to exactly one model field or to "dropped". It does not define method signatures (Interfaces), the
inventory (Inventory), error types (Error model), or the anchor rules and limits (Declarations). Model names follow the
Interfaces section where it already names them (`CommitInfo`, `ReviewThread`, `Check`, `Workflow`, `Job`, `Credential`,
`ReviewPostResult`, `InlineComment`, `Anchor`).

### Conventions

- Every model is `@dataclass(frozen=True)` with full annotations. Collections are `tuple[...]`, optional values are
  `X | None`, and timestamps are timezone-aware `datetime`.
- No model carries a raw host payload or a `dict[str, Any]` escape hatch. No field name contains "pull request", `pr_`, a gh
  JSON key such as `headRefName`, or `owner`/`name` of a repository.
- Enum member values are lower-case host-neutral words. Every enum below is host-neutral: a host maps its own vocabulary onto
  these members and does not add members. gh values appear only in the mapping table.
- Fields marked "only on request" are `None` or an empty tuple when the operation that produced the model did not fetch them.
  The rule for each is stated under `ChangeRequest`. A consumer that needs one calls the interface method named there.
- Pagination, caching, serialization and write-side request types other than `InlineComment` are out of scope here.
- Examples write the credential as `<token>`.

### Repository identity

`RepositoryRef` is a single frozen dataclass with one opaque `value: str`. It is not subclassable by consumers. A host
chooses the string it stores and is the only reader of it. The reference implementation stores a string that names the
repository. No consumer reads, splits or builds it (the three forms observed today are: explicit `owner/name` as `--repo`,
`gh repo view --json owner,name` or the `git remote get-url origin` URL, and implicit from the cwd clone).

- Supported consumer operations: pass it to an interface method, compare with `==` or use it as a dict key or set member (it
  is hashable), and display it via `Repository.display_name(repo)` (Interfaces, DEC-13).
- Creation is host-defined: consumers receive a ref from `Repository.current()` or `Repository.resolve(identity)` (Interfaces,
  "Repository resolution"). How each of the three identity forms is turned into a ref is the Session and Repository
  sections' business, not this one.
- There is no `Repository` model: the default branch is `Repository.default_branch(repo)` (DEC-15), so `defaultBranchRef` does not map to a field.

### Models

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# Anchor (path, line, side) is defined by the Declarations section and imported here.


class ChangeRequestState(Enum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class MergeableState(Enum):
    MERGEABLE = "mergeable"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class ReviewDecision(Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REVIEW_REQUIRED = "review_required"


class ReviewState(Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    DISMISSED = "dismissed"
    PENDING = "pending"


class CheckStatus(Enum):
    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"
    SKIPPING = "skipping"
    CANCEL = "cancel"


class WorkItemState(Enum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class RepositoryRef:
    """Opaque, host-defined repository identity. Consumers pass it through, compare it and hash it; only the host reads value."""

    value: str


@dataclass(frozen=True)
class Credential:
    """Resolved host credential. The token is never shown by repr; examples write it as <token>."""

    token: str = field(repr=False)


@dataclass(frozen=True)
class User:
    login: str
    display_name: str | None = None
    is_bot: bool = False


@dataclass(frozen=True)
class CommitInfo:
    revision: str
    message: str | None
    authors: tuple[User, ...]
    committed_at: datetime | None


@dataclass(frozen=True)
class Comment:
    id: str
    author: User | None
    body: str
    created_at: datetime
    url: str | None
    is_hidden: bool = False
    diff_hunk: str | None = None


@dataclass(frozen=True)
class ReviewThread:
    id: str
    is_resolved: bool
    anchor: Anchor | None
    comments: tuple[Comment, ...]


@dataclass(frozen=True)
class InlineComment:
    body: str
    anchor: Anchor


@dataclass(frozen=True)
class Review:
    id: str
    author: User | None
    state: ReviewState
    body: str
    submitted_at: datetime | None
    revision: str | None
    comments: tuple[InlineComment, ...]


@dataclass(frozen=True)
class ReviewPostResult:
    fell_back: bool
    review: Review | None
    comment: Comment | None
    reason: str | None = (
        None  # set by the host only when fell_back is true: the rejection text, with secrets redacted by the host
    )


@dataclass(frozen=True)
class WorkItemRef:
    repository: RepositoryRef
    number: int


@dataclass(frozen=True)
class WorkItem:
    repository: RepositoryRef
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    assignees: tuple[User, ...]
    created_at: datetime
    state: WorkItemState


@dataclass(frozen=True)
class ChangeRequest:
    repository: RepositoryRef
    number: int
    title: str
    body: str
    url: str
    state: ChangeRequestState
    is_draft: bool
    author: User | None
    head_reference: str
    head_revision: str
    base_reference: str
    is_cross_repository: bool
    created_at: datetime
    mergeable: MergeableState | None = None
    auto_merge_enabled: bool = False
    review_decision: ReviewDecision | None = None
    requested_reviewers: tuple[User, ...] = ()
    additions: int | None = None
    deletions: int | None = None


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    details_url: str | None
    workflow_name: str | None
    started_at: datetime | None
    completed_at: datetime | None
    job_id: str | None


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str


@dataclass(frozen=True)
class Job:
    id: str
    failed_step: str | None
```

`Anchor` (path, line, side) and its host-declared rules are owned by the Declarations section. This section only uses the
name, so the fence does not import it.

Notes on the fields:

- `ChangeRequest`, only on request. Every operation of ChangeRequests (Interfaces) fills identity, title, body, url, state,
  draft flag, author, head and base reference, head revision, cross-repository flag and creation time. `mergeable`,
  `auto_merge_enabled`, `review_decision`, `requested_reviewers`, `additions`, and `deletions`: `get` fills them only. From
  `list`, `find_by_branch`, and `find_by_commit`, they are `None`, `False`, or empty. `ChangeRequest` has no
  commits or linked work items field. The consumer calls `ChangeRequests.commits()` and `WorkItems.linked_work_items()`
  (DEC-17).
- `ChangeRequest.mergeable` and `ReviewDecision` are `None` when the host has no such concept or did not compute it.
- `User.is_bot` exists because two consumers distinguish bot authors today (`auto_approve_github.py`, `scripts/pr-comments.py`).
- `CommitInfo.authors` is a tuple since one commit can have several authors. The commit-clock gate counts commits whose
  author list includes the change request author (`auto_approve_github.py`).
- `Comment.is_hidden` is the "minimized" state read by `address_comments.py` and `common.py`. `Comment.diff_hunk` is the code
  context printed by `scripts/pr-comments.py`. It is `None` for change-request-level comments.
- `Review.comments` is filled when a review is the input or result of `Discussion.post_review`, and empty from
  `ChangeRequests.reviews()`.
- `Check.job_id` is the opaque id `Checks.get_job` accepts. Today `ci_fix.py` extracts it from the check link with a regex.
  That URL parsing moves into the host.
- `WorkflowRun` is omitted. The audit found no consumer of workflow runs. `ci_fix.py` reads a log for a check (`failed_log`)
  and a job (`get_job`). `ci_checks.py` only asks whether the repository has workflows (`list_workflows`). No row lists or
  reads runs by id. `Workflow` carries only what `list_workflows` returns (DEC-14).
- Work item sources other than the gh issue tracker are not in the inventory and are not modeled here (Inventory, "Not
  inventoried").

### Mapping table: gh field to model field

Columns: gh field, where used, model, model field, notes. Sources are `pr`/`issue`/`repo`/`search` `--json` fields, then the
REST and GraphQL fields the inventory rows read. Paths are under `src/dotharness/`. `runners/` is `pr_review/runners/`.

Change request (`--json` on `pr view`, `pr list`, `search prs`):

| gh field | Where used | Model | Model field | Notes |
| --- | --- | --- | --- | --- |
| `number` | `runners/common.py`, `runners/ci_fix.py`, `runners/ci_checks.py`, `sisyphus/cli.py`, `sisyphus/pick.py`, `scripts/pr-comments.py` | `ChangeRequest` | `number` | |
| `title` | `runners/common.py` `get_pr_description`, `scripts/pr-comments.py` | `ChangeRequest` | `title` | the "## PR Description" formatting is consumer text, not a model field |
| `body` | `runners/common.py`, `sisyphus/pick.py` `open_pr_bodies` | `ChangeRequest` | `body` | `null` becomes `""` |
| `url` | `sisyphus/cli.py`, `runners/review_requested.py`, `scripts/pr-comments.py` | `ChangeRequest` | `url` | |
| `state` | `runners/common.py`, `runners/auto_approve_github.py`, `sisyphus/cli.py`, `sisyphus/cleanup.py` | `ChangeRequest` | `state` | `OPEN`/`CLOSED`/`MERGED` to `ChangeRequestState` members |
| `isDraft` | `runners/common.py`, `runners/auto_approve_github.py`, `runners/address_comments.py` | `ChangeRequest` | `is_draft` | |
| `author` | `runners/common.py`, `runners/ci_fix.py`, `runners/self_review.py`, `runners/review_requested.py` | `ChangeRequest` | `author` | `{login, ...}` to `User`. Absent author (deleted account) is `None` |
| `headRefName` | `runners/common.py`, `runners/ci_fix.py`, `scripts/pr-comments.py` | `ChangeRequest` | `head_reference` | |
| `headRefOid` | `runners/auto_approve_github.py`, `runners/common.py`, `sisyphus/cli.py` | `ChangeRequest` | `head_revision` | |
| `baseRefName` | `runners/common.py`, `runners/ci_checks.py`, `runners/auto_approve_github.py` | `ChangeRequest` | `base_reference` | |
| `isCrossRepository` | `runners/ci_fix.py`, `runners/auto_approve.py` | `ChangeRequest` | `is_cross_repository` | |
| `mergeable` | `runners/auto_approve_github.py` `_read_pr_state` | `ChangeRequest` | `mergeable` | `MERGEABLE`/`CONFLICTING`/`UNKNOWN` to `MergeableState`. Only from `get` |
| `autoMergeRequest` | `runners/auto_approve_github.py` `_read_pr_state` | `ChangeRequest` | `auto_merge_enabled` | non-null becomes `True`. The request object itself is dropped |
| `reviewDecision` | `scripts/pr-comments.py` | `ChangeRequest` | `review_decision` | `APPROVED`/`CHANGES_REQUESTED`/`REVIEW_REQUIRED` to `ReviewDecision`. `""` becomes `None` |
| `reviewRequests` | `runners/common.py` `get_requested_reviewers`, `scripts/pr-comments.py` | `ChangeRequest` | `requested_reviewers` | entries with a `login` become `User`. Team entries without a login are dropped as today |
| `reviews` | `scripts/pr-comments.py` | `Review` | (list returned by `ChangeRequests.reviews()`) | not a `ChangeRequest` field. Element fields are in the REST rows below |
| `additions` | `runners/merge.py` `_read_changed_lines` | `ChangeRequest` | `additions` | only from `get` |
| `deletions` | `runners/merge.py` `_read_changed_lines` | `ChangeRequest` | `deletions` | only from `get` |
| `commits` | `runners/auto_approve_github.py` `_fetch_pr_commits` | `CommitInfo` | (list returned by `ChangeRequests.commits()`) | element fields below |
| `commits[].authors` | same | `CommitInfo` | `authors` | each `{login}` to `User` |
| `commits[].committedDate` | same | `CommitInfo` | `committed_at` | ISO text to aware `datetime`. Unparsable is `None` |
| `commits[].oid` | same (implied by the list shape) | `CommitInfo` | `revision` | |
| `createdAt` | `runners/self_review.py`, `runners/review_requested.py`, `runners/common.py` | `ChangeRequest` | `created_at` | ISO text to aware `datetime` |
| `closingIssuesReferences` | `runners/self_review.py`, `runners/review_requested.py`, `runners/common.py` | `ChangeRequest` | (none) | not a `ChangeRequest` field. Each reference becomes a `WorkItemRef` returned by `WorkItems.linked_work_items()` |

Work item (`issue view`, `issue list`):

| gh field | Where used | Model | Model field | Notes |
| --- | --- | --- | --- | --- |
| `number` | `runners/common.py` `_fetch_issue`, `sisyphus/pick.py` | `WorkItem` | `number` | |
| `title` | `runners/common.py`, `sisyphus/pick.py` | `WorkItem` | `title` | |
| `body` | `runners/common.py` `_fetch_issue` | `WorkItem` | `body` | |
| `createdAt` | `sisyphus/pick.py` `list_candidates` | `WorkItem` | `created_at` | text to aware `datetime` |
| `assignees` | `sisyphus/pick.py` `list_candidates` | `WorkItem` | `assignees` | `{login}` to `User`. The consumer's filter by login stays a `list_open` parameter |
| `labels` | `sisyphus/pick.py` `list_candidates` | `WorkItem` | `labels` | `{name}` to `str` |
| `closedByPullRequestsReferences` | `sisyphus/pick.py` `GhPullRequestLookup.linked_prs` | (none) | dropped | first step of `WorkItems.closing_change_requests`. The host resolves each reference and returns full `ChangeRequest` models (`number`, `state` of the per-reference `pr view`) |

Check run (`_CHECK_FIELDS` in `runners/ci_checks.py`, `pr checks --json`):

| gh field | Where used | Model | Model field | Notes |
| --- | --- | --- | --- | --- |
| `name` | `runners/ci_checks.py` `_read_buckets`, `_read_root_required_names` | `Check` | `name` | |
| `bucket` | `runners/ci_checks.py` `_read_buckets` | `Check` | `status` | `pass`/`fail`/`pending`/`skipping`/`cancel` to `CheckStatus` members of the same name |
| `link` | `runners/ci_checks.py`, `runners/ci_fix.py` | `Check` | `details_url` | the job id the `/job/<id>` regex extracts from it is `Check.job_id`. Consumers no longer parse the URL |
| `workflow` | `runners/ci_checks.py`, `runners/ci_fix.py` | `Check` | `workflow_name` | |
| `startedAt` | `runners/ci_checks.py` | `Check` | `started_at` | text to aware `datetime` |
| `completedAt` | `runners/ci_checks.py` | `Check` | `completed_at` | text to aware `datetime` |

Repository (`repo view --json`):

| gh field | Where used | Model | Model field | Notes |
| --- | --- | --- | --- | --- |
| `owner` | `vibe_heal/review/github.py` `_get_owner_repo` | `RepositoryRef` | `value` | host-internal. Consumers never see owner or name |
| `name` | `vibe_heal/review/github.py` `_get_owner_repo` | `RepositoryRef` | `value` | host-internal, together with `owner` |
| `nameWithOwner` | `scripts/pr-comments.py` | `RepositoryRef` | `value` | host-internal. Never shown to consumers as a split string |
| `defaultBranchRef` | `runners/ci_checks.py`, `sisyphus/cli.py` | (none) | dropped | `Repository.default_branch(repo)` returns the branch name `str`. No repository model |

REST and GraphQL fields read by inventory rows (DEC-16: all rows needed are listed, rows added because the
`--json` list alone misses them):

| gh field | Where used | Model | Model field | Notes |
| --- | --- | --- | --- | --- |
| reviews `id` | `runners/auto_approve_github.py`, `scripts/pr-comments.py` | `Review` | `id` | numeric id to `str` |
| reviews `user.login` / `author.login` | `runners/review_requested.py`, `runners/auto_approve_github.py`, `scripts/pr-comments.py` | `Review` | `author` | to `User` |
| reviews `user.type` | `runners/auto_approve_github.py` | `User` | `is_bot` | `Bot` or login ending `[bot]` becomes `True` |
| reviews `state` | `runners/review_requested.py`, `runners/auto_approve_github.py`, `scripts/pr-comments.py` | `Review` | `state` | `APPROVED`/`CHANGES_REQUESTED`/`COMMENTED`/`DISMISSED`/`PENDING` to `ReviewState` |
| reviews `body` | `scripts/pr-comments.py` | `Review` | `body` | |
| reviews `submitted_at` | `runners/auto_approve_github.py` | `Review` | `submitted_at` | `None` while pending |
| reviews `commit_id` | `runners/auto_approve_github.py` (POST body also carries it) | `Review` | `revision` | the POST `commit_id` is `approve(head_revision=...)` |
| reviews `html_url` | `scripts/pr-comments.py` | `Comment` | `url` | review bodies are read as `Review`. `url` is dropped for reviews (no consumer outside the script's text output) |
| issue comments `id` | `scripts/pr-comments.py`, `runners/common.py` | `Comment` | `id` | numeric id to `str` |
| issue comments `user.login`, `user.type` | `runners/common.py`, `scripts/pr-comments.py` | `Comment` | `author` | `login` to `User.login`, `type == "Bot"` to `User.is_bot` |
| issue comments `body` | `runners/common.py`, `scripts/pr-comments.py` | `Comment` | `body` | |
| issue comments `created_at` | `runners/common.py` | `Comment` | `created_at` | text to aware `datetime` |
| issue comments `html_url` | `scripts/pr-comments.py` | `Comment` | `url` | |
| inline comments `id` | `scripts/pr-comments.py` | `Comment` | `id` | |
| inline comments `user.login`, `user.type` | `scripts/pr-comments.py` | `Comment` | `author` | as above |
| inline comments `path`, `line` / `original_line`, `side` | `scripts/pr-comments.py` | `Anchor` | (Declarations section) | `ReviewThread.anchor`. `line` falls back to `original_line` in the host |
| inline comments `body`, `html_url` | `scripts/pr-comments.py` | `Comment` | `body`, `url` | |
| inline comments `diff_hunk` | `scripts/pr-comments.py` | `Comment` | `diff_hunk` | |
| inline comments `in_reply_to_id` | `scripts/pr-comments.py` | `ReviewThread` | `comments` | dropped as a field: reply linkage is expressed by thread membership and order |
| GraphQL `reviewThreads.nodes.id` | `runners/common.py` | `ReviewThread` | `id` | the id `Discussion.reply` takes |
| GraphQL `reviewThreads.nodes.isResolved` | `runners/common.py`, `runners/address_comments.py` | `ReviewThread` | `is_resolved` | |
| GraphQL `comments.nodes.id` | `runners/common.py` | `Comment` | `id` | node id. One `id` per comment |
| GraphQL `comments.nodes.databaseId` | `runners/common.py`, `runners/address_comments.py` | (none) | dropped | second spelling of the comment id. The host keeps the mapping between its two id forms |
| GraphQL `comments.nodes.isMinimized` | `runners/common.py`, `runners/address_comments.py` | `Comment` | `is_hidden` | |
| GraphQL `pageInfo.endCursor`, `pageInfo.hasNextPage`, REST `per_page`, `page` | `runners/common.py`, `runners/auto_approve_github.py` | (none) | dropped | pagination is a host detail (Interfaces, rule 3) |
| GraphQL `files.nodes.path` | `runners/auto_approve_github.py` | (none) | dropped | key of the `Mapping[str, bool]` returned by `viewed_files` |
| GraphQL `files.nodes.viewerViewedState` | `runners/auto_approve_github.py` | (none) | dropped | value of the mapping. `VIEWED` becomes `True` |
| GraphQL `files.nodes.additions`, `deletions`, `changeType` | `runners/auto_approve_github.py` | (none) | dropped | requested by the query but unused. Line counts come from git numstat in `auto_approve_size.py` |
| GraphQL `pullRequest.id` | `runners/auto_approve_github.py` | (none) | dropped | host-internal node id used by `markFileAsViewed`. The host looks it up |
| jobs `steps[].name`, `steps[].conclusion` | `runners/ci_fix.py` `_failed_step_from_api` | `Job` | `failed_step` | name of the first step whose conclusion is `failure` |
| jobs id (from the check link) | `runners/ci_fix.py` | `Job` | `id` | |
| workflows `workflows[]` | `runners/ci_checks.py` `repo_has_workflows` | `Workflow` | `id`, `name` | |
| `api user` `login` | `runners/common.py` `get_current_user`, `scripts/pr-comments.py` | (none) | dropped | `Session.current_user()` returns the login as `str` |
| commit pulls (`commits/SHA/pulls`) `number`, `state`, `head`/`base` | `runners/regret_review.py` | `ChangeRequest` | `number`, `state`, ... | elements of `find_by_commit` |
| `search prs --json number` | `runners/common.py` `_list_review_requested_prs` | `ChangeRequest` | `number` | stub only. The host returns full models from `list(requested_reviewer=...)` |

### Coverage check

The set of gh `--json` field names in the first table group must equal the set found by this grep. Run it from the repo root.
Anything it finds that is not in a table is a missing row. A table row absent from the grep is a stale row:

```
grep -rnE '"--json"|_FIELDS|--json' src/dotharness --include=*.py | grep -v knowledge
```

The field lists in those hits are: `_PR_FIELDS` (ci_fix), `_CHECK_FIELDS` (ci_checks), and the quoted comma-separated lists at
each call. GraphQL query bodies are read from `GRAPHQL_QUERY`, `GRAPHQL_QUERY_REVIEW_THREAD_STATE` and `_FILES_QUERY`.

## Error model

The single exception hierarchy that every code-host implementation raises, and the mapping of the existing failure types and
conventions onto it (A-2.3, goal G3 of issue #192). Fact labels:

- [code @f293b99] read from the repository at commit `f293b99`.
- [ticket] from issue #192.
- [inferred] not verified.

Terms (Code host, Change request, Capability interface, Consumer, Session, Anchor, Conformance suite) are defined in the
Inventory terms and the Interfaces section and are not redefined here. This section adds two. An **old type** is an
exception class in the inventory table below, as it exists at `f293b99`. A **discriminator** is a documented attribute on
an error that lets a caller tell cases apart without a subclass. Which method raises which type is stated once, in the
Interfaces section (rule 5 and the method docstrings). This section defines types and mappings only. There is no code in
`src/dotharness/code_host/` in this phase. The fenced code is illustrative.

### Problem

Three failure conventions coexist in the gh-touching code [code @f293b99]:

1. Raise. `get_gh_token` (`pr_review/runners/common.py`) raises `RuntimeError`. `_fetch_issue` raises `_IssueFetchError`.
2. Return `None`, `[]`, `False` or `""` after logging. `is_pr_open` returns False on a failed lookup (fail-closed).
   `get_current_user` returns `""` (fail-open).
3. Fall back. `vibe_heal/review/github.py` catches `GitHubReviewError` from the inline review post and falls back to a plain
   post, except on `GitHubTimeoutError` (`except GitHubTimeoutError:` precedes `except GitHubReviewError as inline_err:`).

"Not authenticated" is not an exception type anywhere [ticket]. A bad token surfaces as `RuntimeError`, `GitHubError`,
`AutoApproveError` or a blank login. Ten unrelated types express overlapping failures, so a second host could not honor any of
them uniformly.

### Goals

- G3.1 Exactly one hierarchy, rooted at `CodeHostError`.
- G3.2 Every old type below maps to exactly one new type. The table has no unmapped row.
- G3.3 Three distinctions are expressible by type: no change request for the branch (`NoChangeRequestError`), timed out and
  the request may have taken effect (`CodeHostTimeoutError`), and not authenticated (`AuthenticationError`).
- G3.4 The timeout error carries a boolean `write`.
- G3.5 One unsupported-capability type exists, and every consumer call site names its policy for it (policy table below).
- G3.6 Per call site, the convention that is preserved is stated (call-site table below).
- G3.7 No message built by the design contains a token or the token command (message and secret rules below).

Non-goals:

- No code or edits outside this page.
- No behavior change (fail-open sites stay fail-open in their consumer).
- No raw `api(...)` pass-through and no raw `--json` dicts in error payloads.
- No login flow ("not authenticated" is a type to raise, not a flow to start).
- No retry or backoff policy beyond "do not retry a write timeout".
- No rate-limit-specific type.
- No Linear tracker error translation.
- No ledger entries and no nav entry.

### Old-type inventory and mapping

Every row is [code @f293b99].

| Old type | Defined in | Base | Meaning at f293b99 | New type |
|---|---|---|---|---|
| `GitHubReviewError` | `vibe_heal/review/github.py` | `Exception` | generic review-op failure. Caught to trigger fallback post | `CodeHostError` (or the specific subclass raised; note 1) |
| `NoOpenPrError` | `vibe_heal/review/github.py` | `GitHubReviewError` | no open PR for the current branch. Caught in `vibe_heal/cli.py` at two sites | `NoChangeRequestError` |
| `GitHubTimeoutError` | `vibe_heal/review/github.py` | `GitHubReviewError` | gh call timed out. Request may have been processed. Callers must not blindly retry | `CodeHostTimeoutError` (field `write: bool`) |
| `PickError` | `sisyphus/pick.py` | `Exception` | unusable pick setup or failed tracker call, including invalid JSON from gh | `CodeHostError` for host failures. `ConfigurationError` for unusable setup (note 2) |
| `LabelRemovalError` | `sisyphus/pick.py` (removed in Phase 2) | `PickError` | `gh issue edit --remove-label` failed (non-zero exit, timeout as 124) | `CodeHostError`, or `CodeHostTimeoutError(write=True)` for a timeout. The text is new (exit code and a short reason) |
| `GitHubError` | `sisyphus/orchestrator.py` | `Exception` | "unrecoverable failure (auth, network)" from a `PullRequestClient`. Maps to orchestrator condition C8 | `AuthenticationError` for auth. `CodeHostError` otherwise (note 3) |
| `MergeError` | `pr_review/runners/merge.py` | `RuntimeError` | failed `gh pr merge` call, and the aggregate "merge failed for: ..." raised after the loop | per call: `CodeHostError`. Aggregate stays consumer-local (note 4) |
| `AutoApproveError` | `pr_review/runners/auto_approve.py` | `RuntimeError` | PR list unreadable (e.g. bad token), and the aggregate raised after the loop | per call: `AuthenticationError` or `CodeHostError`. Aggregate stays consumer-local (note 4) |
| `CiFixError` | `pr_review/runners/ci_fix.py` | `RuntimeError` | aggregate "ci-fix failed for: ...". Also a `git push` failure | host part: `CodeHostError`. Aggregate and `git push` stay consumer-local (note 4) |
| `_IssueFetchError` | `pr_review/runners/common.py` | `Exception` | inconclusive issue lookup (rate limit, 5xx, network, malformed). Distinct from confirmed absence | `CodeHostError` with discriminator `transient: bool = True`. Confirmed absence is not an error (note 5) |

Notes:

1. `GitHubReviewError` is the base of `NoOpenPrError` and `GitHubTimeoutError`. In the new hierarchy the host's fallback inside
   `post_review` checks `CodeHostTimeoutError` first and then `CodeHostError`, so the ordering is preserved (row F1 below).
2. `PickError` mixes tracker failures and local setup problems. A Linear-tracker `PickError` is not a code-host failure: the
   Linear path (`LINEAR_API_URL` in `sisyphus/pick.py`) keeps a consumer-local error, and only code-host-sourced raises map to
   `CodeHostError` [inferred from `pick.py`, DEC-19].
3. `GitHubError` is raised by a `PullRequestClient` and caught at two sites in `sisyphus/orchestrator.py`, both mapping to C8.
   After the change the orchestrator catches `CodeHostError` there. Authentication and network failures both stay C8, so exit
   behavior is unchanged.
4. `MergeError`, `AutoApproveError` and `CiFixError` serve two purposes: one host call failed, and an aggregate "failed for:
   ..." summary raised after a loop so the CLI exits non-zero. The per-call failure maps to the hierarchy. The aggregate is a
   consumer concern. It keeps its name and base, and only its cause chain changes (`raise ... from <CodeHostError>`, DEC-9).
5. `_fetch_issue` returns `None` for a confirmed absence (`_ISSUE_NOT_FOUND_RE`) and raises for anything inconclusive. That
   split is kept: absence is returned as `None` (Interfaces), inconclusive failure raises `CodeHostError`. No `NotFoundError`
   type is introduced (DEC-20).

### Hierarchy

```python
from __future__ import annotations


class CodeHostError(Exception):
    """Root of every error a code-host implementation raises."""


class NoChangeRequestError(CodeHostError):
    """No open change request exists for the requested branch."""


class AuthenticationError(CodeHostError):
    """The session is not authenticated, or its credential was rejected.

    The message never contains the token or the token command (message and secret rules).
    """


class CodeHostTimeoutError(CodeHostError):
    """The call timed out; the host may have processed it. Do not retry blindly."""

    def __init__(self, message: str, *, write: bool) -> None:
        super().__init__(message)
        self.write = write


class UnsupportedCapabilityError(CodeHostError):
    """The host does not implement a capability interface or operation."""

    def __init__(self, message: str, *, capability: str) -> None:
        super().__init__(message)
        self.capability = capability


class ConfigurationError(CodeHostError):
    """The code-host setup is unusable (missing repository identity, bad option)."""
```

The `transient: bool = True` discriminator of the `_IssueFetchError` row is an attribute of a plain `CodeHostError` raised by
the issue lookup. It is documented here and not a separate class.

- R1. All six classes are importable from `dotharness.code_host`. Whether they live in a module `errors.py` is left to the
  implementation phase (#193).
- R2. Implementations raise only `CodeHostError` subclasses for host failures. They never let a raw subprocess, HTTP or parsing
  exception escape, `subprocess.TimeoutExpired` included, because it stringifies the command.
- R3. The three distinctions: (1) no change request for this branch is `NoChangeRequestError`. (2) Timed out and the request
  may have taken effect is `CodeHostTimeoutError`, with `write=True` for a write and `write=False` for a read. Consumers must
  not retry or fall back after a `write=True` timeout. A `write=False` timeout may be retried. (3) Not authenticated is
  `AuthenticationError`.
- R4. `UnsupportedCapabilityError.capability` holds the name of a capability interface (Session, Repository, ChangeRequests,
  Discussion, Checks, WorkItems, see Interfaces).
- R5. A subclass test in the conformance suite checks that each of the six types is a subclass of `CodeHostError`.

### Failure conventions per call site

Rule: default behavior does not change. A site that raised keeps raising. A site that returned a sentinel keeps returning it in
its consumer. A fallback keeps falling back. The implementation raises. The consumer wrapper converts to the old sentinel
where the old behavior was fail-open or fail-closed, so the interface stays raise-only.

| ID | Call site [code @f293b99] | Convention today | Preserved as |
|---|---|---|---|
| R-1 | `get_gh_token` (`pr_review/runners/common.py`) | raise `RuntimeError` on failed command or blank stdout | Raise. Not a code-host call: it resolves the credential before a Session exists. The Session reports a blank or failed token command as `AuthenticationError`, under the message and secret rules. |
| R-2 | `_fetch_issue` | raise `_IssueFetchError`. `None` on confirmed absence | Raise `CodeHostError`. `None` kept for absence (note 5). |
| R-3 | `vibe_heal/cli.py`, two `except NoOpenPrError` sites | raise, caught and reported | Raise `NoChangeRequestError`. Same handler. |
| R-4 | `sisyphus/orchestrator.py`, two `except GitHubError` sites | raise, mapped to C8 | Raise `CodeHostError`. Same C8 mapping. |
| R-5 | `sisyphus/cli.py`, two `except PickError` sites | raise, caught and reported | Raise. Handler catches `CodeHostError` plus the consumer-local pick error (note 2). |
| R-6 | `merge.py`, `auto_approve.py`, `ci_fix.py` loops | collect failures per PR, raise aggregate after loop | Same collection. Each collected item is a `CodeHostError` message. Aggregate stays consumer-local (note 4). |
| N-1 | `is_pr_open` | returns False after a failed lookup (fail-closed) | Consumer wrapper catches `CodeHostError` and returns False. The interface method raises. |
| N-2 | `get_current_user` | returns `""` after logging (fail-open) | Consumer wrapper catches `CodeHostError`, logs, returns `""`. Stays fail-open. An `AuthenticationError` is logged the same way (that log line is where a revoked token is visible today). |
| N-3 | `sisyphus/orchestrator.py` label removal wrapped in `except Exception` ("never alters the exit code or the records") | swallow and continue | Unchanged. Swallows `CodeHostError` like any other exception. |
| F1 | `vibe_heal/review/github.py` inline-review post with fallback | fall back to a plain post on `GitHubReviewError`, except `GitHubTimeoutError` | The host falls back once, inside `Discussion.post_review`, on a rejected review or anchors. It skips the fallback on `CodeHostTimeoutError` (the first request may have succeeded), `AuthenticationError` and `UnsupportedCapabilityError`, because a plain post would fail the same way [inferred; DEC-21]. The consumer does not catch and re-post. It reads `fell_back`. |

Other inventory call sites not listed here inherit the convention of their old code.

### Unsupported-capability policy

Each consumer declares a policy per call site:

- **degrade**: skip the feature silently and continue with reduced behavior
- **warn**: log a warning naming `capability`, then continue
- **stop**: re-raise and let the consumer's top-level handler exit non-zero

The reference implementation supports every capability, so no policy fires on it and default behavior is
unchanged. Initial policies [inferred; DEC-22]:

| Consumer | Call site | Capability | Policy |
|---|---|---|---|
| `vibe_heal` review | inline review comments with an Anchor | Discussion | degrade (the host posts a plain top-level comment and sets `fell_back`. The consumer does not re-post) |
| `vibe_heal` review | change-request lookup for the branch | ChangeRequests | stop |
| `pr_review` runners | merge, auto-approve, ci-fix | ChangeRequests / Checks | stop |
| `pr_review` runners | current-user lookup | Session | warn (keeps the blank-user fail-open result) |
| `sisyphus` | issue picking and label removal | WorkItems | stop for picking, warn for label removal |

### Message and secret rules

- No exception message, `__cause__` text, log line or `repr` raised by an implementation or by the conformance suite contains
  the token value or the token command string (secret rule).
- Evidence [code @f293b99]: `get_gh_token` builds its messages from the literal string `gh_token_cmd`, not from its value. It
  re-raises `subprocess.TimeoutExpired` as `RuntimeError(...) from None` because `TimeoutExpired` stringifies the command "and
  it may embed a credential". The same rule applies to every implementation: catch the transport exception and raise a
  hierarchy member with `from None`, or with a cause whose text has been checked.
- Host CLI stderr may be included in a message only after the implementation has confirmed it does not echo the credential
  [inferred]. The rule adopted: include the exit code and a short host-defined reason, and omit the raw command line.
- Examples on this page use `<token>` for any token value.
- Testable check for the Conformance section: a fake whose credential is the sentinel `<token>` raises each error type. The
  test asserts that neither the sentinel nor the token command string appears in `str(exc)`, `repr(exc)`, or
  `str(exc.__cause__)`.

## Declarations

What a host says about itself (A-2.4): which capability interfaces it implements, its size and time limits, and which inline
anchors it accepts. Each is a frozen value object that the host exposes and a consumer reads. This section owns these three
shapes and the `Anchor` value type. The Protocols that expose them are defined in the Interfaces section. The error raised
when a consumer uses an interface the host did not declare, and each consumer's policy for it, is defined in the Error model
section (`UnsupportedCapabilityError`, "Unsupported-capability policy"). The reference implementation (the existing `gh`
behavior) declares the constants that the code hard-codes today (see "Reference implementation values"). Fields and values are
data. Nothing here changes a constant.

Non-goals:

- No paging or cursor parameter.
- No timeout supplied by a consumer per call.
- No per-repository limit override.
- No runtime negotiation protocol.

### Capabilities

```python
from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    SESSION = "Session"
    REPOSITORY = "Repository"
    CHANGE_REQUESTS = "ChangeRequests"
    DISCUSSION = "Discussion"
    CHECKS = "Checks"
    WORK_ITEMS = "WorkItems"


@dataclass(frozen=True)
class HostDeclaration:
    capabilities: frozenset[Capability]
    limits: "HostLimits"
    anchors: "AnchorRules"
```

- The values of `Capability` are the interface names of the Interfaces section, verbatim (`Session`, `Repository`,
  `ChangeRequests`, `Discussion`, `Checks`, `WorkItems`). If the set of interfaces changes, this enum and the Interfaces
  section change together.
- Reader: a consumer reads `capabilities` before first use of an interface. The conformance suite reads it to run only the
  declared interfaces' tests. The reference implementation declares all six.
- The declaration is the single source of truth for support: a consumer never probes by calling and catching a failure. The
  Error model section owns what a consumer does on an undeclared interface.

### Limits

```python
@dataclass(frozen=True)
class OperationTimeouts:
    default_seconds: float
    per_operation: dict[str, float]


@dataclass(frozen=True)
class ListBounds:
    default: int
    per_operation: dict[str, int | None]  # None: unbounded, the host returns every item


@dataclass(frozen=True)
class HostLimits:
    max_inline_comments_per_review: int
    max_review_body_chars: int
    max_change_request_body_chars: int
    timeouts: OperationTimeouts
    list_bounds: ListBounds
```

- The keys of both `per_operation` maps are written `Interface.method`, using the Protocol and method names of the Interfaces
  section (for example `Discussion.post_review`). The Interfaces section's Rows tables map each method to its inventory rows,
  so a key traces to the Inventory. An operation absent from a map uses `default_seconds` or `default`.
- Readers:
  - `max_inline_comments_per_review` and `max_review_body_chars`: the review-posting consumer (the vibe-heal review path).
    It moves the overflow beyond the inline limit into the review body, and truncates the body with a notice.
  - `max_change_request_body_chars`: the consumer that builds a change request description (`sisyphus/prbody.py`).
  - `timeouts`: the host implementation, which applies them to its own calls. A consumer never passes a timeout.
  - `list_bounds`: the host implementation of every list-returning operation, which returns at most that many items. A bound of `None` means the host returns every item. A host never
    truncates silently: if it cannot return the whole result under the bound that applies, it raises `CodeHostError` instead.
- A list size bound is a declared limit, never a paging parameter. No operation takes a page, cursor or `--limit` argument and
  no caller passes one. Each declared bound replaces `--limit 1000` (`address_comments.py`), `--limit 500` (the four
  `@me` listings), the single-call `per_page=100` (`ci_checks.py`. The paged loops in `common.py` and `review_requested.py`
  declare `None`) and the `limit=100` / `limit=200`
  constructor defaults of `sisyphus/pick.py`. What the host does to honor a bound (several pages, one call) is its own
  concern.
- The caller-supplied `limit` of the sisyphus pick functions (`pick.py` lines 162 and 186, and `cli.py`) means "how many
  candidates to pick". It is a consumer parameter, distinct from the host bound, and stays with the consumer.

### Reference implementation values

The reference implementation declares exactly these values. The "Operation key" column is the `per_operation` entry (blank
means the default applies).

| Field | Value | Operation key | Source |
| --- | --- | --- | --- |
| `max_inline_comments_per_review` | 50 | | `_MAX_INLINE_COMMENTS` in `vibe_heal/review/github.py` |
| `max_review_body_chars` | 60 000 | | `_MAX_REVIEW_BODY_CHARS` in the same file (the host's real limit is about 65 536. Headroom kept) |
| `max_change_request_body_chars` | 60 000 | | `MAX_BODY_CHARS` in `sisyphus/prbody.py` |
| `timeouts.default_seconds` | 30 | | `TIMEOUT_GH` in `pr_review/runners/common.py` |
| timeout, CR state lookup (INV-SY-09) | 10.0 | `ChangeRequests.state` | `PR_STATE_TIMEOUT` in `sisyphus/cli.py` (removed in Phase 2) |
| timeout, label removal (INV-SY-02) | 10.0 | `WorkItems.remove_label` | `GhTimeboundLabelRemover` in `sisyphus/cli.py` (removed in Phase 2). A timeout is `CodeHostTimeoutError(write=True)` |
| timeout, installed check (INV-VH-01) | 10 | `Session.available` | `validate_installed` in `vibe_heal/review/github.py` |
| timeout, CR field read of the current branch (INV-VH-02) | 30 | `ChangeRequests.find_by_branch` | `_view_pr_field` in the same file. `ChangeRequests.for_current_branch` runs under the same 30 s |
| timeout, review post (INV-VH-03) | 60 | `Discussion.post_review` | `_POST_TIMEOUT_SECONDS` in the same file |
| `list_bounds.default` | 100 | | `per_page=100` in `ci_checks.py` (single call). Exhaustive reads are `None`, see below |
| list bound, CR listings | 1000 | `ChangeRequests.list` | `--limit 1000` in `address_comments.py` (covers the `--limit 500` listings, INV-PR-11, INV-PR-27, INV-PR-41) |
| list bound, work-item candidate listing (INV-SY-03) | 100 | `WorkItems.list_open` | `GitHubIssueTracker.__init__(limit=100)` in `sisyphus/pick.py` |
| list bound, open CR bodies (INV-SY-05) | 200 | `WorkItems.open_change_request_bodies` | `GhPullRequestLookup.__init__(limit=200)` in `sisyphus/pick.py` |
| list bound, exhaustive reads (INV-PR-07, INV-PR-17, INV-PR-29, INV-PR-39, INV-PR-40, INV-PR-42) | `None` | `Discussion.comments`, `Discussion.review_threads`, `ChangeRequests.reviews`, `ChangeRequests.commits`, `ChangeRequests.viewed_files` | `fetch_all_pages`, `--paginate` and cursor loops page to exhaustion today (safety gates read them in full) |
| list bound, workflows | 100 | `Checks.list_workflows` | `per_page=100` in `ci_checks.py` |

Phase 2 deviation from G1: the reference host applies these declared limits. The other sisyphus `gh` calls ran with a
3600 s limit before. They now run under the 30 s default. The two sisyphus listings use the host's own list bounds. The
fixed 100 and 200 are gone from the consumer.

The page contains no other values. Examples of other hosts use the placeholder `examplehost`, and a token, where one
appears, is written `<token>`.

### Anchor and anchor rules

An `Anchor` is the file and line position a consumer attaches an inline comment to. `AnchorRules` is the host's declaration of
which anchors it accepts.

```python
@dataclass(frozen=True)
class Anchor:
    path: str
    line: int
    side: str  # one of the host's declared `sides`


@dataclass(frozen=True)
class AnchorRules:
    inline_supported: bool
    sides: frozenset[str]
    requires_line_in_diff: bool
    supports_ranges: bool
    fallback: str = "top_level_comment"
```

- Reader: the review-posting consumer (the vibe-heal review path) consults the rules before posting, to decide which findings
  go inline and which go into the review body. The host's fallback below still covers rejections the rules did not predict.
- Fallback (decided, not reopened): when the host rejects an inline comment, the host posts a top-level comment carrying
  the same text and its location, once (single owner: the host, inside `post_review`). The host declares rules and performs the
  fallback. The consumer does not catch and re-post. `Discussion.post_review` reports
  which happened through `ReviewPostResult.fell_back`. `fallback` is informational and fixed to `"top_level_comment"`. A
  timeout is not a rejection and never triggers the fallback (the review may already exist. INV-VH-03).
- Reference implementation values (derived from `vibe_heal/review/github.py` `_collect_file_comments`, the
  `on_changed_line` filter in `line_filter.py`, and `tests/vibe_heal/review/test_github.py`):

| Field | Value | Evidence |
| --- | --- | --- |
| `inline_supported` | `True` | the review payload carries inline `comments` |
| `sides` | `frozenset({"RIGHT"})` | every inline comment is built with `"side": "RIGHT"`. No `LEFT` anywhere |
| `requires_line_in_diff` | `True` | findings with `on_changed_line=False` are listed in the body ("outside diff"), and the fallback test uses "line outside diff" as the rejection |
| `supports_ranges` | `False` | payloads carry `line` only, never `start_line` |
| `fallback` | `"top_level_comment"` | `post_review` fallback payload (INV-VH-04) |

## Factory and selection

How a tool gets a code host (ticket tasks 6, 7 and 10; overview D-9). One factory takes the selection and the options and
returns the capability interfaces of the Interfaces section. A consumer never imports an implementation. This section owns the
factory shape, the per-tool selection settings, the extension mechanism, the version contract and the "Reserved names" table.
The types it returns come from the Interfaces, Models, Declarations and Error model sections and are not restated.

### Factory

```python
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HostSelection:
    setting: str  # the setting that carried `name`, written as the user sees it (see "Selection per tool")
    name: str | None  # registered name or dotted path; None means the reference implementation
    options: Mapping[
        str, Any
    ]  # untyped mapping from the config surface; the implementation's options model validates it
    token_cmd: str | None  # the tool's existing token command setting, passed through unread
    working_directory: Path | None = None  # directory the host runs its client in; None means the process directory


@dataclass(frozen=True)
class CodeHost:
    session: "Session"
    repository: "Repository"  # offers both current() (cwd mode) and resolve() (explicit mode)
    change_requests: "ChangeRequests"
    discussion: "Discussion"
    checks: "Checks"
    work_items: "WorkItems"
    declaration: "HostDeclaration"


def create_code_host(selection: HostSelection) -> CodeHost: ...


@dataclass(frozen=True)
class HostRegistration:
    interface_version: int  # the CODE_HOST_INTERFACE_VERSION the implementation targets
    options_model: type  # called as options_model(**options); owned by the implementation
    factory: Callable[[Any, str | None], CodeHost]  # (validated options, token_cmd) -> CodeHost
```

- `create_code_host` is synchronous and does no host I/O. Building a `CodeHost` runs no command and makes no network call.
  Every host call happens later, through the capability objects.
- Both repository-resolution modes are exposed by the return value (`CodeHost.repository`), not by separate factories
  (Interfaces, "Repository resolution"). A cwd-mode consumer calls `repository.current()` once. An explicit-mode consumer
  calls `repository.resolve(identity)`.
- A capability the host did not declare (`declaration.capabilities`) is still present on `CodeHost`: the factory substitutes an
  object whose methods raise `UnsupportedCapabilityError(capability=<interface name>)`. Consumers read `declaration` first
  (Declarations, "Capabilities") and apply the Error model policy.
- `token_cmd` is passed through to the implementation and never executed by the factory or by a config loader.
- The reference implementation is the built-in registration named `reference`. It has an options model with no required field.

#### Rules

1. **Absent setting.** If `HostSelection.name` is `None`, the factory returns the reference implementation with empty options.
   An existing config file or environment with none of the new keys behaves exactly as today, because every migrated call
   site is served by the reference implementation (Inventory, Reference implementation values).
2. **Typed options.** Host-specific options belong to the implementation's `options_model`. The tool reads them as an untyped
   mapping from its config surface, passes them through and never interprets or validates them. The factory calls
   `options_model(**options)`. Any exception from it becomes `ConfigurationError` (rule 3). Options given with
   `name = None` are an error: the reference implementation takes none (a message that names the options setting).
3. **Fail at startup.** An unknown name, an entry point that cannot be loaded, an invalid dotted path, an options model that
   rejects the options, a duplicate entry-point name or a version mismatch raises `ConfigurationError` from
   `create_code_host`. The tool calls the factory at startup, before its first host call, prints the message and exits
   non-zero. The message names the setting and its value and the reason, in the form
   `code host setting <setting> = "<value>": <reason>`. It never contains the token, `token_cmd` or any option value: the
   reason for an options failure lists field names and error kinds only, and the factory builds messages itself instead of
   passing the exception text of the options model or of an import error through (Error model, "Message and secret rules").
   `__cause__` is cleared with `from None`.

   Implementation detail (N-3): the options-error message is built from a fixed reason text, for example
   "options are not valid". It carries no option value, so an option that holds a secret never reaches the message,
   the log or `__cause__`.

### Selection per tool

The exact name and location of each selection is fixed in the "Reserved names" table. This table says how each surface reads
it.

| Tool | Where the selection lives | Options | How it is read |
| --- | --- | --- | --- |
| pr-review | TOML key `host` in table `[harness]` of `.harness.toml`, next to `gh_token_cmd` / `host_token_cmd` (Rename plan REN-31). `src/dotharness/pr_review/config.py`. | Table `[harness.host_options]` | Plain string (or absent), validated as a string at load time. The factory validates its value at startup. |
| sisyphus | TOML key `name` in table `[source.host]` of `.sisyphus.toml`, next to `token_cmd` (Rename plan REN-32). `src/dotharness/sisyphus/config.py`. | Table `[source.host.options]` | Plain string. The loader is stdlib only, never executes `token_cmd` and never reads the environment: it checks only that the value is a non-empty string and that `options` is a table. Resolving the name belongs to the factory. |
| vibe-heal | Settings field `code_host` plus environment variable `DOTHARNESS_CODE_HOST` on `VibeHealConfig(BaseSettings)` (`vibe_heal/config/models.py`). | Field `code_host_options` (a JSON object), environment variable `DOTHARNESS_CODE_HOST_OPTIONS` | vibe-heal has no TOML file. `VibeHealConfig` uses `env_prefix=""`, `case_sensitive=False`, `extra="ignore"` and the env files `.env.vibeheal` and `.env`. With an empty prefix a field named `code_host` would read a bare `CODE_HOST`, so the environment name carries the `DOTHARNESS_` prefix explicitly (overview D-5). The field declares `validation_alias="DOTHARNESS_CODE_HOST"` (and the options field `"DOTHARNESS_CODE_HOST_OPTIONS"`), so a bare `CODE_HOST` is not read. |
| spec-prism-flow | No host key today (overview D-9). | n/a | If a key is added later it is validated strictly at startup (an unknown or unloadable value stops the tool with the rule 3 message). sisyphus reads the same `.spec-prism-flow.toml`. It ignores a host key placed there, so a spec-prism-flow key never selects the sisyphus host. |

Relation of `[source.work_item] kind` to the sisyphus host key (DEC-32): the new key is
`name` under `[source.host]` and `kind` is unchanged. `kind` stays validated against `TRACKER_KINDS`, `kind = "github"` still
conflicts with `pick_state` as today, and the value is not renamed (Rename plan REN-33, DEC-11). Meaning: `kind = "github"`
selects the selected host's `WorkItems` capability. Any other kind selects its own tracker, and the host still serves change
requests. If the selected host does not declare `WorkItems` and `kind = "github"`, sisyphus stops at startup with a
`ConfigurationError` that names `kind` and its value. No value of `name` changes how `kind` is validated.

### Extension mechanism

A third-party implementation is installed as a Python distribution and selected by name or by dotted path. Examples use the
placeholder `examplehost`.

- **Entry-point group.** `dotharness.code_hosts` (reserved. See "Reserved names"). The entry-point name is the host name used in
  the selection. A name is lowercase letters, digits, `_` and `-`, starting with a letter, and never contains `:`.
- **Entry-point value.** The value names one object: a `HostRegistration` (Factory), that is the factory callable together with
  its typed options model and the interface version it targets. Example in the distribution's own packaging metadata (not
  part of this repository. `pyproject.toml` is not edited here):

  ```toml
  [project.entry-points."dotharness.code_hosts"]
  examplehost = "examplehost_pkg.host:REGISTRATION"
  ```

- **Dotted path.** `package.module:attribute`. The part before `:` is imported with `importlib.import_module`. The part after
  is an attribute name, optionally dotted (`Holder.REGISTRATION`), and must resolve to a `HostRegistration`. Both parts are
  required and non-empty. A dotted path needs no installed distribution, so a project may point at a local module on
  `sys.path`.
- **Resolution order.** (1) A value containing `:` is a dotted path and is never looked up in the registry. (2) Otherwise the
  value `reference` selects the built-in implementation. An entry point named `reference` is ignored. (3) Otherwise the value is
  looked up in the entry-point group. Because registered names never contain `:`, a registered name and a dotted path cannot
  both match one value. If an entry point is nonetheless registered under a name containing `:`, it is ignored, so the dotted
  path wins (precedence: dotted path, then built-in, then entry point).
- **Duplicate names.** If two entry points in the group share the selected name, the factory loads neither and raises
  `ConfigurationError` that names the setting, the value and the two distribution names. Duplicates that nobody selects are
  not an error and are not loaded. The factory never picks one by install order.
- **Load failure.** Import errors, a missing attribute, a value that is not a `HostRegistration`, or an exception raised while
  loading an entry point are `ConfigurationError` under rule 3. Entry points are loaded only for the selected name. No other
  registered host is imported.
- **Version.** The registration's `interface_version` is checked before `options_model` or `factory` is called (Version
  and stability contract).

### Version and stability contract

- **Expression.** An integer constant `CODE_HOST_INTERFACE_VERSION`, defined in `src/dotharness/code_host/version.py` and
  re-exported as `dotharness.code_host.CODE_HOST_INTERFACE_VERSION`. The first release of the interface is `1`. Version `2` (Phase 2) adds `HostSelection.working_directory` (the registration factory takes it as third argument) and lets the `state` parameter of `ChangeRequests.find_by_branch` be `None`. An integer is
  used because a plugin either matches or does not. There is no minor-version negotiation.
- **What counts as breaking** (the constant is incremented):
  - Removing or renaming a Protocol, a method, a model field, an enum member, or a `Capability` value
  - Adding a required parameter to a method
  - Adding a method to an existing Protocol (a plugin that does not have it would fail)
  - Changing a parameter or return type incompatibly
  - Changing which `CodeHostError` subclass a method raises for a documented condition, or the meaning of a declared limit
  - Moving an error type out of the `CodeHostError` hierarchy
- **Not breaking** (the constant is unchanged):
  - Adding a new capability interface that hosts may leave undeclared
  - Adding an optional parameter with a default
  - Adding a model field with a default
  - Adding an enum member (consumers treat an unknown member as the safe fallback)
  - Adding a subclass of an existing error
  - Adding a declaration field with a default
- **What an implementation declares.** Its `HostRegistration.interface_version` is the one integer it was written against. It
  is exact: a host works with the dotharness release that provides the same integer.
- **Mismatch.** If `interface_version` differs from `CODE_HOST_INTERFACE_VERSION`, `create_code_host` raises
  `ConfigurationError` under rule 3: `code host setting <setting> = "<value>": implementation targets interface version <n>,
  this dotharness provides <m>`. Neither side is guessed to be newer. The message gives both numbers. A registration whose
  `interface_version` is not an `int` is also a load failure.
- **Aliases and the deprecation window (Rename plan, task 8).** Backward-compatible aliases for the old `GitHub*` / `Gh*`
  names and the `gh_token_cmd` key are added in the migrating phase of each tool (Phase 1 vibe-heal, Phase 2 sisyphus,
  Phase 3 pr-review) and removed in Phase 4 (#196). The window is therefore "until Phase 4", counted in phases and not in
  interface versions. The aliases are not part of the interface and are not covered by `CODE_HOST_INTERFACE_VERSION`. No
  `DeprecationWarning` is emitted (DEC-7). Phase 4 release notes say that `gh_token_cmd` stops working (REN-31).
- The conformance suite (Declarations and Error model readers) runs against `CODE_HOST_INTERFACE_VERSION` and the fake
  registers itself with that value.

### Reserved names

Reserved by #192 so that later phases and third parties do not collide. Nothing is added to `pyproject.toml`, `src/` or any
config model by #192. The group name appears in this design only. Phase numbers are those of the Phase split section.

| # | Name | Exact string | Where | Status |
| --- | --- | --- | --- | --- |
| 1 | Entry-point group | `dotharness.code_hosts` | Python entry-point group name, read by the factory | reserved by #192, introduced by Phase 1 |
| 2 | pr-review TOML key and table | key `host` in table `[harness]` | `.harness.toml` | reserved by #192, introduced by Phase 3 |
| 3 | sisyphus TOML key and table | key `name` in table `[source.host]` | `.sisyphus.toml` | reserved by #192, introduced by Phase 2 |
| 4 | vibe-heal settings field | `code_host` (options: `code_host_options`) | `VibeHealConfig` in `vibe_heal/config/models.py` | reserved by #192, introduced by Phase 1 |
| 5 | vibe-heal environment variable | `DOTHARNESS_CODE_HOST` (options: `DOTHARNESS_CODE_HOST_OPTIONS`) | process environment, `.env.vibeheal`, `.env` | reserved by #192, introduced by Phase 1 |
| 6 | Interface-version constant | `CODE_HOST_INTERFACE_VERSION` | `src/dotharness/code_host/version.py`, re-exported from `dotharness.code_host` | reserved by #192, introduced by Phase 1 |
| 7 | Typed-options table in each TOML file | `[harness.host_options]` in `.harness.toml`, `[source.host.options]` in `.sisyphus.toml` | nested under the keys of rows 2 and 3 | reserved by #192, introduced by Phase 3 (`.harness.toml`) and Phase 2 (`.sisyphus.toml`) |

The reference built-in name `reference` is reserved in the same way (entry points of that name are ignored). The Rename plan's
`host_token_cmd` (REN-31) is introduced by Phase 3 and is not a selection key.

## Conformance suite and in-memory fake

How an implementation author checks a code host, and how a consumer test runs without one (A-3-2; ticket task 9, goal G5). The
suite and the fake ship in one module of the wheel. This section cites interfaces (Interfaces), models (Models), errors (Error
model), declarations (Declarations) and the factory (Factory and selection) by section name and does not restate them. Names
below that are not in those sections (`CodeHostConformance`, `InMemoryCodeHost` and their members) are owned here. Phase 0 added
no code and no test. Phase 1 (#193) implements the module and the packaging test.

Non-goals:

- No live-host run in CI (it needs network and credentials. An author may run the suite against the reference
  implementation by choice, but it is not run by default).
- No second host implementation and no login flow.
- No raw host dicts or `api(...)` pass-through in the suite or fake interfaces.
- No migration of existing tests or seams in Phase 0.
- No property-based, fuzz, or performance tests.
- No record and replay of real host traffic.
- No pytest plugin entry point in `pyproject.toml`.

Examples use the placeholder host `examplehost` and the token value `<token>`.

### Module, packaging and run mechanism

- **Module.** `src/dotharness/code_host/testing.py`, importable as `dotharness.code_host.testing`. One file holds the suite and
  the fake. If it later outgrows one file it becomes the package `code_host/testing/` with the same import path (DEC-37).
- **Imports.** Standard library and `dotharness.code_host` modules only. `pytest` is not imported by the module. Verified in
  `pyproject.toml` (read only): `pytest`, `pytest-asyncio` and `pytest-mock` are in the `dev` dependency group, not in the
  runtime dependencies, so a pytest import would break `import dotharness.code_host.testing` in an installed wheel (DEC-38).
- **Run mechanism: base class.** `class CodeHostConformance` holds the test methods as `async def test_...` coroutines. The
  author subclasses it in their own test file and overrides one hook. Whatever runner collects the subclass runs the tests. No
  plugin, fixture registration, or conftest wiring belongs to this module. (The fixture alternative ties the shipped module to
  pytest at import time and is rejected.)
- **The one hook.** `async def make_target(self) -> ConformanceTarget` returns a fresh, empty host for each test:

```python
from dataclasses import dataclass
from typing import Protocol


class HostSeeder(Protocol):
    """Test-side handle that puts state into, and injects failures into, one host.

    `InMemoryCodeHost` is its own seeder (see "In-memory fake"). A real implementation supplies a seeder that drives a sandbox.
    Methods are the seeding and injection methods named in "In-memory fake" (add_repository ... fail_always)."""


@dataclass(frozen=True)
class ConformanceTarget:
    host: "CodeHost"  # what create_code_host returns (Factory)
    seeder: HostSeeder
```

- **Usage** (three lines. `examplehost` and `<token>` are placeholders):

```python
class TestExampleHost(CodeHostConformance):
    async def make_target(self) -> ConformanceTarget:
        host = ExampleHost(token="<token>")
        return ConformanceTarget(host=host.code_host(), seeder=host.seeder())
```

- **Opting out of a capability.** An implementation that lacks a capability leaves it out of `declaration.capabilities`
  (Declarations, "Capabilities"). The suite reads the declaration, skips nothing silently, and for each undeclared capability
  runs the B-ERR-4 test instead of that capability's group. A group is never skipped because a test fails.
- **Suite validated by the fake.** `InMemoryCodeHost` must pass every test the suite runs against it: the groups of the capabilities it declares (Session, Repository, ChangeRequests, Discussion; see the review-flow subset below) and B-ERR-4 for the two it does not (Checks, WorkItems). A declared capability's group exercises only the methods that group names, so a declared-but-partial capability is not asked to implement methods outside it. That is a requirement, and a Phase 1 test
  (`TestInMemoryCodeHost(CodeHostConformance)` in `tests/code_host/test_in_memory.py`) enforces it. The reference host passes the same suite in `tests/code_host/test_reference_conformance.py` with a stubbed runner. It is how the fake stays honest.
- **Packaging check (NF-9).** A test in the style of `tests/test_wheel_install.py` (build the wheel, install it into a clean
  venv outside the repo, then exercise it) or `tests/test_package_data.py` confirms the module ships. The assertion: in the clean
  venv, `import dotharness.code_host.testing` succeeds, and `dotharness/code_host/testing.py` is in the wheel's file list.
  Phase 1 adds the test (`tests/test_wheel_install.py`, `test_code_host_testing_module_ships_and_imports`), together with the module.
- **NF-8.** Phase 0 edits no knowledge file. `KNOWLEDGE_SHA256` in `tests/test_package_data.py` (27 entries) is untouched.

### Behaviors asserted

Types and error names come from the sections cited above. Each behavior has a stable id. Where the capability tables differ
(DEC-36), Interfaces wins:

- ChangeRequests has no generic "update" or "close" (it has `create_draft`, `squash_merge`, `request_reviewer`, `approve`,
  `mark_viewed`).
- WorkItems has `remove_label` but no add-label.
- "not-found" is an empty result or `None` where the signature says so, not an error (DEC-20).
- `find_by_branch` returns an empty sequence, and `NoChangeRequestError` is raised by the consumer that needs exactly one
  change request (Interfaces rule 5).

**Cross-capability (B-ERR).** Operation names are `Interface.method`, as in the Declarations reference-values table.

| ID | Statement |
| --- | --- |
| B-ERR-1 | `ChangeRequests.find_by_branch` for a branch with no change request returns an empty sequence: not `None`, not an error. The seeder can make a host call raise `NoChangeRequestError` (injection), and the test asserts it is a `CodeHostError` and distinct from the other four types. |
| B-ERR-2 | A write whose outcome is unknown raises `CodeHostTimeoutError` with `write=True`. A read timeout raises it with `write=False`. The test asserts the two `write` values differ, for at least `ChangeRequests.create_draft`, `Discussion.post_comment` and `ChangeRequests.get`. |
| B-ERR-3 | An operation without valid credentials raises `AuthenticationError`, a different type from the B-ERR-1 and B-ERR-2 errors. |
| B-ERR-4 | For each capability absent from `declaration.capabilities`, every method of that interface (taken from `CodeHost`, Factory) raises `UnsupportedCapabilityError` whose `capability` equals the interface name. Not `AttributeError`, not a stub result. |
| B-ERR-5 | No error raised by the host carries a token value: with a credential `<token>` and a token command string, `str(exc)`, `repr(exc)` and `str(exc.__cause__)` of every error type from B-ERR-1..4 contain neither (Error model, "Message and secret rules"). |
| B-ERR-6 | Each of the six error types is a subclass of `CodeHostError` (Error model R5). |

B-ERR-2 asserts the error type and the `write` flag only. It does not assert that the write is retry-safe, because the Error
model says consumers must not retry after `write=True` and defines no idempotency promise (DEC-41).

**Per capability.**

| ID | Statement |
| --- | --- |
| B-SES-1 | `available()` returns a `bool` and does not raise, with and without credentials. |
| B-SES-2 | `current_user()` returns the login the seeder configured. Without credentials it raises `AuthenticationError`. |
| B-SES-3 | `credential()` returns a `Credential`. Its `repr` does not contain the token (Models: `token` is `repr=False`). |
| B-SES-4 | `agent_environment_names()` returns the names as data. `agent_environment(base)` contains every base entry and each credential variable, and a credential variable wins over a base entry of the same name. |
| B-REP-1 | `resolve(identity)` for a seeded identity returns a `RepositoryRef`. For an unknown identity it raises `ConfigurationError`. |
| B-REP-2 | A `RepositoryRef` is opaque: passing the ref from `resolve()` to another method works without the test parsing it, and two `resolve()` calls for one identity return equal refs. |
| B-REP-3 | `current()` returns a ref for the repository the target was created for (`ConfigurationError` when the target has none). `default_branch(ref)` returns the seeded name. `display_name(ref)` returns a non-empty `str`. |
| B-CR-1 | `get(repo, n)` returns the seeded `ChangeRequest`. For a missing number it raises `CodeHostError`. |
| B-CR-2 | `find_by_branch` returns the change requests whose `head_reference` equals the branch, honoring `state` (default open). |
| B-CR-3 | `list` returns seeded change requests in insertion order, filtered by `state`, `author`, `assignee` and `requested_reviewer` when given. |
| B-CR-4 | `find_by_commit`, `commits` (oldest first) and `reviews` return what was seeded. A change request with none returns an empty sequence. A fake seeded with more items than the default list bound returns all of `reviews` and `commits` (their declared bound is `None`). |
| B-CR-5 | `create_draft` returns a `ChangeRequest` with `is_draft` true and visible to a following `get` and `find_by_branch`. Creating a second one for the same head and base raises `CodeHostError`. |
| B-CR-6 | `squash_merge(expected_head=...)` with the current head revision makes the state merged. With a different revision it raises `CodeHostError` and changes nothing. |
| B-CR-7 | `request_reviewer` adds the login to `requested_reviewers`. `approve(head_revision=...)` appears in `reviews()` with an approved state. |
| B-CR-8 | `mark_viewed` is reflected in `viewed_files` for that path. Marking twice leaves it viewed. |
| B-DIS-1 | `post_comment` returns the new `Comment` and it appears in `comments()`. Order is posting order. |
| B-DIS-2 | `review_threads` returns seeded threads with `is_resolved` and `anchor`. A thread whose anchor no longer maps to the diff is returned with `anchor=None`, not dropped. `comments` and `review_threads` return every seeded item even above the default list bound (declared `None`). |
| B-DIS-3 | `reply(thread_id, body)` appends a `Comment` to that thread. An unknown `thread_id` raises `CodeHostError`. |
| B-DIS-4 | `post_review` with anchors the host's `AnchorRules` accept returns `fell_back=False` and the inline comments appear in `review_threads`. With an anchor the rules reject (a side not in `sides`, or a line outside the diff when `requires_line_in_diff`) it returns `fell_back=True` and the body is a top-level comment. |
| B-DIS-5 | A `CodeHostTimeoutError` with `write=True` from `post_review` propagates and the body is not posted a second time (no fallback; Declarations, "Anchor and anchor rules"). |
| B-CHK-1 | `list_checks` returns seeded checks. `required_only=True` returns only the required ones. A change request with no checks returns an empty sequence, which is not an error. |
| B-CHK-2 | `list_workflows` returns seeded workflows, empty when none. |
| B-CHK-3 | `failed_log(check)` returns the seeded log text. `get_job(job_id)` returns the `Job` with its `failed_step`. An unknown id raises `CodeHostError`. |
| B-WI-1 | `get` returns the seeded `WorkItem`. A missing number returns `None` (confirmed absence, Error model note 5), not an error. |
| B-WI-2 | `list_open` returns open work items in insertion (tracker) order, filtered by `assignee` and `label`. Closed ones are excluded. The result respects the declared list bound. |
| B-WI-3 | `remove_label` removes the label. Removing an absent label is a no-op (DEC-42). |
| B-WI-4 | `closing_change_requests` returns the change requests the seeder linked. `linked_work_items` is the reverse relation. With no link both are empty. |
| B-WI-5 | `open_change_request_bodies` returns open change requests with their descriptions, separate from B-WI-4 (Interfaces: the two lookups are not merged). |

Conformance groups built in Phase 2: B-SES-3, B-REP-3, B-CR-1, B-CR-2, B-CR-5 and B-WI-1 to B-WI-5, run against the reference
host (with a stubbed `gh`) and the fake. B-CR-3/4/6/7/8, B-DIS-1..3 and the Checks groups stay for Phase 3.

**Review flow (Phase 1 extension).** These ids extend the table above for the vibe-heal review flow. They are implemented by
`CodeHostConformance` in `dotharness.code_host.testing`. The seeder handle is `HostSeeder` (`set_available`,
`set_current_change_request`, `reject_inline_reviews`, `reject_all_posts`, `fail_posts_with`, `posts`, `post_attempts`).
`make_target` returns a `ConformanceTarget(host, seeder, repository)`.

| ID | Statement |
| --- | --- |
| B-NCR-1 | `Session.available()` returns a `bool` and does not raise, available or not. |
| B-NCR-2 | `ChangeRequests.for_current_branch(repo)` returns the `ChangeRequest` with the seeded number and base reference. |
| B-NCR-3 | With no change request it raises `NoChangeRequestError` (a `CodeHostError`) whose text is "No open pull request found for the current branch". |
| B-NCR-4 | An unavailable host client raises `OSError` from `for_current_branch` and `post_review`, and nothing is posted. |
| B-REV-1 | An accepted `post_review` returns `fell_back=False` and `reason=None`, and posts the body and inline comments as given. |
| B-REV-2 | A rejected review falls back to one top-level comment: `fell_back=True`, `reason` holds the rejection text. With no `fallback_body` the comment is `body`. |
| B-REV-3 | With a `fallback_body`, the fallback comment is that text. |
| B-REV-4 | `reason` is set if and only if `fell_back` is true. |
| B-REV-5 | `CodeHostTimeoutError`, `AuthenticationError` and `UnsupportedCapabilityError` propagate with exactly one attempt and no fallback. |
| B-REV-6 | When the fallback is also rejected, a `CodeHostError` propagates after exactly two attempts (one fallback only). |
| B-LIM-1 | The declaration holds 50 inline comments and 60 000 body characters. |
| B-LIM-2 | A review posts at most the declared number of inline comments. |
| B-LIM-3 | A body or fallback body above the declared limit is cut to the limit and ends with a truncation notice. |
| B-SEC-1 | With the sentinel `<token>`, no exception message, `__cause__` chain, log record, `repr` (host, declaration, results) or `ReviewPostResult.reason` contains it, across the failure scenarios above. |

B-ERR-4 and B-ERR-6 are run by the same class: every method of each undeclared capability raises
`UnsupportedCapabilityError(capability=<interface name>)`, and the error types are `CodeHostError` subclasses.

`InMemoryCodeHost` in Phase 1 implements the review-flow subset: Session (`available`, `current_user`), Repository, ChangeRequests
(`for_current_branch`, `find_by_branch`) and Discussion (`post_review`). It declares the limits and anchor
rules of the reference host. Its declaration lists Session, Repository, ChangeRequests and Discussion; Checks and WorkItems are
undeclared. Methods of a declared capability outside the review-flow subset also raise `UnsupportedCapabilityError`, and no suite
group calls them (B-ERR-4 covers only undeclared capabilities). The B-NCR/B-REV/B-LIM/B-SEC ids above are the groups
the fake passes; the per-method groups for the full interfaces are the design target, not a Phase 1 requirement for the fake. The seeding and
injection methods of the table in "In-memory fake" below are the full design target. The review-flow seeder methods above are
what exists. A test can set the current repository, the current-branch change request (or none), availability, a rejection
(which forces the fallback and its reason) and a post failure (a timeout, for example), and reads the recorded posts.

**Factory and version (B-FAC).** Run once per module by `CodeHostConformance` against `create_code_host` with the implementation
registered, using only standard-library patching of entry-point discovery.

| ID | Statement |
| --- | --- |
| B-FAC-1 | The implementation's `HostRegistration.interface_version` equals `CODE_HOST_INTERFACE_VERSION`. A registration with another integer makes `create_code_host` raise `ConfigurationError` naming both numbers. |
| B-FAC-2 | Options the `options_model` rejects raise `ConfigurationError` whose message lists field names only (no option value). |
| B-FAC-3 | Two entry points sharing the selected name, an unloadable entry point and an invalid dotted path each raise `ConfigurationError` in the form `code host setting <setting> = "<value>": <reason>`. |
| B-FAC-4 | `create_code_host` makes no host call: the seeder records zero calls after it returns. |

### In-memory fake

One class, `InMemoryCodeHost`, in `code_host/testing.py`. It implements all six capability Protocols and serves as its own
`HostSeeder`. Constructor (all keyword-only):
`InMemoryCodeHost(*, repository: str = "example/repo", user: str = "fake-user", token: str = "<token>", capabilities: frozenset[Capability] | None = None, declaration: HostDeclaration | None = None)`.
The default `capabilities` is every capability except Checks (Phase 2 does not build Checks). Passing Checks raises `ConfigurationError`.
`capabilities` selects the declared subset (this is how B-ERR-4 is exercised). An undeclared interface raises
`UnsupportedCapabilityError(capability=<name>)` from each method, as the factory substitutes (Factory, "Factory"). The fake
registers itself as `FAKE_REGISTRATION` (a `HostRegistration` with `interface_version=CODE_HOST_INTERFACE_VERSION`), selectable by
the dotted path `dotharness.code_host.testing:FAKE_REGISTRATION`. `code_host()` returns the `CodeHost` bundle for the instance.

**Determinism.** No network, no clock, no subprocess, no environment variable read. Timestamps come from a counter the fake owns
(`datetime` values increase by one second per created object from a fixed epoch). Ordering is insertion order unless the
Interfaces section defines otherwise (`commits` oldest first. WorkItems in insertion order, which stands for tracker order).

**Seeding.** Builder methods. They do not go through the capability interfaces and take no host dicts. Arguments use the
Models types. Each returns the created model so a test can use it as an expected value.

| Method | Arguments | Returns |
| --- | --- | --- |
| `add_repository` | `identity: str, *, default_branch: str = "main", is_current: bool = False` | `RepositoryRef` |
| `add_change_request` | `repo, *, head_reference: str, base_reference: str = "main", title: str = "", body: str = "", state: ChangeRequestState = OPEN, is_draft: bool = False, author: str = "fake-user", head_revision: str \| None = None, number: int \| None = None, requested_reviewers: Sequence[str] = ()` | `ChangeRequest` |
| `add_commit` | `repo, number, *, revision: str, message: str \| None = None, authors: Sequence[str] = ()` | `CommitInfo` |
| `add_review` | `repo, number, *, author: str, state: ReviewState, revision: str \| None = None, body: str = ""` | `Review` |
| `add_comment` | `repo, number, *, author: str, body: str` | `Comment` |
| `add_thread` | `repo, number, *, anchor: Anchor \| None, comments: Sequence[Comment], is_resolved: bool = False` | `ReviewThread` |
| `add_check` | `repo, number, *, name: str, status: CheckStatus, required: bool = False, job_id: str \| None = None` | `Check` |
| `add_workflow` | `repo, *, name: str` | `Workflow` |
| `add_job` | `repo, *, job_id: str, failed_step: str \| None = None, log: str = ""` | `Job` |
| `add_work_item` | `repo, *, title: str, body: str = "", labels: Sequence[str] = (), assignees: Sequence[str] = (), state: WorkItemState = OPEN, number: int \| None = None` | `WorkItem` |
| `link_closing` | `repo, *, work_item: int, change_request: int` | `None` |
| `set_user` | `login: str` | `None` |

Built so far (Phases 1 to 2): `capabilities=`, `add_work_item`, `add_change_request`, `link_closing`, `set_default_branch`,
`set_user`, `fail_next`, `fail_always`, `clear_failures`, `calls`, `writes`, read-only `work_items` and `change_requests`, and
`FAKE_REGISTRATION` (with `FakeOptions(repository, user)`). The fake implements Session `credential`, Repository
`default_branch`, ChangeRequests `get`, `state`, `find_by_branch` and `create_draft`, and all six WorkItems methods. The other
seeders (`add_repository`, `add_commit`, `add_review`, `add_comment`, `add_thread`, `add_check`, `add_workflow`, `add_job`) and the
`comments`, `threads`, `reviews` and `checks` views stay for Phase 3. `set_default_branch` is global, not per repository.

`number=None` assigns the next number for that repository. `add_repository(..., is_current=True)` makes it the answer of
`Repository.current()`. None marked makes `current()` raise `ConfigurationError`.

**Error injection.** Two methods cover every kind. `operation` is `Interface.method`, or `"*"` for all operations.

| Method | Meaning |
| --- | --- |
| `fail_next(operation, error, *, apply: bool = False)` | The next call to `operation` raises `error` (a `CodeHostError` instance), then behavior returns to normal. |
| `fail_always(operation, error, *, apply: bool = False)` | Every call to `operation` raises `error` until `clear_failures()`. |
| `clear_failures()` | Removes all injected failures. |

`apply=True` is allowed only for write operations: the fake applies the write to its state and then raises, so a consumer test
can cover "timed out but took effect". `apply=False` raises without applying, covering "timed out and did not take effect".
Both outcomes are needed for B-ERR-2. Each kind is injected by passing an instance:

| Kind | Injection |
| --- | --- |
| No change request (B-ERR-1) | `fail_next("ChangeRequests.find_by_branch", NoChangeRequestError("..."))` for consumer handlers. The empty-result case needs no injection |
| Timeout, write may have taken effect (B-ERR-2) | `fail_next("ChangeRequests.squash_merge", CodeHostTimeoutError("...", write=True), apply=True)` or `apply=False` |
| Timeout, read | `fail_next("ChangeRequests.get", CodeHostTimeoutError("...", write=False))` |
| Not authenticated (B-ERR-3) | `fail_always("*", AuthenticationError("..."))` |
| Unsupported capability (B-ERR-4) | construct with `capabilities=frozenset(Capability) - {Capability.CHECKS}` |

**Inspection.** `calls: list[Call]` records every capability call in order (`Call` is a frozen dataclass of `operation: str`,
`args: tuple`, `kwargs: Mapping[str, object]`, `outcome: str` with value `"ok"` or the error class name). Injected failures
are recorded too. State attributes are read-only mappings keyed by `RepositoryRef`: `change_requests`, `comments`, `threads`,
`reviews`, `checks`, `work_items`. `writes` is the subset of `calls` that mutated state (including `apply=True` injected ones),
so a test asserts `host.writes == []` for a read-only consumer.

### Seam map

Each existing seam is replaced by the fake in consumer tests. Replacing them is later work (Phase split). Evidence from
f293b99. "Members" are interface methods (Interfaces) served by the fake.

| Existing seam | Where | Today | Replaced by (capability: members) |
| --- | --- | --- | --- |
| `LabelRemover` | `src/dotharness/sisyphus/pick.py` (`Protocol`) | label removal | WorkItems: `remove_label`. Inspect via `calls` / `work_items` |
| `TrackerClient` | `src/dotharness/sisyphus/pick.py` (`Protocol`, `list_candidates(source)`) | candidate listing in tracker order | WorkItems: `list_open`. Seed with `add_work_item` |
| `PullRequestLookup` | `src/dotharness/sisyphus/pick.py` (`Protocol`, `linked_prs`, `open_pr_bodies`) | linked and open PR reads | WorkItems: `closing_change_requests` (`linked_prs`), `open_change_request_bodies` (`open_pr_bodies`). Seed with `link_closing`, `add_change_request` |
| `GhCli` | `src/dotharness/sisyphus/pick.py` (class, removed in Phase 2) | wrapped the `gh` subprocess | The whole fake (all capabilities except Checks) |
| `PullRequestClient` | `src/dotharness/sisyphus/orchestrator.py` (`Protocol`) | orchestrator PR operations | ChangeRequests: `get`, `find_by_branch`, `create_draft`. Confirmed in Phase 2: `find_pr` is `find_by_branch` (open), `open_pr` is `create_draft`, `pr_state` is `get` (a number) or `find_by_branch(state=None)` (a branch) |
| injected `_gh` param | pr-review `list_own_open_prs(..., _gh)` | tests pass a fake callable | ChangeRequests: `list(author=...)` with Session `current_user`. Seed with `add_change_request(author=...)`, `set_user` |
| `gh` callable | `src/dotharness/pr_review/runners/ci_checks.py` (`async def gh(args, env)`) | runs `gh`, returns `CompletedProcess` | Checks: `list_checks`, `list_workflows`, `failed_log`, `get_job`. Seed with `add_check`, `add_workflow`, `add_job` |

Gap rule: a seam the fake cannot replace without a new interface method is reported here, not worked around. No gap is found. The two
"to be confirmed" notes were checked in Phase 2 against the code.

`src/dotharness/vibe_heal/review/github.py` (`_run_gh`) is a further `gh` call site found by search. It is not in the ticket's
call-site list (DEC-39). It is not mapped as a seam. Its operations are covered by inventory rows (INV-VH-07 for the transport
error adapter, Interfaces Session table), and the fake serves them through the Session, ChangeRequests and Discussion members.
