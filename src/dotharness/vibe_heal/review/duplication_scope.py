"""Duplication-scope helpers shared by the ``review`` and ``cleanup-pr`` workflows."""

from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.deduplication.client import DuplicationClient
from dotharness.vibe_heal.deduplication.models import DuplicationBlock, DuplicationGroup, DuplicationsResponse
from dotharness.vibe_heal.review.models import DuplicationLocation, FileDiagnostics, ResolvedDuplication
from dotharness.vibe_heal.sonarqube.exceptions import ComponentNotFoundError, SonarQubeAPIError


def changed_lines_in_block(block: DuplicationBlock, changed_lines: set[int]) -> set[int]:
    """Return the changed lines that fall inside a duplication block.

    The block's ``[from_line, to_line]`` range is inclusive. This is the shared
    active-duplication intersection test: a block is active when the returned
    set is non-empty.

    Args:
        block: Duplication block whose line range is tested.
        changed_lines: Changed-line set to intersect with the block (for the
            active-duplication rule, the strict changed lines of the block's file).

    Returns:
        The block's lines that are in ``changed_lines`` (possibly empty).
    """
    block_lines = set(range(block.from_line, block.to_line + 1))
    return block_lines & changed_lines


def build_other_locations(
    group: DuplicationGroup,
    target_block: DuplicationBlock,
    response: DuplicationsResponse,
) -> list[DuplicationLocation]:
    """Build other_locations by iterating all blocks and skipping only the target block.

    Preserves same-file duplicates by skipping only the specific target block
    instance (by identity) rather than excluding all blocks with the same ref.
    """
    other_locations: list[DuplicationLocation] = []
    for block in group.blocks:
        if block is target_block:
            continue
        file_info = response.get_file_info(block.ref)
        if file_info is None:
            continue
        block_file_path = file_info.key.split(":", 1)[1] if ":" in file_info.key else file_info.key
        other_locations.append(
            DuplicationLocation(
                file_path=block_file_path,
                from_line=block.from_line,
                to_line=block.to_line,
            )
        )
    return other_locations


async def get_resolved_duplications(
    config: VibeHealConfig,
    repo_relative: str,
    changed_lines_map: dict[str, set[int]],
    old_changed_lines_map: dict[str, set[int]],
    active_dup_ranges: set[tuple[int, int]],
    original_project_key: str,
    diag: FileDiagnostics,
) -> list[ResolvedDuplication]:
    """Warn about duplication blocks from main that were modified but not active in temp.

    Queries the main project (not the temp project) for duplications on the
    old-side changed lines. If a block from main intersects those old lines
    and Feature 1 found no corresponding active duplication in the temp project,
    we warn the developer to check the other instances.

    Args:
        repo_relative: Repo-root-relative POSIX path of the file.
        changed_lines_map: New-side changed lines per file (for anchor line).
        old_changed_lines_map: Old-side changed lines per file.
        active_dup_ranges: Set of (from_line, to_line) from Feature 1 (skip if covered).
        original_project_key: The main project key (config currently points at temp).
        diag: Per-file diagnostics object to populate with API outcome.

    Returns:
        ResolvedDuplication entries for each uncovered block, if any.
    """
    old_changed_lines = old_changed_lines_map.get(repo_relative, set())
    if not old_changed_lines:
        diag.resolved_dup_api_status = "skipped_no_changed_lines"
        return []
    new_changed_lines = changed_lines_map.get(repo_relative, set())
    if not new_changed_lines:
        diag.resolved_dup_api_status = "skipped_no_changed_lines"
        return []

    try:
        original_config = config.model_copy(update={"sonarqube_project_key": original_project_key})
        async with DuplicationClient(original_config) as dup_client:
            response = await dup_client.get_duplications_for_file(repo_relative)
    except ComponentNotFoundError:
        diag.resolved_dup_api_status = "component_not_found"
        return []
    except SonarQubeAPIError as e:
        diag.resolved_dup_api_status = f"api_error:{e}"
        return []
    except Exception as e:
        diag.resolved_dup_api_status = f"error:{type(e).__name__}:{e}"
        return []

    diag.resolved_dup_api_status = "ok"
    diag.resolved_dup_groups_found = len(response.duplications)

    if not response.duplications:
        return []

    component_key = f"{original_project_key}:{repo_relative}"
    target_ref = response.get_target_file_ref(component_key)
    if target_ref is None:
        return []

    findings: list[ResolvedDuplication] = []
    for group in response.duplications:
        resolved = _resolve_group(group, target_ref, response, old_changed_lines, active_dup_ranges, new_changed_lines)
        if resolved is not None:
            findings.append(resolved)
    return findings


def _resolve_group(
    group: DuplicationGroup,
    target_ref: str,
    response: DuplicationsResponse,
    old_changed_lines: set[int],
    active_dup_ranges: set[tuple[int, int]],
    new_changed_lines: set[int],
) -> ResolvedDuplication | None:
    target_block = group.get_target_block(target_ref)
    if target_block is None:
        return None
    block_lines = set(range(target_block.from_line, target_block.to_line + 1))
    if not block_lines & old_changed_lines:
        return None
    # Suppress if any active dup range overlaps this block (line shifts mean
    # the ranges are unlikely to match exactly after edits).
    if any(a_from <= target_block.to_line and a_to >= target_block.from_line for a_from, a_to in active_dup_ranges):
        return None
    other_locations = build_other_locations(group, target_block, response)
    # Choose the new-side anchor closest to the block so the GitHub PR comment
    # is attached near the relevant change rather than an unrelated hunk.
    anchor_new_line = min(new_changed_lines, key=lambda ln: abs(ln - target_block.from_line))
    return ResolvedDuplication(
        main_from_line=target_block.from_line,
        main_to_line=target_block.to_line,
        other_locations=other_locations,
        anchor_new_line=anchor_new_line,
    )
