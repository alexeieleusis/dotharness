# Review Guide

## Overview

The `vibe_heal review` command analyzes SonarQube issues **scoped to the lines you actually changed** in your branch. It is a read-only companion to `cleanup`: it reports issues instead of fixing them, and can post the findings as inline GitHub PR review comments.

The workflow has two phases, plus a third standalone mode for CI baseline maintenance:

1. **Analyze** (`vibe_heal review`) — create a temporary SonarQube project, run `sonar-scanner`, filter issues to changed lines, and save a `review.json` + `review.md` report.
2. **Post** (`vibe_heal review --post`) — load the saved report and post inline comments to an open GitHub PR via the `gh` CLI.
3. **Baseline** (`vibe_heal review --baseline`) — scan the real SonarQube project directly (no temp project, no diff, no report) to refresh its analysis. See [Baseline Mode](#baseline-mode).

## When to Use

- Before opening a pull request — catch issues on new/modified lines only (no noise from pre-existing issues).
- During code review — share a structured issue report with reviewers automatically.
- In CI/CD — gate a PR on new SonarQube issues introduced in the diff.

## Prerequisites

### Required for `review` (analyze phase)

1. **sonar-scanner CLI** must be installed
   ```bash
   # macOS
   brew install sonar-scanner

   # Linux — download from https://docs.sonarsource.com/sonarqube/latest/analyzing-source-code/scanners/sonarscanner/
   ```

2. **SonarQube server** with API access and permission to create/delete projects.

3. **git repository** with at least one commit and a reachable base branch.

### Required for `review --post` (post phase)

4. **gh CLI** installed and authenticated
   ```bash
   # macOS
   brew install gh
   gh auth login
   ```

5. An open GitHub Pull Request for the current branch.

### Configuration

Create `.env.vibeheal`:

```bash
# Required
SONARQUBE_URL=https://sonar.example.com
SONARQUBE_TOKEN=your_token_here
SONARQUBE_PROJECT_KEY=your_project_key

# Optional — enriches inline PR comments with rule documentation
INCLUDE_RULE_DESCRIPTION=true  # default: true
```

No AI tool configuration is required for the review command — it only reads, never writes.

## sonar-project.properties Support

If your project has a `sonar-project.properties` file, vibe_heal uses it automatically. vibe_heal appends only those auth or host-URL flags to the `sonar-scanner` command that are absent from both the file and the environment. All other scanner settings (exclusions, language plugins, coverage paths, etc.) are preserved as-is.

For the temporary SonarQube project created during `review`, vibe_heal patches `sonar.projectKey` and `sonar.projectName` in the file while the scanner runs, then restores the originals. vibe_heal writes a recovery comment first so you can restore manually if the process is interrupted. See the [Branch Cleanup Guide](branch-cleanup-guide.md#sonar-projectproperties-support) for full details.

## Basic Usage

### Analyze changed lines

```bash
# Auto-detects the current PR's base branch via gh (falls back to origin/main if no open PR is found)
vibe_heal review

# Compare against a different base branch
vibe_heal review --base-branch origin/develop

# Limit to specific file patterns
vibe_heal review --pattern "src/**/*.py"

# Include test coverage for changed lines (requires coverage data in SonarQube)
vibe_heal review --coverage

# Save report to a custom path
vibe_heal review --report-file /tmp/my-review.json
```

### Refresh SonarQube's baseline (CI only)

```bash
# Run this on a checkout of main to refresh the real project's SonarQube analysis
vibe_heal review --baseline
```

See [Baseline Mode](#baseline-mode) for when and why to use this.

### Post findings to GitHub PR

```bash
# Post saved report as inline PR comments (auto-detects open PR)
vibe_heal review --post

# Preview what would be posted without making API calls
vibe_heal review --post --dry-run

# Specify an explicit PR number
vibe_heal review --post --pr 42

# Load a report from a custom path
vibe_heal review --post --report-file /tmp/my-review.json
```

### Full flags reference

| Flag | Default | Description |
|---|---|---|
| `--base-branch` / `-b` | auto-detect via `gh`, else `origin/main` | Base branch to compare against. When omitted, auto-detects the current branch's open PR base via `gh pr view` and diffs against `origin/<that>`. Falls back to `origin/main` if no open PR is found or `gh` is unavailable. Passing an explicit value skips detection. |
| `--pattern` / `-p` | (all files) | Glob pattern to filter files (repeatable) |
| `--report-file` | auto | Override report output path |
| `--env-file` | `.env.vibeheal` | Path to custom config file |
| `--verbose` / `-v` | off | Enable debug logging |
| `--coverage` | off | Report test coverage for changed lines (requires coverage data in SonarQube) |
| `--post` | off | Post saved report to GitHub PR instead of analyzing |
| `--dry-run` | off | With `--post`: preview comments without API calls |
| `--pr` | auto | With `--post`: explicit GitHub PR number |
| `--baseline` | off | Scan the real SonarQube project directly to refresh its baseline (no diff, no report). Ignores `--pr`, `--pattern`, `--coverage`, and `--base-branch` if combined. |

### Stacked PRs

If your branch's PR targets another branch instead of `main` (a "stacked" PR), `review` diffs against that real base automatically — no flag needed. This avoids re-reporting issues that belong to the branch below yours in the stack. Those issues would otherwise appear twice (once on each branch's review) and increase the odds of merge conflicts as both branches "fix" the same lines independently.

This requires an open PR (so `gh pr view` can report its base) and a local `origin/<base>` remote-tracking ref. If the base branch hasn't been fetched locally, the diff will fail with a "branch not found" error rather than silently falling back to `origin/main`. Run `git fetch origin <base-branch>` first if you hit this.

Pass `--base-branch` explicitly to skip auto-detection entirely (e.g. in CI, or before a PR exists).

## How It Works

### Analyze phase

```
1. Detect modified files vs base branch (git three-dot diff)
2. Parse diff to identify changed (new) line numbers per file
3. Create temporary SonarQube project
4. Run sonar-scanner against modified files
5. Fetch issues from temporary project
6. Filter issues to lines that appear in the diff (+ 3-line trailing context)
7. Fetch active duplication blocks intersecting changed lines
8. Detect resolved duplications: blocks that existed on the base branch but
   are no longer active — warns if only one copy was deduplicated
9. Optionally enrich each issue with rule root_cause documentation
10. If --coverage: fetch lineHits data per changed line from SonarQube and
    compute coverage_pct/covered_lines/instrumented_changed_lines per file
11. Write report to ~/.vibe-heal/reviews/<project-key>/<branch>/review.json
    and review.md
12. Delete temporary SonarQube project
13. Display per-file summary table in terminal (with Coverage column when applicable)
```

### Post phase

```
1. Load review.json from default path (or --report-file)
2. Detect open PR via `gh pr list` (or use --pr)
3. Build GitHub Reviews API payload with one inline comment per finding
4. Post review to GitHub PR
```

The two phases are decoupled — you can run `review` in CI and `review --post` locally, or re-post an old report to a new PR.

## Output

### Terminal output (analyze phase)

Without `--coverage`:
```
Review Summary:
  Branch: feature/new-api (base: origin/main)
  Files checked: 4
  Total issues: 7
  Duplication findings: 1

Per-File Breakdown:
  File                      Issues  Highest Severity  Duplications
  src/api/users.py               3          CRITICAL             0
  src/api/auth.py                2             MAJOR             1
  src/models/user.py             2             MINOR             0
  tests/test_api.py              0               N/A             0

Report saved to /Users/you/.vibe-heal/reviews/my-project/feature-new-api/review.json
```

With `--coverage` (adds a Coverage column when SonarQube has coverage data):
```
Per-File Breakdown:
  File                      Issues  Highest Severity  Duplications  Coverage
  src/api/users.py               3          CRITICAL             0     66.7%
  src/api/auth.py                2             MAJOR             1     50.0%
  src/models/user.py             2             MINOR             0      100%
  tests/test_api.py              0               N/A             0        —
```

The Coverage column shows the percentage of **instrumented changed lines** (lines SonarQube has hit-count data for) that were covered by at least one test. The calculation excludes lines with no instrumentation data (e.g. comments, blank lines, or files not processed by the coverage tool). `—` means no coverage data was available for that file.

### Report files

Two files are written after each analysis:

- **`review.json`** — machine-readable report. It includes `root_cause` HTML per issue so `--post` can include rule docs without re-fetching. It also stores `coverage_pct`, `covered_lines`, and `instrumented_changed_lines` per file when `--coverage` was used.
- **`review.md`** — human-readable Markdown with issues per file and one collapsed `<details>` block per unique rule (deduped). When coverage data is present, each file section opens with a bold coverage line (e.g. `**Coverage on changed lines: 66.7% (4/6 instrumented lines covered)**`).

Default report directory: `~/.vibe-heal/reviews/<project-key>/<branch>/`

### Inline PR comments (post phase)

Each issue becomes an inline comment anchored to the relevant line. Comments include:

- Issue message and severity
- Rule key and link to SonarQube rule documentation
- A collapsed `<details>` block with the rule's `root_cause` HTML (when `INCLUDE_RULE_DESCRIPTION=true`)

Duplication findings appear as comments noting the other locations where the same code exists.

Resolved duplications (code that was duplicated on the base branch but is no longer duplicated in your branch — possibly meaning you fixed only one copy) appear with a warning to check the remaining copies.

When the report contains coverage data (i.e. you ran `review` with `--coverage`), the PR review body includes a **Coverage on changed lines** section listing each file's coverage percentage and instrumented line counts.

## Coverage Reporting

When `--coverage` is passed, vibe_heal fetches SonarQube's `lineHits` data for each changed file and computes coverage metrics scoped to the lines you actually changed.

### What is counted

- **Instrumented line** — a changed line for which SonarQube has hit-count data (i.e. the coverage tool processed the line during test execution). Comments, blank lines, and lines outside the coverage tool's scope are not instrumented.
- **Covered line** — an instrumented line with `lineHits > 0` (executed at least once during testing).
- **`coverage_pct`** — `covered_lines / instrumented_changed_lines × 100`, rounded to one decimal place.

### Prerequisites

Coverage data must be present in SonarQube. This typically means:

1. Running your test suite with a coverage tool (e.g. `pytest --cov`, `jest --coverage`, `jacoco`) and generating a coverage report.
2. Passing the coverage report path to `sonar-scanner` (e.g. `sonar.python.coverage.reportPaths=coverage.xml`) before vibe_heal runs its analysis.

If SonarQube has no coverage data for a file, `--coverage` shows `—` for that file when the Coverage column is present, and omits the file from the Coverage section in PR/Markdown output. With `--verbose`, vibe_heal prints the note: `no coverage data available`.

### Example

```bash
# Run tests and generate coverage first
uv run pytest --cov --cov-report=xml

# Then review with coverage
vibe_heal review --coverage
```

## Duplication Findings

The review command reports two types of duplication findings:

**Active duplications** — code in your changed lines that SonarQube still considers duplicated. The comment shows the other locations where the same block exists.

**Resolved duplications** — code that was duplicated on the base branch and intersects your changed lines, but is no longer flagged as duplicated in your branch. This typically means you refactored one copy of a duplicated block without updating the others. The comment warns you to check the remaining copies.

## Baseline Mode

`vibe_heal review --baseline` refreshes the real SonarQube project's analysis directly. It exists for CI harnesses that need SonarQube's baseline for `main` to stay current between PR review cycles, so later PR reviews get accurate duplicate-code comparisons. The "resolved duplications" check queries the real project's most recent analysis, not a temp project (see [Duplication Findings](#duplication-findings)).

### Why it's a separate mode

The normal `review` analyze phase (see [How It Works](#how-it-works)) always scans a **temporary, disposable SonarQube project** and deletes it afterward — it never re-analyzes the real project. If you run `review` on a checkout of `main` with no diff against the base branch, it short-circuits with "No modified files to review" and no scan runs at all, so the real project's SonarQube analysis goes stale.

`--baseline` bypasses all of that: it computes no diff, creates no temporary project, and writes no report. It runs `sonar-scanner` directly against `config.sonarqube_project_key` — the same thing a normal CI build of `main` would do.

`--baseline` is **explicit-only**. It does not trigger automatically when a diff happens to be empty. That would risk overwriting the real project's SonarQube baseline with a developer's local working-tree state during ordinary interactive use (e.g. running `vibe_heal review` on a feature branch with no committed diff yet, but uncommitted local edits).

### Usage

```bash
# On a clean checkout of main
git checkout main && git pull

vibe_heal review --baseline
```

`--baseline` ignores `--pr`, `--pattern`, `--coverage`, and `--base-branch` if they're passed alongside it — none of that diff/filtering machinery applies. The exit code is `0` on a successful scan, non-zero on failure (scanner not found, analysis error, etc.). A CI harness can gate on it.

### Output

```
Baseline scan complete: project my-project re-analyzed.
Dashboard: https://sonar.example.com/dashboard?id=my-project
```

### CI example

```yaml
name: Refresh SonarQube baseline

on:
  push:
    branches: [main]

jobs:
  baseline:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install sonar-scanner
        run: |
          wget -q https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-6.2.1.4610-linux-x64.zip
          unzip -q sonar-scanner-cli-6.2.1.4610-linux-x64.zip
          sudo mv sonar-scanner-6.2.1.4610-linux-x64 /opt/sonar-scanner
          sudo ln -s /opt/sonar-scanner/bin/sonar-scanner /usr/local/bin/sonar-scanner
      - name: Install vibe_heal
        run: pip install git+https://github.com/alexeieleusis/dotharness
      - name: Create config
        run: |
          cat > .env.vibeheal <<EOF
          SONARQUBE_URL=${{ secrets.SONARQUBE_URL }}
          SONARQUBE_TOKEN=${{ secrets.SONARQUBE_TOKEN }}
          SONARQUBE_PROJECT_KEY=${{ secrets.SONARQUBE_PROJECT_KEY }}
          EOF
      - name: Refresh SonarQube baseline
        run: vibe_heal review --baseline
```

## Examples

### Example 1: Pre-PR review

```bash
# On feature/payment-redesign
git checkout feature/payment-redesign

# Analyze new issues on changed lines
vibe_heal review

# Open a PR, then post findings inline
gh pr create --title "feat: payment redesign"
vibe_heal review --post
```

### Example 2: Analyze and post in one step (CI)

```bash
# In GitHub Actions
vibe_heal review --base-branch origin/${{ github.base_ref }}
vibe_heal review --post --pr ${{ github.event.pull_request.number }}
```

### Example 3: Preview before posting

```bash
# See what comments would be posted
vibe_heal review --post --dry-run
```

Output:
```
[dry-run] Would post 3 inline comment(s) to PR #42:

  src/api/users.py:87
  **CRITICAL** python:S3776 — Refactor this function to reduce its Cognitive Complexity from 16 to the 15 allowed.
  ...
```

### Example 4: Use a custom base branch and file filter

```bash
vibe_heal review \
  --base-branch origin/develop \
  --pattern "src/**/*.py" \
  --pattern "tests/**/*.py"
```

## CI/CD Integration

### GitHub Actions

```yaml
name: SonarQube Review

on:
  pull_request:

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Install sonar-scanner
        run: |
          wget -q https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-6.2.1.4610-linux-x64.zip
          unzip -q sonar-scanner-cli-6.2.1.4610-linux-x64.zip
          sudo mv sonar-scanner-6.2.1.4610-linux-x64 /opt/sonar-scanner
          sudo ln -s /opt/sonar-scanner/bin/sonar-scanner /usr/local/bin/sonar-scanner

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install vibe_heal
        run: pip install git+https://github.com/alexeieleusis/dotharness

      - name: Create config
        run: |
          cat > .env.vibeheal <<EOF
          SONARQUBE_URL=${{ secrets.SONARQUBE_URL }}
          SONARQUBE_TOKEN=${{ secrets.SONARQUBE_TOKEN }}
          SONARQUBE_PROJECT_KEY=${{ secrets.SONARQUBE_PROJECT_KEY }}
          EOF

      - name: Analyze changed lines
        run: vibe_heal review --base-branch origin/${{ github.base_ref }}

      - name: Post review comments
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: vibe_heal review --post --pr ${{ github.event.pull_request.number }}
```

## Limitations

1. **Requires sonar-scanner**: Must be installed and on PATH.
2. **Requires SonarQube project permissions**: Create and delete projects.
3. **Requires gh CLI for `--post`**: Must be installed and authenticated.
4. **Open PR required for `--post`**: Auto-detection needs an open PR. Use `--pr` to override.
5. **SonarQube `isNew` flag**: The `isNew` field on SonarQube issues is unreliable for branches. vibe_heal uses its own diff-based line filter instead.
6. **Line filter trailing context**: Changed-line clusters expand by 3 lines of trailing context, which may occasionally include issues on lines just after your edits.

## Troubleshooting

### "gh: command not found"

Install the GitHub CLI: <https://cli.github.com/>

```bash
brew install gh
gh auth login
```

### "No open PR found for branch"

Either open a PR first, or use `--pr <number>` to specify one explicitly:

```bash
vibe_heal review --post --pr 42
```

### Report file not found when running `--post`

If you ran `review` with a custom `--report-file`, pass the same path to `--post`:

```bash
vibe_heal review --report-file /tmp/review.json
vibe_heal review --post --report-file /tmp/review.json
```

### "sonar-scanner is not installed or not in PATH"

See the [Branch Cleanup Guide troubleshooting section](branch-cleanup-guide.md#issue-sonar-scanner-is-not-installed-or-not-in-path) for installation instructions.

### Coverage column not shown despite using `--coverage`

Coverage data must be uploaded to SonarQube before running `vibe_heal review`. Make sure you:

1. Run your test suite with coverage enabled and generate a report (e.g. `coverage.xml`).
2. Verify that your `sonar-project.properties` or `sonar-scanner` flags include the coverage report path (e.g. `sonar.python.coverage.reportPaths=coverage.xml`).
3. Run `vibe_heal review --coverage --verbose` — if coverage is missing, vibe_heal prints `no coverage data (run tests before sonar-scanner)` for each affected file.

### No issues reported despite known violations

The review command only reports issues on **changed lines**. Issues on lines you did not touch will not appear. To see all issues for a file:

```bash
vibe_heal fix src/file.py --dry-run
```

## See Also

- [Branch Cleanup Guide](branch-cleanup-guide.md) — automatically fix issues before PR
- [Architecture Documentation](architecture.md)
- [Project Home](../vibe_heal.md)
