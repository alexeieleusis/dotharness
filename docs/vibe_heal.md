# vibe_heal

AI-powered SonarQube issue remediation that automatically fixes your code quality problems using
Claude Code or opencode. `vibe_heal` is one of the tools in `dotharness`. It was migrated from the
standalone vibe-heal repository in I-2 (#164). See the [migration ledger](migration-ledger.md) for
module status.

!!! note "Removed in I-2"
    The migration removed the Aider and Gemini CLI backends. The AI backends are now
    `claude-code` and `opencode`.

## Pages

- [Overview](vibe_heal/overview.md): features and the command table
- [Branch cleanup guide](vibe_heal/branch-cleanup-guide.md): the `cleanup`, `dedupe-branch` and `cleanup-pr` commands
- [Review guide](vibe_heal/review-guide.md): the `review` command and GitHub PR commenting
- [Architecture](vibe_heal/architecture.md): system design and module structure
- [API reference](vibe_heal/api.md): generated from the `dotharness.vibe_heal` docstrings

## Quick start

### 1. Install

```bash
git clone https://github.com/alexeieleusis/dotharness.git
cd dotharness
uv sync
```

This installs the `vibe_heal` command. You can also run `uv run python -m dotharness.vibe_heal`.

### 2. Install an AI tool

Install either [Claude Code](https://docs.claude.com/claude-code) or
[opencode](https://opencode.ai). `vibe_heal` auto-detects them (Claude Code first, then opencode).
Pick one explicitly with `--ai-tool claude-code` or `--ai-tool opencode`, or `AI_TOOL=` in the
environment file.

### 3. Configure the SonarQube connection

Create `.env.vibeheal` (or copy the [example environment file](https://github.com/alexeieleusis/dotharness/blob/main/.env.vibeheal.example)):

```bash
cat > .env.vibeheal <<EOF
SONARQUBE_URL=https://sonar.example.com
SONARQUBE_TOKEN=your_token_here
SONARQUBE_PROJECT_KEY=your_project_key
EOF
```

By default `vibe_heal` reads `.env.vibeheal` or `.env` in the current directory. Use `--env-file` for
another file:

```bash
vibe_heal config --env-file .env.staging
```

### 4. Fix issues and remove duplications

```bash
# Single file
vibe_heal fix src/main.py --dry-run
vibe_heal fix src/main.py --max-issues 1
vibe_heal fix src/main.py --min-severity MAJOR

# Duplications in a single file
vibe_heal dedupe src/main.py --max-duplications 5

# Whole branch (recommended before code review)
vibe_heal cleanup
vibe_heal cleanup --base-branch develop --pattern "*.py"
vibe_heal dedupe-branch --max-iterations 20

# Only changed lines of a PR
vibe_heal cleanup-pr

# Review changed lines without fixing; post inline comments on the PR
vibe_heal review
vibe_heal review --coverage
vibe_heal review --post --dry-run
```

## Configuration

Settings come from the environment file:

```bash
# SonarQube configuration (required)
SONARQUBE_URL=https://sonarqube.example.com
SONARQUBE_TOKEN=your_token_here
# OR username/password (token is preferred)
# SONARQUBE_USERNAME=your_username
# SONARQUBE_PASSWORD=your_password
SONARQUBE_PROJECT_KEY=your_project_key

# AI tool (optional; auto-detected if not set)
# AI_TOOL=claude-code
# AI_TOOL=opencode

# Context enrichment (optional)
# CODE_CONTEXT_LINES=5
# INCLUDE_RULE_DESCRIPTION=true
```

## sonar-project.properties support

If your repository already has a `sonar-project.properties` file, `vibe_heal` respects it
automatically.

- **Minimal command**: only appends auth and/or host-URL flags that are not already configured in the file or in environment variables (`SONAR_TOKEN`, `SONARQUBE_TOKEN`, `SONAR_LOGIN`, `SONAR_HOST_URL`, `SONARQUBE_HOST_URL`). All other scanner settings are preserved.
- **Temp-project patching**: commands that create a temporary SonarQube project (`cleanup`, `dedupe-branch`, `review`) temporarily replace `sonar.projectKey` and `sonar.projectName` in the file during the analysis. The commands restore the original content after the analysis. `vibe_heal` writes a recovery comment block first so you can restore it manually if the process is interrupted.

If the file does not exist, `vibe_heal` passes all scanner settings as `-D` flags on the command line.

## Development

```bash
uv run python -m pytest tests/vibe_heal
uv run pre-commit run -a
```
