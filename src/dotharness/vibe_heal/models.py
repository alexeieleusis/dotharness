"""Top-level models for vibe-heal."""

from pydantic import BaseModel, Field


class FixSummary(BaseModel):
    """Summary of fix operation."""

    total_issues: int = Field(description="Total issues found")
    fixed: int = Field(default=0, description="Number of issues fixed")
    failed: int = Field(default=0, description="Number of fixes that failed")
    skipped: int = Field(default=0, description="Number of issues skipped")
    commits: list[str] = Field(default_factory=list, description="List of commit SHAs")

    @property
    def success_rate(self) -> float:
        """Calculate success rate (fixed / attempted).

        Returns:
            Success rate as percentage (0-100)
        """
        attempted = self.fixed + self.failed
        if attempted == 0:
            return 0.0
        return (self.fixed / attempted) * 100

    @property
    def has_failures(self) -> bool:
        """Check if any fixes failed.

        Returns:
            True if any fixes failed
        """
        return self.failed > 0
