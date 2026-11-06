"""Shared filesystem locations (usable without importing ``dotharness.pr_review``)."""

from importlib import resources
from pathlib import Path


def default_knowledge_dir() -> Path:
    """`~/.harness/knowledge` when it exists, else the knowledge shipped inside the dotharness package (D-7)."""
    user_dir = Path("~/.harness/knowledge").expanduser()
    if user_dir.is_dir():
        return user_dir
    # Plain path from importlib.resources: the wheel is installed unpacked, so the traversable is a real directory.
    return Path(str(resources.files("dotharness") / "knowledge"))
