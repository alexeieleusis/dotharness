# Usage

`sisyphus` is a single-shot command line tool. One invocation reads one config, does one pass and exits.
There is no daemon, no loop and no scheduler installer; run it from cron or launchd.

```
sisyphus run --config <file> [--base <branch>]
sisyphus intake <work-item-ref> --config <file>
```

## `sisyphus run`

1. Loads the config (`.sisyphus.toml`, see [configuration](configuration.md)).
2. Takes the per-clone lock for `[project].repo_dir` without blocking, and holds it for the whole
   invocation (awaiting-review check, pick, every item, digest write). If another run holds it, exits 0
   quietly before doing anything else.
3. If an earlier Sisyphus PR (a run record that reached the PR stage whose branch still has an open PR)
   awaits review, skips picking and reports it under "Needs you" in the digest.
4. Otherwise picks up to `[limits].max_tickets_per_run` items (default 1) the tracker offers and Sisyphus has
   not started (no run record):
   - Linear: assigned to `assignee`, in `pick_state`, in Linear's priority order (urgent first, no priority
     last).
   - GitHub: open, assigned to `assignee`, labelled `pick_label` (default `sisyphus`), oldest first; an issue
     with an open linked PR, or one mentioned as `#N` in an open PR body, is skipped. Merged or closed PRs do
     not exclude an issue.
5. Runs each item through the orchestrator (intake, plan, build, push, open PR, optional ci-fix and merge) and
   stops at the first checkpoint (C1..C8). Before starting each further item it checks `max_run_minutes`; once
   exceeded it starts nothing else and notes it under "Limits hit" (exit 0).
6. Writes one merged digest to `[digest].path_template`.

Nothing to do prints nothing and exits 0. When something ran, one line per item and the digest path go to
stdout.

`--base <branch>` is the PR base branch. Precedence: `--base`, then `[project].base_branch` in the config,
then `main`. Self-hosting on a stacked branch (for example building Sisyphus on branch `sisyphus` stacked on
`95_pr-review_auto-approve`) needs `--base` or the config key, since `main` would be wrong there. Until PR #96
merges, use `95_pr-review_auto-approve`, then `main`.

The CLI takes the clone lock once, at the start of `run` and `intake`, and releases it when the invocation ends
(including on error). It calls the orchestrator with `lock_held=True` so the orchestrator does not lock again
(`flock` is not re-entrant), so there is no unlocked window between the pick and the items or between items.

### Credentials and backends

Credentials come only from the inherited environment and `[source.host].token_cmd` (no shell; its stdout is
the token, passed to `gh` as `GH_TOKEN` and used as the Linear API key). Linear without `token_cmd` reads
`LINEAR_API_KEY` from the environment. `gh` must be installed and able to reach the repository.

`[agent].backend` selects the agent command, run inside `repo_dir`:

| backend | command |
|---|---|
| `claude` | `claude -p <prompt> --dangerously-skip-permissions` |
| `opencode` | `opencode run <prompt>` |

The agent runs in the dedicated clone only (see [safety rails](safety-rails.md)). Real `claude`/`opencode`,
`gh`, Linear and `git push` calls are built from subprocess and `urllib` and are covered by tests with fakes
only; they have not been exercised against the real services.

## `sisyphus intake`

Runs only the intake stage for one work item reference: the agent drafts the brief at the path from
`brief_path_template`. Prints `brief: <path>` and any open decisions (checkpoint C1). Needs
`[project].repo_dir`, `workspace_dir` and `brief_path_template`. Takes the same lock; exits 0 quietly if held.

## Exit codes

| code | meaning |
|---|---|
| 0 | success, nothing to do, lock held, or a run stopped at a checkpoint (C1..C8) after its digest was written (the checkpoint is in the digest's "Needs you" section) |
| 1 | an unhandled error, a config error, or a digest that cannot be written (message on stderr) |

The digest is always attempted when anything ran, even if the run failed early; the failure is recorded in
its Deviations section.

## Scheduling

Run it with an absolute config path and an explicit base. Output is empty when there is nothing to do, so a
cron mailer stays quiet.

### cron

```
# every 30 minutes
*/30 * * * * cd /path/to/dedicated/clone && /usr/local/bin/sisyphus run --config /path/to/.sisyphus.toml --base main >> "$HOME/.local/state/sisyphus.log" 2>&1
```

Cron has a minimal `PATH`; use absolute paths, or set `PATH` (so `gh`, `git`, `claude` are found) at the top of
the crontab.

### launchd

Save as `~/Library/LaunchAgents/com.example.sisyphus.plist`, then
`launchctl load ~/Library/LaunchAgents/com.example.sisyphus.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.sisyphus</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/sisyphus</string>
    <string>run</string>
    <string>--config</string>
    <string>/path/to/.sisyphus.toml</string>
    <string>--base</string>
    <string>main</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartInterval</key><integer>1800</integer>
  <key>StandardOutPath</key><string>/tmp/sisyphus.out.log</string>
  <key>StandardErrorPath</key><string>/tmp/sisyphus.err.log</string>
</dict>
</plist>
```

`StartInterval` is in seconds. Overlapping runs are harmless: the second one finds the lock held and exits 0.
