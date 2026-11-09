# sisyphus

This tool is part of `dotharness` since I-5. See the
[migration ledger](migration-ledger.md) for module status.

- [Configuration](sisyphus/configuration.md) — the `.sisyphus.toml` file.
- [Usage](sisyphus/usage.md) — the `sisyphus` commands.

## Intake base and brief parsing

Before the intake agent runs, the clone is detached at the intake base so the
agent reads the code the work will build on, not a stale local branch.

**Base resolution order.** The base name is `--base`, else `[project].base_branch`,
else the repository's default branch, else `main`. The default-branch fallback
applies to intake only; the PR base and the push guard keep their existing
semantics. The first candidate that exists wins:

1. `origin/<base>`, probed with `git ls-remote --exit-code --heads origin <base>`
   and then fetched.
2. The local branch `<base>`.
3. `origin/main` (skipped when the base is already `main`).

If none exists, the run stops at checkpoint C8 and names all three candidates.

**Detached checkout.** The clone is switched with `git checkout --detach <ref>`,
so no local branch is moved or required. The agent runner starts with HEAD at
the base commit.

**C8 on network or auth failure.** An `ls-remote` that fails for any reason
other than "branch not found" (exit 2), a failed `fetch`, or a failed checkout
stops the run at C8 with the git command and the tail of its stderr. There is no
silent fallback to the local branch in that case.

**Divergence warning.** When both `origin/<base>` and a local `<base>` exist and
point at different commits, the run records a note naming both short SHAs and
says it is using `origin/<base>`. This is a note, not a stop.

**When preparation is skipped.** Preparation runs only when the intake agent
will run. A C1 reparse (resuming after the owner edited the brief) and a skip
because the brief sidecar matches run no `ls-remote`, `fetch` or `checkout`.

**Parser rule.** A brief can contain several "Open decisions" headings (for
example prose first, the log later). The parser reads the first section that
contains numbered entries. If no section has entries, the first section decides:
"None" means no open decisions, and a malformed lone section blocks as before.
