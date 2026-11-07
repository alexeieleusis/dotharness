"""Tests for AI tool models."""

from dotharness.vibe_heal.ai_tools.models import FixResult


class TestFixResult:
    """Tests for FixResult model."""

    def test_successful_fix(self) -> None:
        """Test creating a successful fix result."""
        result = FixResult(
            success=True,
            files_modified=["src/main.py"],
            ai_response="Fixed the issue",
        )

        assert result.success is True
        assert result.failed is False
        assert result.files_modified == ["src/main.py"]
        assert result.ai_response == "Fixed the issue"
        assert result.error_message is None

    def test_failed_fix(self) -> None:
        """Test creating a failed fix result."""
        result = FixResult(
            success=False,
            error_message="Tool not found",
        )

        assert result.success is False
        assert result.failed is True
        assert result.error_message == "Tool not found"
        assert result.files_modified == []
        assert result.ai_response is None

    def test_failed_property(self) -> None:
        """Test the failed property."""
        success_result = FixResult(success=True)
        failure_result = FixResult(success=False, error_message="Error")

        assert success_result.failed is False
        assert failure_result.failed is True

    def test_default_values(self) -> None:
        """Test default values for optional fields."""
        result = FixResult(success=True)

        assert result.success is True
        assert result.error_message is None
        assert result.files_modified == []
        assert result.ai_response is None

    def test_multiple_files_modified(self) -> None:
        """Test result with multiple modified files."""
        result = FixResult(
            success=True,
            files_modified=["src/main.py", "src/utils.py", "tests/test_main.py"],
        )

        assert len(result.files_modified) == 3
        assert "src/main.py" in result.files_modified
        assert "src/utils.py" in result.files_modified
        assert "tests/test_main.py" in result.files_modified
