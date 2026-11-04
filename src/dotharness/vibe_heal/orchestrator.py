"""Main workflow orchestration for vibe_heal."""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn

from dotharness.vibe_heal.ai_tools import AIToolFactory
from dotharness.vibe_heal.ai_tools.base import AITool, AIToolType
from dotharness.vibe_heal.ai_tools.external_docs import fetch_external_rule_docs, fetch_vibe_types_knowledge_docs
from dotharness.vibe_heal.ai_tools.models import FixResult
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.git import GitManager
from dotharness.vibe_heal.models import FixSummary
from dotharness.vibe_heal.output import console, cyan, info, success, warn
from dotharness.vibe_heal.processor import IssueProcessor
from dotharness.vibe_heal.sonarqube import ComponentNotFoundError, SonarQubeClient
from dotharness.vibe_heal.sonarqube.exceptions import SonarQubeRuleNotFoundError
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue, SonarQubeRule
from dotharness.vibe_heal.utils import display_fix_summary

logger = logging.getLogger(__name__)


class VibeHealOrchestrator:
    """Orchestrates the fix workflow."""

    def __init__(self, config: VibeHealConfig, ai_tool: AITool | None = None) -> None:
        """Initialize orchestrator.

        Args:
            config: Application configuration
            ai_tool: Optional pre-initialized AI tool. If not provided, will auto-detect.
        """
        self.config = config
        self.git_manager = GitManager(
            pre_commit_command=config.pre_commit_command,
            pre_commit_timeout=config.pre_commit_timeout_seconds,
        )
        self.ai_tool = ai_tool if ai_tool is not None else self._initialize_ai_tool()

    def _initialize_ai_tool(self) -> AITool:
        """Initialize AI tool based on config or auto-detect.

        Returns:
            Initialized AI tool

        Raises:
            RuntimeError: If no AI tool is available
        """
        if self.config.ai_tool:
            tool_type: AIToolType = self.config.ai_tool
            info(f"Using configured AI tool: {tool_type.display_name}")
        else:
            detected_tool = AIToolFactory.detect_available()
            if not detected_tool:
                msg = "No AI tool found. Please install Claude Code or opencode."
                raise RuntimeError(msg)
            tool_type = detected_tool
            info(f"Auto-detected AI tool: {tool_type.display_name}")

        return AIToolFactory.create(tool_type, self.config)

    async def fix_file(
        self,
        file_path: str,
        dry_run: bool = False,
        max_issues: int | None = None,
        min_severity: str | None = None,
        issue_filter: Callable[[SonarQubeIssue], bool] | None = None,
    ) -> FixSummary:
        """Fix SonarQube issues in a file.

        Args:
            file_path: Path to file to fix
            dry_run: If True, don't commit changes
            max_issues: Maximum number of issues to fix
            min_severity: Minimum severity to process
            issue_filter: Optional predicate; when set, only fetched issues for which it
                returns True are passed to the processor

        Returns:
            Summary of fixes

        Raises:
            RuntimeError: If not a Git repository, file has uncommitted changes, or AI tool unavailable
            FileNotFoundError: If the specified file does not exist
        """
        # Step 1: Validate preconditions
        self._validate_preconditions(file_path, dry_run)

        # Step 2: Fetch issues from SonarQube
        info(f"Fetching issues for {file_path}...")
        try:
            async with SonarQubeClient(self.config) as sonar_client:
                issues = await sonar_client.get_issues_for_file(file_path)
        except ComponentNotFoundError:
            warn(f"File '{file_path}' not found in SonarQube project. It may not be included in the analysis sources.")
            return FixSummary(total_issues=0)

        if not issues:
            success("No issues found!")
            return FixSummary(total_issues=0)

        # Apply the optional caller-supplied issue filter before processing
        if issue_filter is not None:
            issues = [i for i in issues if issue_filter(i)]

        # Step 3: Process issues (filter and sort)
        processor = IssueProcessor(
            min_severity=min_severity,
            max_issues=max_issues,
        )
        result = processor.process(issues)

        cyan(
            f"Found {result.total_issues} total issues, "
            f"{result.fixable_issues} fixable, "
            f"processing {len(result.issues_to_fix)}\n"
        )

        if not result.has_issues:
            warn("No fixable issues to process")
            return FixSummary(
                total_issues=result.total_issues,
                skipped=result.skipped_issues,
            )

        # Step 4: Process each issue
        summary = await self._process_issues(
            result.issues_to_fix,
            file_path,
            dry_run,
        )
        summary.total_issues = result.total_issues
        summary.skipped = result.skipped_issues

        # Step 5: Display summary
        self._display_summary(summary, dry_run)

        return summary

    def _validate_preconditions(self, file_path: str, dry_run: bool) -> None:
        """Validate preconditions before fixing.

        Args:
            file_path: Path to file
            dry_run: Whether in dry-run mode

        Raises:
            RuntimeError: If not a Git repository, working directory has uncommitted changes, or AI tool unavailable
            FileNotFoundError: If the specified file does not exist
        """
        # Check Git repository
        if not self.git_manager.is_repository():
            msg = "Not a Git repository"
            raise RuntimeError(msg)

        # Check file exists
        if not Path(file_path).exists():
            msg = f"File not found: {file_path}"
            raise FileNotFoundError(msg)

        # Check working directory is clean (no modified/staged files, untracked OK) unless dry-run
        if not dry_run:
            self.git_manager.require_clean_working_directory()

        # Check AI tool is available
        if not self.ai_tool.is_available():
            msg = f"{self.ai_tool.tool_type.display_name} is not available"
            raise RuntimeError(msg)

    async def _fetch_source_lines(self, file_path: str) -> list | None:
        """Fetch source code lines for a file.

        Args:
            file_path: Path to file

        Returns:
            List of source lines or None if fetch failed
        """
        if not self.config.include_rule_description:
            return None

        try:
            async with SonarQubeClient(self.config) as sonar_client:
                return await sonar_client.get_source_lines(file_path)
        except Exception as e:
            logger.warning(f"Failed to fetch source lines: {e}. Continuing without code context.")
            return None

    async def _fetch_rule_details(
        self,
        issue: SonarQubeIssue,
    ) -> tuple[SonarQubeRule | None, list[str] | None]:
        """Fetch rule details or fallback external docs for an issue.

        Runs the vibe-types knowledge-doc lookup concurrently with the rule lookup, since the
        two are independent. On success those docs are attached alongside the rule; a failure
        fetching them never discards an already-fetched rule.

        Args:
            issue: Issue to fetch rule details for

        Returns:
            Tuple of (rule, docs)
        """
        if not self.config.include_rule_description:
            return None, None

        async def _get_rule() -> SonarQubeRule:
            async with SonarQubeClient(self.config) as sonar_client:
                return await sonar_client.get_rule_details(issue.rule)

        rule_outcome, knowledge_outcome = await asyncio.gather(
            _get_rule(), fetch_vibe_types_knowledge_docs(issue.message), return_exceptions=True
        )

        if isinstance(rule_outcome, SonarQubeRuleNotFoundError):
            logger.debug("Rule %s not found in SonarQube; fetching docs from issue message URLs", issue.rule)
            if isinstance(knowledge_outcome, BaseException):
                docs = await fetch_external_rule_docs(issue.message)
            else:
                other_docs = await fetch_external_rule_docs(issue.message, exclude_vibe_types=True)
                docs = [*knowledge_outcome, *other_docs]
            return None, docs if docs else None
        if isinstance(rule_outcome, BaseException):
            logger.warning(f"Failed to fetch rule details for {issue.rule}: {rule_outcome}")
            return None, None

        if isinstance(knowledge_outcome, BaseException):
            logger.warning(f"Failed to fetch vibe-types knowledge docs for issue {issue.rule}: {knowledge_outcome}")
            return rule_outcome, None

        return rule_outcome, knowledge_outcome if knowledge_outcome else None

    def _extract_code_context(
        self,
        issue: SonarQubeIssue,
        source_lines: list,
    ) -> list | None:
        """Extract code context lines around an issue.

        Args:
            issue: Issue to extract context for
            source_lines: Source lines of the file

        Returns:
            List of context lines or None
        """
        if not issue.line:
            return None

        context_lines = self.config.code_context_lines
        start_line = max(1, issue.line - context_lines)
        end_line = issue.line + context_lines
        code_context = [line for line in source_lines if start_line <= line.line <= end_line]

        if not code_context:
            return None

        logger.debug("Code context for issue %s (line %s):", issue.key, issue.line)
        for source_line in code_context:
            marker = ">>>" if source_line.line == issue.line else "   "
            logger.debug("%s %d: %s", marker, source_line.line, source_line.plain_code)

        return code_context

    async def _fetch_enriched_context(
        self,
        issue: SonarQubeIssue,
        source_lines: list | None,
    ) -> tuple[SonarQubeRule | None, list | None, list[str] | None]:
        """Fetch enriched context for an issue.

        Args:
            issue: Issue to fetch context for
            source_lines: Pre-fetched source lines

        Returns:
            Tuple of (rule, code_context, external_docs)
        """
        rule, external_docs = await self._fetch_rule_details(issue)

        code_context = self._extract_code_context(issue, source_lines) if source_lines else None

        return rule, code_context, external_docs

    def _handle_fix_result(
        self,
        fix_result: FixResult,
        issue: SonarQubeIssue,
        dry_run: bool,
        summary: FixSummary,
        progress: Progress,
        task: TaskID,
        rule: SonarQubeRule | None,
    ) -> None:
        """Handle the result of a fix attempt.

        Args:
            fix_result: Result from AI tool
            issue: Issue that was fixed
            dry_run: Whether in dry-run mode
            summary: Summary to update
            progress: Progress bar
            task: Progress task
            rule: Rule details
        """
        if fix_result.success:
            # Commit if not dry-run
            if not dry_run:
                try:
                    # Pass None to auto-detect all modified files
                    sha = self.git_manager.commit_fix(
                        issue,
                        None,  # Auto-detect all modified files
                        self.ai_tool.tool_type,
                        rule=rule,
                    )
                    if sha:
                        # Commit was created
                        summary.commits.append(sha)
                        summary.fixed += 1
                        progress.update(
                            task,
                            description=f"[green]✓ Fixed line {issue.line}[/green]",
                        )
                    else:
                        # No commit created (issue was already fixed)
                        summary.skipped += 1
                        progress.update(
                            task,
                            description=f"[yellow]⊘ Line {issue.line} already fixed[/yellow]",
                        )
                        logger.info(f"Issue {issue.key} was already fixed by a previous commit")
                except Exception as e:
                    logger.exception("Failed to commit")
                    summary.failed += 1
                    progress.update(
                        task,
                        description=f"[red]✗ Commit failed: {e}[/red]",
                    )
            else:
                summary.fixed += 1
                progress.update(
                    task,
                    description=f"[green]✓ Would fix line {issue.line} (dry-run)[/green]",
                )
        else:
            summary.failed += 1
            error = fix_result.error_message or "Unknown error"
            progress.update(
                task,
                description=f"[red]✗ Failed: {error[:50]}[/red]",
            )
            logger.error(f"Failed to fix issue {issue.key}: {fix_result.error_message}")

    async def _process_issues(
        self,
        issues: list[SonarQubeIssue],
        file_path: str,
        dry_run: bool,
    ) -> FixSummary:
        """Process each issue.

        Args:
            issues: List of issues to process
            file_path: Path to file
            dry_run: Whether in dry-run mode

        Returns:
            Fix summary
        """
        summary = FixSummary(total_issues=0)
        source_lines = await self._fetch_source_lines(file_path)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            for idx, issue in enumerate(issues, 1):
                task = progress.add_task(
                    f"[cyan]Fixing issue {idx}/{len(issues)} (line {issue.line}): {issue.message[:50]}...",
                    total=None,
                )

                rule, code_context, external_docs = await self._fetch_enriched_context(issue, source_lines)

                fix_result = await self.ai_tool.fix_issue(
                    issue,
                    file_path,
                    rule=rule,
                    code_context=code_context,
                    external_docs=external_docs,
                )

                self._handle_fix_result(fix_result, issue, dry_run, summary, progress, task, rule)

                # Remove the completed task to prevent duplicates in the display
                progress.remove_task(task)

        return summary

    def _display_summary(self, summary: FixSummary, dry_run: bool) -> None:
        """Display fix summary.

        Args:
            summary: Fix summary
            dry_run: Whether in dry-run mode
        """
        display_fix_summary(summary, dry_run)
