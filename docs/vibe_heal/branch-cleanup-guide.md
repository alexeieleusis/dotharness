# Branch Cleanup Guide

## Overview

The `vibe_heal cleanup` command automatically fixes all SonarQube issues in modified files of your feature branch before code review. This ensures pull requests have no new code quality issues.

Two related commands are covered here as well: `dedupe-branch` (duplications) and `cleanup-pr` (only findings on lines your branch changed, see [PR-Scoped Cleanup](#pr-scoped-cleanup-cleanup-pr)).

## When to Use

Use `vibe_heal cleanup` when:

- 🔍 **Before creating a pull request** - Ensure your branch is clean
- ✨ **After implementing a feature** - Clean up any issues introduced
- 🚀 **In CI/CD pipelines** - Automatically fix issues before merge
- 📝 **During code review** - Address quality issues systematically

## How It Works

1. **Analyzes branch**: Compares current branch against base branch (default: `origin/main`)
2. **Creates temporary project**: Creates a unique SonarQube project for analysis
   - Project naming format: `{base_key}_{sanitized_email}_{sanitized_branch}_{timestamp}`
   - Timestamp format: `yymmdd-hhmm` (e.g., `251024-1630` for October 24, 2025 at 4:30 PM UTC)
   - Example: `my-project_user_example_com_feature_new_api_251024-1630`
3. **Runs analysis**: Analyzes all modified files using `sonar-scanner`
4. **Fixes issues iteratively**: For each file:
   - Runs SonarQube analysis
   - Gets fixable issues
   - Fixes all issues using AI tool
   - Creates git commits for each fix
   - Repeats until no fixable issues remain (or max iterations reached)
5. **Cleans up**: Deletes temporary SonarQube project

## Prerequisites

### Required

1. **sonar-scanner CLI** must be installed
   ```bash
   # macOS
   brew install sonar-scanner

   # Linux
   # Download from https://docs.sonarsource.com/sonarqube/latest/analyzing-source-code/scanners/sonarscanner/
   ```

2. **SonarQube server** with API access

3. **AI tool** installed (Claude Code or opencode)

4. **Git repository** with remote branches

5. **SonarQube user permissions**:
   - Create projects (`POST /api/projects/create`)
   - Delete projects (`POST /api/projects/delete`)
   - Run analysis

### Configuration

Create `.env.vibeheal`:

```bash
# Required
SONARQUBE_URL=https://sonar.example.com
SONARQUBE_TOKEN=your_token_here
SONARQUBE_PROJECT_KEY=your_project_key

# Optional - AI tool will be auto-detected if not specified
AI_TOOL=claude-code  # or "opencode"

# Optional - pre-commit hook behaviour (see Pre-commit Hook Compatibility below)
# PRE_COMMIT_COMMAND=            # empty string = disabled
# PRE_COMMIT_COMMAND=pre-commit run --files  # explicit command
```

## sonar-project.properties Support

If your project already has a `sonar-project.properties` file at the repository root, vibe_heal detects it automatically and adjusts its scanner invocation accordingly.

### How It Works

**Without the file** — vibe_heal passes all scanner settings as `-D` flags:

```
sonar-scanner -Dsonar.projectKey=... -Dsonar.projectName=... -Dsonar.host.url=... -Dsonar.token=... -Dsonar.sources=.
```

**With the file** — vibe_heal builds a minimal command and only appends flags that are not already configured:

```
sonar-scanner  # -Dsonar.token=... added only if no auth found in file or env
```

Auth presence is checked in both the properties file itself and the environment variables `SONAR_TOKEN`, `SONARQUBE_TOKEN`, `SONAR_LOGIN`. Host URL is similarly checked against `SONAR_HOST_URL` and `SONARQUBE_HOST_URL`.

### Temporary Project Patching

`cleanup` (and `dedupe-branch`) create a **temporary** SonarQube project for the analysis run. Because the properties file governs the project key, vibe_heal patches `sonar.projectKey` and `sonar.projectName` in the file for the duration of the analysis, then restores the original content.

Before patching, vibe_heal writes a recovery comment block so the file can be restored manually if the process is interrupted:

```properties
# vibe_heal: temporary analysis project. If this process was interrupted,
# restore the lines below (remove the '#' prefix):
# sonar.projectKey=my-project
# sonar.projectName=My Project
sonar.projectKey=my-project_user_example_com_feature_new_api_260525-1430
sonar.projectName=vibe_heal cleanup: my-project (feature/new-api @ 2026-05-25 14:30)
```

On normal completion the original values are restored automatically.

### If sonar-scanner Reports Authentication Errors

When the properties file exists and a scanner run fails with an auth-related error (401/403), vibe_heal prints a hint:

```
Hint: authentication may be configured via environment variable
(SONAR_TOKEN, SONARQUBE_TOKEN) or the central scanner settings
(~/.sonar/sonar-scanner.properties). Check these if you expected
auth to be picked up automatically.
```

If auth is already in the properties file or env, vibe_heal will not add it again, so check both places.

## Basic Usage

### Clean up current branch

```bash
# Clean up all modified files
vibe_heal cleanup
```

This will:
- Compare against `origin/main`
- Fix all modified files
- Run up to 10 iterations per file
- Create git commits for each fix

### Specify base branch

```bash
# Compare against develop branch
vibe_heal cleanup --base-branch origin/develop

# Compare against a specific branch
vibe_heal cleanup --base-branch origin/release-1.0
```

### Filter by file patterns

```bash
# Only clean up Python files
vibe_heal cleanup --pattern "*.py"

# Clean up multiple patterns
vibe_heal cleanup --pattern "*.py" --pattern "*.ts"

# Clean up specific directory
vibe_heal cleanup --pattern "src/**/*.py"
```

### Adjust iteration limit

```bash
# More iterations for stubborn issues
vibe_heal cleanup --max-iterations 20

# Fewer iterations for quick cleanup
vibe_heal cleanup --max-iterations 5
```

### Specify AI tool

```bash
# Use Claude Code explicitly
vibe_heal cleanup --ai-tool claude-code

# Use opencode explicitly
vibe_heal cleanup --ai-tool opencode
```

### Use custom environment file

```bash
# Use a different environment file for production SonarQube
vibe_heal cleanup --env-file .env.production

# Use different configs for different projects
vibe_heal cleanup --env-file ~/configs/project-a.env
```

### Verbose output

```bash
# Enable verbose logging
vibe_heal cleanup --verbose
```

## Examples

### Example 1: Pre-PR cleanup

```bash
# You're on feature/new-api branch
git checkout feature/new-api

# Clean up all modified files before creating PR
vibe_heal cleanup

# Output:
# Branch Cleanup
#   Base branch: origin/main
#   Max iterations per file: 10
#
# Auto-detected AI tool: Claude Code
#
# Cleanup Summary:
#   Files processed: 5
#   Total issues fixed: 23
#
# Per-File Results:
#   ✓ src/api/users.py: 8 issues fixed
#   ✓ src/api/auth.py: 5 issues fixed
#   ✓ src/models/user.py: 4 issues fixed
#   ✓ src/utils/validation.py: 6 issues fixed
#   ✓ tests/test_api.py: 0 issues fixed
#
# ✨ Branch cleanup complete!
```

### Example 2: Clean up only backend files

```bash
# Only fix Python files in src/ directory
vibe_heal cleanup --pattern "src/**/*.py"
```

### Example 3: Clean up with custom base and more iterations

```bash
# Compare against develop, allow more iterations
vibe_heal cleanup --base-branch origin/develop --max-iterations 15
```

## Output and Results

### Success Output

```
Branch Cleanup
  Base branch: origin/main
  Max iterations per file: 10

Using configured AI tool: Claude Code

Cleanup Summary:
  Files processed: 3
  Total issues fixed: 12

Per-File Results:
  ✓ src/file1.py: 5 issues fixed
  ✓ src/file2.py: 7 issues fixed
  ✓ src/file3.py: 0 issues fixed

✨ Branch cleanup complete!
```

### Failure Output

```
Branch Cleanup
  Base branch: origin/main
  Max iterations per file: 10

Using configured AI tool: Claude Code

Cleanup Summary:
  Files processed: 2
  Total issues fixed: 5

Per-File Results:
  ✓ src/file1.py: 5 issues fixed
  ✗ src/file2.py: 0 issues fixed
      Error: Analysis failed at iteration 1: Analysis failed

Cleanup failed: 1 fixes failed
```

### Git Commits

Each fix creates a separate git commit:

```
fix: [python:S1234] Remove unused import

SonarQube Issue: https://sonar.example.com/issues?id=issue-key
Rule: python:S1234
Severity: MAJOR
File: src/api/users.py:15

Message: Remove this unused import of 'datetime'

Fixed by: Claude Code

🤖 Generated with Claude Code
Co-Authored-By: Claude <noreply@anthropic.com>
```

## PR-Scoped Cleanup (`cleanup-pr`)

`vibe_heal cleanup-pr` fixes only the findings **your branch introduced**: SonarQube issues and duplications on lines changed relative to the base branch. Pre-existing findings elsewhere in the same files are left alone. Use it when `cleanup` would touch too much of a large, legacy file.

`cleanup-pr` does **not** replace `cleanup` or `dedupe-branch`, which keep whole-file semantics. It also does no GitHub interaction and never pushes: commits stay local. To report findings on the PR afterwards, run `vibe_heal review --post`.

### Usage

```bash
# Fix issues and duplications on lines changed by the current branch
vibe_heal cleanup-pr

# Preview in-scope fixes (the AI tool is invoked; nothing is committed)
vibe_heal cleanup-pr --dry-run

# Compare against another base, only Python files, only MAJOR and above issues
vibe_heal cleanup-pr --base-branch origin/develop --pattern "*.py" --min-severity MAJOR

# Also refactor duplications that existed in main (see the warning below)
vibe_heal cleanup-pr --include-main-duplications
```

### Options

| Flag | Default | Behavior |
|---|---|---|
| `--base-branch`, `-b` | `origin/main` | Base to diff against. Plain `origin/main`; no `gh` auto-detection |
| `--max-iterations`, `-i` | `10` | Maximum analyze -> fix rounds for the whole branch |
| `--pattern`, `-p` | none | Glob filters (repeatable), same as `cleanup` (matched with `Path.match`) |
| `--min-severity` | none | Minimum severity (`BLOCKER`, `CRITICAL`, `MAJOR`, `MINOR`, `INFO`); applies to issues only |
| `--dry-run` | off | Run analysis and scoping, attempt each in-scope fix with the AI tool, and report what would be fixed. No commits are made; the uncommitted edits remain in the working tree (discard with `git checkout -- .`). Runs a single round |
| `--ai-tool` | auto-detect | Same as `cleanup` |
| `--env-file` | `.env.vibeheal` / `.env` | Same as `cleanup` |
| `--verbose`, `-v` | off | Same as `cleanup` |
| `--include-main-duplications` | off | Also handle duplications that existed in main (runs a baseline scan) |

`--pattern` uses `Path.match` (like `cleanup` and `review`), whereas `dedupe-branch` uses `fnmatch`, so the same pattern can behave differently between the commands.

### What "in scope" means

- **Issues** are kept if they sit on lines added or modified by the branch, plus a 3-line trailing window (the same filter `review` uses).
- **Duplications** are fixed only when the duplicated block in your file intersects lines strictly changed by the branch.
- Issue fixes stay within one file. A duplication refactor may edit other files (for example when extracting a shared helper); a warning is printed when a refactor touches a file outside the branch diff. Review those commits.

### Workflow

1. **Preconditions** (all checked before any SonarQube work):
   - You are in a git repository and the base branch exists.
   - An AI tool is available, even with `--dry-run` (the in-scope fixers invoke it; only the main-duplication counts are reported without an AI call).
   - The working tree has no modified or staged files (untracked files are fine).
   - The base ref's remote is fetched, also in `--dry-run`. If the fetch fails, the command refuses to run; there is no fallback to the local ref.
   - The base tip is an ancestor of `HEAD`. Otherwise it stops with `Branch is not up to date with origin/main; rebase or merge origin/main first`. A branch that merged the base in also passes. The rule applies to any `--base-branch`.
2. **File selection**: modified files versus the base, then the `--pattern` filter. An empty selection is a success with zero counts.
3. **Baseline scan** (only with `--include-main-duplications`, see below).
4. **Temporary project**: a temporary SonarQube project is created for the analysis.
5. **Iteration loop**, up to `--max-iterations` rounds: analyze the whole repository, recompute the diff against the base, then for each file fix in-scope duplications first and issues second. If a duplication commit landed in a file, that file's issues are deferred to the next round, so they are re-evaluated against the new code. Issues introduced by cleanup-pr's own commits are in scope on the next round. The loop stops early once nothing in scope remains, and waits 5 seconds between rounds.
6. **Cleanup**: the temporary project is always deleted afterwards. A deletion failure is only a warning.

### Main duplications (`--include-main-duplications`)

> **Warning:** this flag runs a baseline scan of the base ref, in a temporary detached `git worktree`, against your **real** SonarQube project key. This **overwrites the real project's analysis on the SonarQube server, including with `--dry-run`**. With the flag, a run performs two full-repository analyses. The scan is skipped when no files are selected; if it fails the run stops with `Baseline scan failed: ...`.

A *main duplication* is a duplication that existed in main, was modified or removed by your branch, and is no longer active. For each one, the AI tool is asked to extract the shared code into a helper, update all other locations, and then re-apply your branch's change on top, with no behavior change beyond your diff. Each one gets its own commit. With `--dry-run` they are only counted as would-fix.

These mechanics are the least proven part of the command. Review those commits carefully.

### Commit formats

One commit per fix, with no extra trailer:

| Kind | Subject |
|---|---|
| Issue | `fix: [SQ-RULE] message` |
| Duplication | `refactor: [duplication] remove duplicate code at line X` |
| Main duplication | `refactor: [duplication] extract shared code from removed main duplication at line X` |

### Output

```
Branch Cleanup (PR scope)
  Base branch: origin/main
  Max analysis rounds (whole branch): 10

Cleanup Summary (PR scope):
  Files processed: 2
  Total issues fixed: 3
  Total duplications fixed: 1
  Total main duplications fixed: 0

Per-File Results:
  ✓ src/api/users.py
      issues: 2 fixed, 4 out of scope
      duplications: 1 fixed, 0 out of scope
  ✓ src/api/auth.py
      issues: 1 fixed, 0 out of scope
      duplications: 0 fixed, 1 out of scope

✨ Branch cleanup (PR scope) complete!
```

Out-of-scope counts show what was deliberately left alone. With `--dry-run` the summary adds `Dry run: no changes were made`, meaning no commits: in-scope fixes were still attempted by the AI tool, and their uncommitted edits remain in the working tree — discard them with `git checkout -- .` before the next run. The summary also lists the total of main duplications fixed and, when duplication refactors edited files outside the branch diff, those file paths. Per-file lines add `main duplications: N fixed, M skipped` when there is anything to report. The "Max analysis rounds" header line reflects `--max-iterations`.

### Failure behavior

- **Nothing is reverted.** A failed AI attempt is counted and processing continues, but its edits stay in the working tree. The next fix attempt then aborts with a dirty-working-tree error, so a single failed fix can end the whole run with a failed result. Commits made earlier are kept. Discard (`git checkout -- .`) or commit the leftover edits by hand before re-running.
- A failed analysis returns a failed result immediately.
- Exit codes: `Configuration error: ...` exits 1; any other exception prints `Error: ...` and exits 1; a failed result exits 1 after the per-file table; a failed precondition prints `Error: ...` and exits 1.

## Pre-commit Hook Compatibility

vibe_heal automatically handles projects that use [pre-commit](https://pre-commit.com/) hooks (e.g. `ruff-format`, `ruff-check --fix`).

### The Problem

Hooks like `ruff-format` that auto-fix staged files exit with a non-zero code, which would normally abort the commit and leave orphaned staged changes — interrupting batch commands like `cleanup` and `dedupe-branch`.

### How vibe_heal Solves It

Before calling `git commit`, vibe_heal runs your pre-commit hooks itself, detects any files they modified, and re-stages those files. By the time the actual commit runs, all hook-modified files are already staged, so hooks exit 0 and the commit succeeds.

```
Stage AI-fixed files
      ↓
Run pre-commit hooks manually
      ↓
Re-stage hook-modified files (e.g. ruff-formatted output)
      ↓
git commit  ← hooks run again here, but files are clean → exit 0
```

### Hook Runner Priority

vibe_heal selects the hook runner in this order:

1. **Custom command** — `PRE_COMMIT_COMMAND` in `.env.vibeheal`
2. **`pre-commit` CLI** — if `pre-commit` is on your PATH: `pre-commit run --files <files>`
3. **Native git fallback** — `git hook run --ignore-missing pre-commit` (safe even if no hook exists)

### Configuration Options

```bash
# Auto-detect (default) — uses pre-commit CLI if installed, otherwise native git
# PRE_COMMIT_COMMAND is unset

# Disable hooks entirely (skip all pre-commit processing)
PRE_COMMIT_COMMAND=

# Use a custom hook runner
PRE_COMMIT_COMMAND=my-hook-runner --check
```

### If Commits Still Fail Due to Hooks

vibe_heal includes a one-retry safety net: if `git commit` fails, it re-stages any files modified during the commit attempt and tries once more. If the second attempt also fails, a `GitOperationError` is raised with details.

If you're still seeing failures, try disabling hooks temporarily to isolate the issue:

```bash
PRE_COMMIT_COMMAND=  # in .env.vibeheal
```

## Troubleshooting

### Issue: "sonar-scanner is not installed or not in PATH"

**Solution**: Install sonar-scanner CLI

```bash
# macOS
brew install sonar-scanner

# Linux - download from official site
wget https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-5.0.1.3006-linux.zip
unzip sonar-scanner-cli-5.0.1.3006-linux.zip
sudo mv sonar-scanner-5.0.1.3006-linux /opt/sonar-scanner
sudo ln -s /opt/sonar-scanner/bin/sonar-scanner /usr/local/bin/sonar-scanner
```

### Issue: "No AI tool found"

**Solution**: Install an AI tool

```bash
# Option A: Install Claude Code
# See https://docs.claude.com/claude-code

# Option B: Install opencode
# See https://opencode.ai
```

### Issue: "Authentication failed. Check your credentials"

**Solution**: Verify your SonarQube token

```bash
# Test token manually
curl -u "your_token:" https://sonar.example.com/api/system/status

# If using username/password
curl -u "username:password" https://sonar.example.com/api/system/status
```

### Issue: "API request failed: Project already exists"

**Cause**: A previous cleanup didn't finish properly and left a temp project

**Solution**: Manually delete the temp project

1. Go to SonarQube web UI → Administration → Projects
2. Search for projects with your email and branch name
3. Delete the orphaned project

Or use the API:

```bash
# List projects
curl -u "your_token:" "https://sonar.example.com/api/projects/search"

# Delete specific project
curl -u "your_token:" -X POST "https://sonar.example.com/api/projects/delete?project=PROJECT_KEY"
```

### Issue: "Analysis timed out or failed on server"

**Possible causes**:
- Large project taking too long
- SonarQube server is busy
- Network issues

**Solutions**:
1. Check SonarQube server status
2. Try cleaning up fewer files with `--pattern`
3. Check SonarQube server logs for errors
4. Increase the scanner timeout by setting `SCANNER_TIMEOUT_SECONDS` in `.env.vibeheal` (default 300)

### Issue: Files not being detected

**Cause**: Modified files not in git

**Solution**: Ensure files are tracked by git

```bash
# Check what git sees as modified
git diff --name-only origin/main...HEAD

# Add files to git
git add <files>
git commit -m "WIP: changes to clean up"

# Then run cleanup
vibe_heal cleanup
```

### Issue: Max iterations reached but issues still remain

**Possible causes**:
- Issues are not actually fixable by AI
- AI tool is having trouble understanding the issue
- Complex issues requiring manual intervention

**Solutions**:
1. Increase max iterations: `--max-iterations 20`
2. Check which specific issues remain:
   ```bash
   vibe_heal fix src/file.py --dry-run
   ```
3. Fix manually and commit
4. Review the issue in SonarQube to understand why it can't be fixed

## Best Practices

### 1. Run cleanup regularly

```bash
# After implementing a feature
vibe_heal cleanup

# Before creating a PR
vibe_heal cleanup

# After rebasing
vibe_heal cleanup
```

### 2. Use file patterns for large branches

For branches with many modified files, clean up incrementally:

```bash
# First, clean up backend
vibe_heal cleanup --pattern "src/backend/**/*.py"

# Then frontend
vibe_heal cleanup --pattern "src/frontend/**/*.ts"

# Finally, tests
vibe_heal cleanup --pattern "tests/**/*.py"
```

### 3. Review commits after cleanup

```bash
# After cleanup completes
git log --oneline -20

# Review changes
git diff HEAD~10..HEAD

# If needed, squash cleanup commits
git rebase -i origin/main
```

### 4. Test after cleanup

```bash
# Run tests to ensure nothing broke
make test

# Run linters
make check

# Manual testing of affected features
```

### 5. Use in CI/CD

See [CI/CD Integration](#cicd-integration) section below.

## Advanced Usage

### Cleanup specific files only

While `cleanup` is designed for all modified files, you can combine patterns to target specific files:

```bash
# Only files in src/api/
vibe_heal cleanup --pattern "src/api/**/*.py"

# Only test files
vibe_heal cleanup --pattern "tests/**/*.py"

# Multiple patterns
vibe_heal cleanup --pattern "src/**/*.py" --pattern "tests/**/*.py"
```

### Dry-run equivalent

There's no dry-run for `cleanup` command, but you can:

1. Run analysis manually:
   ```bash
   # For each modified file
   vibe_heal fix src/file.py --dry-run
   ```

2. Check what would be cleaned:
   ```bash
   # See modified files
   git diff --name-only origin/main...HEAD
   ```

### Cleanup after rebase

```bash
# After rebasing
git rebase origin/main

# Fix any new issues introduced
vibe_heal cleanup
```

## CI/CD Integration

### GitHub Actions

```yaml
name: Branch Cleanup

on:
  pull_request:
    types: [opened, synchronize]

jobs:
  cleanup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
        with:
          fetch-depth: 0  # Need full history for branch comparison

      - name: Install sonar-scanner
        run: |
          wget https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-5.0.1.3006-linux.zip
          unzip sonar-scanner-cli-5.0.1.3006-linux.zip
          sudo mv sonar-scanner-5.0.1.3006-linux /opt/sonar-scanner
          sudo ln -s /opt/sonar-scanner/bin/sonar-scanner /usr/local/bin/sonar-scanner

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'

      - name: Install vibe_heal
        run: pip install git+https://github.com/alexeieleusis/dotharness

      - name: Create config
        run: |
          cat > .env.vibeheal <<EOF
          SONARQUBE_URL=${{ secrets.SONARQUBE_URL }}
          SONARQUBE_TOKEN=${{ secrets.SONARQUBE_TOKEN }}
          SONARQUBE_PROJECT_KEY=${{ secrets.SONARQUBE_PROJECT_KEY }}
          AI_TOOL=claude-code
          EOF

      - name: Run cleanup
        run: vibe_heal cleanup --base-branch origin/${{ github.base_ref }}

      - name: Push changes
        run: |
          git config user.name "vibe_heal[bot]"
          git config user.email "vibe_heal[bot]@users.noreply.github.com"
          git push
```

### GitLab CI

```yaml
cleanup:
  stage: quality
  image: python:3.11
  before_script:
    - apt-get update && apt-get install -y wget unzip
    - wget https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-5.0.1.3006-linux.zip
    - unzip sonar-scanner-cli-5.0.1.3006-linux.zip
    - mv sonar-scanner-5.0.1.3006-linux /opt/sonar-scanner
    - ln -s /opt/sonar-scanner/bin/sonar-scanner /usr/local/bin/sonar-scanner
    - pip install git+https://github.com/alexeieleusis/dotharness
  script:
    - |
      cat > .env.vibeheal <<EOF
      SONARQUBE_URL=${SONARQUBE_URL}
      SONARQUBE_TOKEN=${SONARQUBE_TOKEN}
      SONARQUBE_PROJECT_KEY=${SONARQUBE_PROJECT_KEY}
      AI_TOOL=claude-code
      EOF
    - vibe_heal cleanup --base-branch origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME
    - git push origin HEAD:$CI_COMMIT_REF_NAME
  only:
    - merge_requests
```

## Limitations

1. **Requires sonar-scanner**: Must have sonar-scanner CLI installed
2. **Network access**: Needs access to SonarQube server
3. **Project permissions**: Needs permission to create/delete projects
4. **AI tool availability**: Requires Claude Code or opencode installed
5. **Git state**: Files must be committed to git to be detected
6. **Iterative approach**: May not fix all issues in one pass (hence max iterations)
7. **No rollback**: No built-in rollback mechanism (use git reset)

## FAQ

### Q: Can I run cleanup without committing?

**A**: No, cleanup is designed to create commits for each fix. If you want to preview without committing, use `vibe_heal fix <file> --dry-run` on individual files first.

### Q: How many iterations are enough?

**A**: Default is 10, which should handle most cases. If issues persist:
- Increase to 15-20 for complex issues
- Check if issues are actually fixable
- Review remaining issues manually

### Q: Can I cancel cleanup mid-way?

**A**: Yes, press Ctrl+C. The temporary project will be cleaned up, and commits made so far will remain.

### Q: What happens if my internet connection drops?

**A**: The cleanup will fail, but:
- Commits made before the failure remain
- Temporary project should be cleaned up in finally block
- If temp project remains, manually delete it (see Troubleshooting)

### Q: Can I use cleanup on main branch?

**A**: Technically yes, but **not recommended**. Cleanup is designed for feature branches before merging to main.

### Q: Does cleanup affect my working directory?

**A**: Yes, it modifies files and creates commits. Ensure you don't have uncommitted changes you care about.

### Q: Can I customize the commit messages?

**A**: Not currently. Commit messages follow a standard format. Feature request: #TBD

## See Also

- [Architecture Documentation](architecture.md)
- [Review Guide](review-guide.md) (`review --post` to post findings on a PR)
- [CI/CD Integration Examples](#cicd-integration)
- [Project Home](../vibe_heal.md)
