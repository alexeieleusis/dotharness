# Safety rails

Code-enforced guarantees for unattended runs. Modules: `sisyphus.paths`, `sisyphus.lock`,
`sisyphus.rails`. All are pure and injectable (home, checkout root, lock dir, clock); none calls
the network, a subprocess or the harness.

## Rail to checkpoint map

| Rail | Inputs | Result | Checkpoint |
| --- | --- | --- | --- |
| Push guard (`push_guard`) | `PushRequest(remote, refspec, flags, history_rewrite, needs_rebase)`, default branch, set of Sisyphus-created branch names | `PushDecision(allowed, reason, checkpoint)` | C7 when the branch needs a rebase or force-push; plain rejections (force flag, `+refspec`, history rewrite or deletion, default branch, uncreated branch) carry no checkpoint: they are caller bugs |
| Limit tracker (`LimitTracker`) | `[limits]` values, injectable clock; `record_ticket`, `record_leaf`, `record_fix_attempt(pr)` | `LimitStatus(reached, checkpoint)` | C6 when any limit reached; at count == limit the limit is reached |
| Bounded retries (`RetryBound`) | `max_retry_cycles` (default 3) | `RetryResult(attempts, escalate, checkpoint)` | C5 once attempts reach the cap |
| Scope violation (`ScopeViolationResult`) | files, detail reported by the caller | result type only | C5 |
| Merge-gate failure (`MergeGateFailureResult`) | gate name, detail reported by the caller | result type only | C5 |
| Permission model | `--dangerously-skip-permissions` (`PERMISSION_FLAG`) accepted (D-11) | n/a | boundary is the dedicated clone plus the push guard; Package A never approves or merges |

`Checkpoint` is the enum C1..C8; checkpoint names are opaque strings elsewhere (run record).

`parse_push_argv(argv)` turns `[git] push [flags] <remote> <refspec>` into a `PushRequest`.
History rewrite and rebase-needed are git-state facts the caller passes in.

## Lock

`clone_lock(clone_dir, lock_dir=None)` takes a non-blocking `flock` on a file named by a hash of
the resolved clone directory (path only). Contention raises `LockHeld`, which the CLI maps to a
quiet exit 0. The lock is released on normal exit and on exceptions. Different clone directories
never contend.

## Path resolution order

`tool_dir`, `knowledge_dir` and `knowledge_file(name)`: the installed location under the home
directory (`~/.harness/tools/spec-prism-flow`, `~/.harness/knowledge`) first; otherwise the dev
checkout, found by walking up from the package file to the directory holding `knowledge/` and
`tools/`. If neither exists, `PathResolutionError` names every path tried.
