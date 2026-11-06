from __future__ import annotations


class ChunkError(ValueError):
    pass


class DecomposeError(Exception):
    """Shared error for the whole `plan decompose` feature.

    Lives in its own dependency-free module (rather than chunk.py, decompose.py, generator.py, or
    linearize.py) because all four of those modules need to raise or catch it, and chunk.py sits
    below the others in the import graph, so parking it there would still make it look like a
    tree-model concept rather than a feature-wide one.
    """
