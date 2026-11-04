def test_layout_sample() -> None:
    """Same basename as its sibling under another tests/<tool>/ directory; must collect under importlib mode."""
    assert "pr_review" in __file__
