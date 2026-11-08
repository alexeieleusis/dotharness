"""Models for AI tool operations."""

from pydantic import BaseModel, Field


class FixResult(BaseModel):
    """Result of attempting to fix an issue.

    This is a placeholder for Phase 4.
    """

    success: bool = Field(description="Whether the fix succeeded")
    error_message: str | None = Field(
        default=None,
        description="Error message if fix failed",
    )
    files_modified: list[str] = Field(
        default_factory=list,
        description="List of files modified by the fix",
    )
    ai_response: str | None = Field(
        default=None,
        description="Raw response from AI tool (for debugging)",
    )

    @property
    def failed(self) -> bool:
        """Check if the fix failed.

        Returns:
            True if the fix failed
        """
        return not self.success
