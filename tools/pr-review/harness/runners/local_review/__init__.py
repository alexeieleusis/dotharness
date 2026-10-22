"""`harness run local-review`: review the commits of the current branch offline, then optionally address findings.

The implementation lives in the submodules (`types`, `git`, `prompts`, `findings`, `store`, `review`,
`address`); this package re-exports only the public names documented in `docs/modules.md`. Everything else
(constants, private helpers) is reached through its defining submodule, which is also where tests patch it.
"""

from __future__ import annotations

from harness.runners.local_review.address import (
    address_findings,
    address_usage_error,
    collect_findings,
    run_address_phase,
    select_review_dir,
)
from harness.runners.local_review.findings import (
    AddressFinding,
    FindingBlock,
    StampResult,
    finding_id,
    parse_findings,
    stamp_findings,
    stamp_findings_file,
)
from harness.runners.local_review.git import check_preconditions
from harness.runners.local_review.prompts import (
    build_address_prompt,
    build_change_context,
    build_change_description,
    build_design_prompt,
    build_file_prompt,
    build_summary_prompt,
)
from harness.runners.local_review.review import ReviewResult, run, run_local_review
from harness.runners.local_review.store import (
    read_finding_status,
    read_manifest,
    reset,
    resolve_output_root,
    review_dir_for,
    review_root_for_reset,
    write_finding_status,
    write_manifest,
)
from harness.runners.local_review.types import (
    LocalReviewError,
    LocalReviewTarget,
    NothingToReviewError,
    RepoMutatedError,
)

__all__ = [
    "AddressFinding",
    "FindingBlock",
    "LocalReviewError",
    "LocalReviewTarget",
    "NothingToReviewError",
    "RepoMutatedError",
    "ReviewResult",
    "StampResult",
    "address_findings",
    "address_usage_error",
    "build_address_prompt",
    "build_change_context",
    "build_change_description",
    "build_design_prompt",
    "build_file_prompt",
    "build_summary_prompt",
    "check_preconditions",
    "collect_findings",
    "finding_id",
    "parse_findings",
    "read_finding_status",
    "read_manifest",
    "reset",
    "resolve_output_root",
    "review_dir_for",
    "review_root_for_reset",
    "run",
    "run_address_phase",
    "run_local_review",
    "select_review_dir",
    "stamp_findings",
    "stamp_findings_file",
    "write_finding_status",
    "write_manifest",
]
