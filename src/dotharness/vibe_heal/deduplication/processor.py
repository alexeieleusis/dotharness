"""Processor for sorting and filtering code duplications."""

from dotharness.vibe_heal.deduplication.models import DuplicationGroup, DuplicationsResponse


class DuplicationProcessingResult:
    """Result of processing duplications."""

    def __init__(
        self,
        total_groups: int,
        processable_groups: int,
        skipped_groups: int,
        groups_to_fix: list[DuplicationGroup],
    ) -> None:
        """Initialize processing result.

        Args:
            total_groups: Total number of duplication groups found
            processable_groups: Number of groups that can be processed
            skipped_groups: Number of groups skipped
            groups_to_fix: List of duplication groups to fix (sorted)
        """
        self.total_groups = total_groups
        self.processable_groups = processable_groups
        self.skipped_groups = skipped_groups
        self.groups_to_fix = groups_to_fix


class DuplicationProcessor:
    """Processes code duplications to determine fix order.

    Sorts duplications in reverse line order (highest line number first)
    to avoid line number shifts during refactoring. Optionally limits
    the number of duplications to process.
    """

    def __init__(
        self,
        max_duplications: int | None = None,
    ) -> None:
        """Initialize the duplication processor.

        Args:
            max_duplications: Maximum number of duplication groups to process
        """
        self.max_duplications = max_duplications

    def process(
        self,
        response: DuplicationsResponse,
        target_file_component_key: str,
    ) -> DuplicationProcessingResult:
        """Process duplications: filter and sort.

        Processing steps:
        1. Filter groups that include the target file
        2. Sort groups by starting line number descending (high to low)
        3. Limit number of groups if max_duplications is specified

        Args:
            response: Duplications API response
            target_file_component_key: Component key of the file being processed

        Returns:
            DuplicationProcessingResult with processed groups
        """
        total_groups = len(response.duplications)

        # Find the reference ID for the target file
        target_ref = response.get_target_file_ref(target_file_component_key)

        # If target file not found in duplications, return empty result
        if not target_ref:
            return DuplicationProcessingResult(
                total_groups=total_groups,
                processable_groups=0,
                skipped_groups=total_groups,
                groups_to_fix=[],
            )

        # Step 1: Filter groups that contain the target file
        target_groups = [group for group in response.duplications if group.get_target_block(target_ref) is not None]

        # Step 2: Sort groups by starting line number descending
        sorted_groups = self._sort_by_line_descending(target_groups, target_ref)

        # Step 3: Limit number of groups if specified
        if self.max_duplications and self.max_duplications > 0:
            sorted_groups = sorted_groups[: self.max_duplications]

        return DuplicationProcessingResult(
            total_groups=total_groups,
            processable_groups=len(target_groups),
            skipped_groups=total_groups - len(sorted_groups),
            groups_to_fix=sorted_groups,
        )

    def _sort_by_line_descending(
        self,
        groups: list[DuplicationGroup],
        target_ref: str,
    ) -> list[DuplicationGroup]:
        """Sort duplication groups by starting line number in descending order.

        Args:
            groups: List of duplication groups to sort
            target_ref: Reference ID of the target file

        Returns:
            Sorted list of duplication groups
        """

        # Sort by the starting line of the target file's block
        # Higher line numbers first to minimize line shifts
        def get_start_line(group: DuplicationGroup) -> int:
            block = group.get_target_block(target_ref)
            return block.from_line if block else 0

        sorted_groups = sorted(groups, key=get_start_line, reverse=True)
        return sorted_groups
