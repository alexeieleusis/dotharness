"""Stage names and run-record retry-counter keys, defined once. Standard library only (a leaf module)."""

from __future__ import annotations

INTAKE_STAGE = "intake"
PLAN_STAGE = "plan"
BUILD_STAGE = "build"
PUSH_STAGE = "push"
OPEN_PR_STAGE = "open-pr"
STAGE_ORDER = (INTAKE_STAGE, PLAN_STAGE, BUILD_STAGE, PUSH_STAGE, OPEN_PR_STAGE)

# Run-record `retries` keys: `<stage>:<counter>` for a per-stage counter, a bare name for a run-wide one.
AUTO_RETRY_KEY = "auto-retry"
BUILD_RETRY_KEY = f"{BUILD_STAGE}:retry-cycles"
LEAF_LIMIT_KEY = f"{PLAN_STAGE}:leaf-limit"
