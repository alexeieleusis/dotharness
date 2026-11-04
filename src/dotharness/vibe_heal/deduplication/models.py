"""Models for SonarQube duplications API responses."""

from pydantic import BaseModel, Field


class DuplicationBlock(BaseModel):
    """Represents a single block of duplicated code.

    Each block indicates where a piece of duplicated code appears,
    using a reference to a file in the files map.
    """

    model_config = {"extra": "ignore"}  # Ignore extra fields from API

    from_line: int = Field(alias="from", description="Starting line number of duplication")
    size: int = Field(description="Number of lines in the duplication")
    ref: str = Field(alias="_ref", description="Reference to file in files map")

    @property
    def to_line(self) -> int:
        """Calculate the ending line number of the duplication.

        Returns:
            Ending line number (inclusive)
        """
        return self.from_line + self.size - 1

    def get_snippet_lines(self, source_lines: list[str]) -> tuple[list[str], list[str]]:
        """Extract first 3 and last 3 lines of the duplication block.

        Args:
            source_lines: Full source code lines (0-indexed)

        Returns:
            Tuple of (first_3_lines, last_3_lines)
        """
        # Convert to 0-indexed
        start_idx = self.from_line - 1
        end_idx = self.to_line  # to_line is inclusive, so end_idx points one past

        # Extract the duplicated block
        block_lines = source_lines[start_idx:end_idx]

        if len(block_lines) <= 6:
            # If 6 or fewer lines, return first half and second half
            mid = len(block_lines) // 2
            return block_lines[:mid], block_lines[mid:]

        # Otherwise return first 3 and last 3
        return block_lines[:3], block_lines[-3:]


class DuplicationFileInfo(BaseModel):
    """Information about a file that contains duplicated code."""

    model_config = {"extra": "ignore"}

    key: str = Field(description="File component key (e.g., 'project:path/to/file.py')")
    name: str = Field(description="File or class name")
    project_name: str = Field(
        alias="projectName",
        description="Name of the project containing the file",
    )


class DuplicationGroup(BaseModel):
    """Represents a group of duplicate code blocks.

    Each group contains multiple blocks that are duplicates of each other.
    For example, if the same code appears in 3 places, there will be 3 blocks.
    """

    model_config = {"extra": "ignore"}

    blocks: list[DuplicationBlock] = Field(description="List of duplicate blocks in this group")

    def get_target_block(self, target_file_ref: str) -> DuplicationBlock | None:
        """Get the duplication block for the target file.

        Args:
            target_file_ref: Reference ID of the target file

        Returns:
            DuplicationBlock for the target file, or None if not found
        """
        for block in self.blocks:
            if block.ref == target_file_ref:
                return block
        return None

    def get_other_blocks(self, target_file_ref: str) -> list[DuplicationBlock]:
        """Get all duplication blocks except the target file's block.

        Args:
            target_file_ref: Reference ID of the target file to exclude

        Returns:
            List of blocks from other files
        """
        return [block for block in self.blocks if block.ref != target_file_ref]


class DuplicationsResponse(BaseModel):
    """Response from /api/duplications/show endpoint."""

    model_config = {"extra": "ignore"}

    duplications: list[DuplicationGroup] = Field(
        default_factory=list,
        description="List of duplication groups",
    )
    files: dict[str, DuplicationFileInfo] = Field(
        default_factory=dict,
        description="Map of file references to file information",
    )

    def get_file_info(self, ref: str) -> DuplicationFileInfo | None:
        """Get file information by reference ID.

        Args:
            ref: Reference ID (e.g., '1', '2')

        Returns:
            File information or None if not found
        """
        return self.files.get(ref)

    def get_target_file_ref(self, target_component_key: str) -> str | None:
        """Find the reference ID for a specific file component key.

        Args:
            target_component_key: Component key to search for

        Returns:
            Reference ID or None if not found
        """
        for ref, file_info in self.files.items():
            if file_info.key == target_component_key:
                return ref
        return None
