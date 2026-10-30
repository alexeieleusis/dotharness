# Usage

`sisyphus` is a single-shot command line tool. One invocation reads one config, does one pass and exits.
There is no daemon, no loop and no scheduler installer. Run it from cron or launchd.

```
sisyphus run --config <file> [--base <branch>]
sisyphus intake <work-item-ref> --config <file>
sisyphus status [<work-item-ref>] --config <file>
sisyphus resume <work-item-ref> --config <file> [--base <branch>]
```

## `sisyphus run`

1. Loads the config (`.sisyphus.toml`, see [configuration](configuration.md)).
2. Takes the per-clone lock for `[project].repo_dir` without blocking, and holds it for the whole
   invocation (pick, every item, digest write). If another run holds it, exits 0
   quietly before doing anything else.
3. Auto-resumes stopped items whose blocker is cleared (see "Stopped items and resume"), oldest stop first.
   Items still waiting for you never stop the loop. They are listed under "Needs you" as "Awaiting you".
4. Picks as many new items as are left of `[limits].max_tickets_per_run` (default 1. Resumed and new
   items share it) that the tracker offers and Sisyphus has not started (no run record):
   - Linear: assigned to `assignee`, in `pick_state`, in Linear's priority order (urgent first, no priority
     last).
   - GitHub: open, assigned to `assignee`, labelled `pick_label` (default `sisyphus`), oldest first. An issue
     with an open linked PR, or one mentioned as `#N` in an open PR body, is skipped. Merged or closed PRs do
     not exclude an issue.
   - File: the `*.md` ticket files in `[source.work_item].dir`, in Linear priority order (1 to 4 first, none
     last), then oldest modification time first, then `ref`. No `assignee` is needed and no PR lookup runs. A file
     whose front matter is invalid (see "Ticket files and the producer") is skipped with a one-line note on
     stderr. Finished ticket files stay in place: they are skipped because the item has a run record, not
     because the file is removed.
5. Runs each item through the orchestrator (intake, plan, build, push, open PR) and
   stops at the first checkpoint (C1..C8). Before starting each further item it checks `max_run_minutes`. Once
   exceeded it starts nothing else and notes it under "Limits hit" (exit 0).
   An item is finished when its draft PR is open: "draft PR open, handed to the human". Nothing runs after
   that. Sisyphus always creates the PR as a draft; the human validates it and marks it ready. `review-prs`,
   `auto-approve` and `ci-fix` skip drafts, so pr-review does not take over until the draft is marked ready. If
   pr-review is configured, self-review and vibe-heal analyze the draft in the meantime. If a non-draft open PR
   already exists for the branch, Sisyphus reuses it and neither converts nor edits it.
6. Writes one merged digest to `[digest].path_template`. It never overwrites an existing file: a second run
   on the same day writes `digest-<date>-2.md`, then `-3`, and so on.

Nothing to do prints nothing and exits 0. When something ran, one line per item and the digest path go to
stdout.

`--base <branch>` is the PR base branch. Precedence: `--base`, then `[project].base_branch` in the config,
then `main`. `main` is the correct base for normal use. Only if you work on your own stacked branch (a branch
whose PR targets another unmerged branch) do you need `--base` or the config key to point at that parent branch.

The CLI takes the clone lock once, at the start of `run` and `intake`, and releases it when the invocation ends
(including on error). It calls the orchestrator with `lock_held=True`. The orchestrator does not lock again
(`flock` is not re-entrant). There is no unlocked window between the pick and the items, or between items.

### Credentials and backends

Credentials come only from the inherited environment and `[source.host].token_cmd` (no shell. Its stdout is
the token, passed to `gh` as `GH_TOKEN` only). Linear always reads its API key from `LINEAR_API_KEY` in the
environment. `token_cmd` is never sent to Linear. `gh` must be installed and able to reach the repository. The `file` kind needs no API key and makes no network
call to pick an item.

`[agent].backend` selects the agent command, run inside `repo_dir`:

| backend | command |
|---|---|
| `claude` | `claude -p <prompt> --dangerously-skip-permissions` |
| `opencode` | `opencode run <prompt>` |

The agent's working directory is the dedicated clone (see [safety rails](safety-rails.md)), but that is not a
sandbox. `claude --dangerously-skip-permissions` can read and write anywhere the invoking user can, and Sisyphus
does not scrub the environment: the agent inherits the full environment of the `sisyphus` process, including
`LINEAR_API_KEY` or `GH_TOKEN` if they are exported there. (A token produced by `token_cmd` is added only to the
`gh` calls Sisyphus makes itself, not to the agent command.) Intake feeds ticket and issue text to the agent, so
a prompt injection in that text could try to exfiltrate those credentials or push elsewhere. Mitigate it by:

- using a least-privilege token scoped to the one repository (and a Linear key limited to what is needed).
- running Sisyphus under a dedicated OS user or in a container, with nothing else of value reachable.
- preferring `token_cmd` over exporting tracker credentials in the environment, and keeping them out of the
  crontab/plist environment.

Real `claude`/`opencode`,
`gh`, Linear and `git push` calls are built from subprocess and `urllib` and are covered by tests with fakes
only. They have not been exercised against the real services.

## `sisyphus intake`

Runs only the intake stage for one work item reference: the agent drafts the brief at the path from
`brief_path_template`. Prints `brief: <path>` and any open decisions (checkpoint C1). Needs
`[project].repo_dir`, `workspace_dir` and `brief_path_template`. Takes the same lock. Exits 0 quietly if held.

## Ticket files and the producer

With `kind = "file"` Sisyphus reads tickets from `<dir>/<ref>.md`. Each file is a front-matter block, then the
ticket body:

```markdown
---
ref: ENG-123
title: Rate-limit the export endpoint
url: https://linear.app/acme/issue/ENG-123
priority: 2
---

<description, acceptance criteria and relevant comments>
```

`ref` must equal the file-name stem and use only letters, digits, `.`, `_` and `-` (a name made only of dots is
rejected). `title`, `url` and `priority` (integer 0 to 4, 0 meaning none) are optional. A bad `priority` counts as
0 and a missing `title` falls back to `ref`. The body is untrusted ticket content and is not parsed. An invalid
file (missing `---` line, missing `ref`, bad `ref`) is skipped and one line naming the file and the reason goes
to stderr. A missing or empty `dir` picks nothing. Finished ticket files stay in place.

### Producer: exporting Linear tickets

`knowledge/sisyphus/linear-feed-prompt.md` is a prompt that exports one Linear ticket into `dir`. Its only
parameter is `<config path>`. It reads `dir`, `assignee`, `pick_state` and the optional `pick_label` from
`[source.work_item]`, lists matching tickets through the Linear MCP in priority order, takes the first one with
no file in `dir` yet, writes `<dir>/<ref>.md` and stops. It never overwrites a file and only reads Linear. There
is no `sisyphus` subcommand for it. Run it with the agent CLI, with the parameter substituted:

```
claude -p "<prompt with <config path> replaced by /path/to/.sisyphus.toml>" --dangerously-skip-permissions
```

As for the backends above, `--dangerously-skip-permissions` grants the invoking user's full access, and Linear text
is untrusted input. Prerequisites: the Linear MCP connector is authorised for the user who runs it, and `dir`
exists and is writable from where the producer runs.

The producer is safe to repeat. A ticket Sisyphus finished stays in Linear status `pick_state`, but its file is
still present (never re-written) and it has a run record (never re-picked). If you delete a ticket file by hand,
the producer writes it again and Sisyphus still skips it because of the run record.

## Stopped items and resume

When a run stops (a checkpoint C1..C8, or an unexpected error) Sisyphus writes a stop record next to the run
record, `<workspace_dir>/<item>.json.stopped.json`, with the stage, the checkpoint, the questions or blocker, the
exact action you must take and the exact resume command. The digest's "Needs you" section shows it (question,
`Action:`, `Resume:`), and `sisyphus status` prints it. The run record's retry counters are never reset by a stop
or a resume.

**Single dedicated clone.** You never edit code in the clone and never move anything between repositories. You
answer in files under `workspace_dir` (outside the repo): the brief, or an answer file. Each item has its own
branch `sisyphus/<slug>-<8 hex>`, so partial work stays on its branch while another item runs. The clone must
be clean between items: a new item, and a build checkout, refuse to start on a dirty clone (checkpoint C8 plus a
digest entry). Sisyphus never stashes, resets or discards anything. A resume at push or PR checks
out the item's own branch first (or, if it was already deleted after a clean finish, works from `origin/<branch>`
detached). Committed work is never redone.

**Local branch cleanup.** After a clean finish (every wanted stage complete, no checkpoint, no error, no stop or
abandoned mark, and the branch was created by Sisyphus) the local branch is deleted so the clone does not collect
stale branches. It is kept, with a `Branch kept: REF <branch> ...` note in the digest's Deviations, unless the
clone is clean, base can be switched to and fast-forwarded from `origin/<base>`, and one proof holds (checked in this
order, cheapest first): (1) the head is an ancestor of `origin/<branch>`. (2) the head is an ancestor of
`origin/<base>` (a merge-commit or rebase merge, even after GitHub deleted the remote branch). (3) `gh pr view`
says the PR is MERGED with a head equal to the local head. After (1) or (2) `git branch -d` must agree (a refusal keeps the branch). A squash-merged branch is proven by (3)
and deleted with `git branch -D`, which is used on that path only (never `--force`, no recovery ref. The reflog is
the last resort). When nothing is proven the note lists each proof tried and why it failed.
Never a remote branch or PR change. A deletion is shown under Done and names the proof used. For proof (3) it gives
the full head SHA, the PR number and `git fetch origin pull/<N>/head`, which recovers the commit from GitHub. Rerunning after the branch is gone
is a silent no-op. The run record, digest, stop and answer
files and `<brief>.intake.json` are never deleted.

**Answering.** C1: edit the brief and mark each open decision resolved. C2, C3, C4: write your answer to
`<workspace_dir>/<item-encoded>.answer.md` (the path is in the action text. Any non-empty text counts). The
agent gets the answer text with its next prompt and the file is renamed `*.answer.md.used`.

**Automatic or explicit.** Each `run` first resumes the stopped items that are cleared, with no agent call
needed to decide: C1 when the brief no longer has blocking open decisions, C2..C4 when the answer file is
non-empty, C8 after 30 minutes since the stop, at most 3 automatic retries. Everything else (C5, C6, C7, errors)
needs `sisyphus resume`. `sisyphus resume REF --config F [--base B]` also works for any stopped item at any time
(for a not yet answered one it simply stops again with the same question). It errors (exit 1) when there is no
run record or the item is not stopped, and exits 0 quietly if another invocation holds the lock.

| stop | what stopped | what you do | clears | what resume does |
|---|---|---|---|---|
| C1 | brief has open decisions | edit the brief, mark them resolved | automatic (brief re-parsed) | re-parses the brief (never redrafts it), then plan init, plan, build, push, PR |
| C2 | OPEN_QUESTIONS.md unresolved after draft-overview | write the answer file | automatic (answer file) | re-runs the plan stage with your answer, then build |
| C3 | a leaf has an open question | write the answer file | automatic (answer file) | re-runs build with your answer. The build prompt continues from the stopped leaf |
| C4 | a leaf deviation may change later leaves | write the answer file (accept or redirect) | automatic (answer file) | same as C3 |
| C5 | retries exhausted, scope violation or a failed gate | decide: change the leaf or config, or fix the cause. To give the build another budget, run `sisyphus resume REF --config F --reset-retries` | explicit | re-runs the stage. The build retry budget (3) is kept, so an exhausted budget stops again at C5 without a new agent run, unless you pass `--reset-retries` (below), which gives the build a fresh budget of 3 (it does not skip the build) |
| C6 | a limit was hit | raise the limit in `.sisyphus.toml` (`max_run_minutes` needs nothing: the clock is per invocation) | explicit | leaf limit: re-counts the leaves, no re-plan. Time limit: continues at the next stage |
| C7 | branch cannot be updated without rebase | no automatic way out: run `sisyphus abandon REF --config F` (the branch and any PR stay as they are for you to handle) | explicit | after you fixed the branch outside Sisyphus: re-runs the push stage on the same branch and PR |
| C8 | gh, Linear, git network call failed, or a dirty clone | fix access. For a dirty clone, clean it yourself | automatic retry after 30 minutes, 3 times, then explicit | retries the failed call in the same stage |
| error | unexpected exception | fix the cause shown | explicit | re-runs the in-progress stage |

## Fresh retry budget: `resume --reset-retries`

`sisyphus resume REF --config F [--base B] --reset-retries` is the only way to zero retry counters, and it is
explicit: `run`, auto-resume, plain `resume` and loading a record never reset anything. Use it after you fixed the
cause by hand (in the brief or the answer file) or decided to give the build another budget. It does **not** skip
the stage: it gives the recorded stage a fresh budget (build: `max_retry_cycles`, 3) and then continues the
resume as usual. It is only valid for a stopped item (same errors as `resume`), takes the same lock, and
combines with `--again` for an abandoned item.

It resets the counters of the stage recorded in the item's run record, nothing else:

| recorded stage | counters set to 0 | never touched |
|---|---|---|
| intake, plan, push, open-pr | `auto-retry` | `build:retry-cycles`, `plan:leaf-limit`, `branch-created` |
| build | `build:retry-cycles`, `auto-retry` | `plan:leaf-limit`, `branch-created` |

`auto-retry` is the C8 automatic-resume count (stage independent). `branch-created` is not a retry counter (it
records that Sisyphus made the branch, which the push guard relies on) and `plan:leaf-limit` is a marker that
stops a C6 resume re-planning. Neither is ever reset. The mapping is `resume.RESET_KEYS`. If the recorded stage
has no counter above zero (for example an item stopped at C1) the flag is still accepted: it records a reset with
no keys ("no counters to reset") and continues.

Every reset is recorded durably in the run record (additive `resets` list: time, stage, each key with its
previous value) and is shown by `sisyphus status` and under Deviations in the digest, for example
`Retry reset: PROJ-7 at stage build on 2026-03-04T05:06:00+00:00: build:retry-cycles 3->0, auto-retry 1->0`.

## Dead ends: `sisyphus abandon`

Some stops have no clean way forward (C7 above all: Sisyphus never rebases or force-pushes, and the dedicated
clone is not yours to edit). `sisyphus abandon REF --config F` is the explicit way out. It takes the same
invocation-wide lock as `resume` (quiet exit 0 if another run holds it), requires a run record (exit 1 with a
message otherwise) and then:

- marks the item abandoned: the stop record becomes `kind: "abandoned"` with the time in `stopped_at`, and the
  run record's checkpoint is cleared. The run record, the local branch, the digest and the answer/brief files stay.
- removes the waiting state: the item is no longer "awaiting you", and is never auto-resumed. It is never re-picked either (any run record makes an item non-pickable).
- never touches the remote branch or any PR, never rewrites history or force-pushes. What happens to them is for
  you to decide by hand.

Abandoning an already abandoned item is a no-op (exit 0). `status` shows `state: abandoned`, `abandoned_at` and
the `re-enable` command. The digest lists a short "Abandoned" note under Deviations (not under "Needs you").

**Re-enabling is deliberate:** `sisyphus resume REF --config F --again` clears the abandoned mark and resumes
from the recorded stage (without `--again`, `resume` refuses an abandoned item and says so). `--again` on an item
that is not abandoned is an error. Reusing `resume` keeps the surface small: one command continues work, one
flag says "I know I abandoned this".

## `sisyphus status`

`sisyphus status [REF] --config F` is read-only (no lock, no agent). For each item (all items without REF) it
prints stage, stage_status, checkpoint (`state: abandoned` plus `abandoned_at` and `re-enable` for an abandoned item), `awaiting` (`you` for a checkpoint, `error`, `none` otherwise (including an abandoned item). `status` needs no network), and for a stopped item the question, action, resume command and auto-resume rule (with whether it is
cleared now) and every recorded `Retry reset:` line. No items prints `no items`. An unknown REF exits 1.

## Exit codes

| code | meaning |
|---|---|
| 0 | success, nothing to do, lock held, or a run stopped at a checkpoint (C1..C8) after its digest was written (the checkpoint is in the digest's "Needs you" section) |
| 1 | an unhandled error, a config error, `resume`/`status` of an item that has no record (or `resume` of one that is not stopped), or a digest that cannot be written (message on stderr) |

The digest is always attempted when anything ran, even if the run failed early. The failure is recorded in
its Deviations section.

## Scheduling

Run it with an absolute config path and an explicit base. Output is empty when there is nothing to do, so a
cron mailer stays quiet.

### cron

```
# every 30 minutes
*/30 * * * * cd /path/to/dedicated/clone && /usr/local/bin/sisyphus run --config /path/to/.sisyphus.toml --base main >> "$HOME/.local/state/sisyphus.log" 2>&1
```

Cron has a minimal `PATH`. Use absolute paths, or set `PATH` (so `gh`, `git`, `claude` are found) at the top of
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
