"""Models for issue processing."""

from pydantic import BaseModel

from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue


class ProcessingResult(BaseModel):
    """Result of processing issues.

    Contains statistics about the processing and the final list of issues to fix.
    """

    total_issues: int
    fixable_issues: int
    skipped_issues: int
    issues_to_fix: list[SonarQubeIssue]

    @property
    def has_issues(self) -> bool:
        """Check if there are any issues to fix.

        Returns:
            True if there are issues to fix
        """
        return len(self.issues_to_fix) > 0
