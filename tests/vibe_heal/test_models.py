"""Tests for top-level models."""

import math

from dotharness.vibe_heal.models import FixSummary


class TestFixSummary:
    """Tests for FixSummary model."""

    def test_create_summary(self) -> None:
        """Test creating a fix summary."""
        summary = FixSummary(
            total_issues=10,
            fixed=7,
            failed=2,
            skipped=1,
            commits=["abc123", "def456"],
        )

        assert summary.total_issues == 10
        assert summary.fixed == 7
        assert summary.failed == 2
        assert summary.skipped == 1
        assert len(summary.commits) == 2

    def test_success_rate_calculation(self) -> None:
        """Test success rate calculation."""
        summary = FixSummary(
            total_issues=10,
            fixed=8,
            failed=2,
        )

        # 8 fixed out of 10 attempted (8 + 2) = 80%
        assert math.isclose(summary.success_rate, 80.0, rel_tol=1e-09, abs_tol=1e-09)

    def test_success_rate_zero_attempted(self) -> None:
        """Test success rate when no issues attempted."""
        summary = FixSummary(
            total_issues=10,
            fixed=0,
            failed=0,
        )

        assert math.isclose(summary.success_rate, 0.0, rel_tol=1e-09, abs_tol=1e-09)

    def test_success_rate_all_successful(self) -> None:
        """Test success rate when all successful."""
        summary = FixSummary(
            total_issues=5,
            fixed=5,
            failed=0,
        )

        assert math.isclose(summary.success_rate, 100.0, rel_tol=1e-09, abs_tol=1e-09)

    def test_has_failures_true(self) -> None:
        """Test has_failures when there are failures."""
        summary = FixSummary(
            total_issues=5,
            fixed=3,
            failed=2,
        )

        assert summary.has_failures is True

    def test_has_failures_false(self) -> None:
        """Test has_failures when no failures."""
        summary = FixSummary(
            total_issues=5,
            fixed=5,
            failed=0,
        )

        assert summary.has_failures is False

    def test_default_values(self) -> None:
        """Test default values for optional fields."""
        summary = FixSummary(total_issues=10)

        assert summary.fixed == 0
        assert summary.failed == 0
        assert summary.skipped == 0
        assert summary.commits == []
