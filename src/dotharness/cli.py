"""Console script entry points for the four tools."""

from dotharness.pr_review.cli import cli as pr_review
from dotharness.sisyphus.cli import cli as sisyphus
from dotharness.spec_prism_flow.cli import cli as spec_prism_flow
from dotharness.vibe_heal.cli import app as vibe_heal

__all__ = ["pr_review", "sisyphus", "spec_prism_flow", "vibe_heal"]
