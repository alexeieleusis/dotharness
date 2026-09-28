from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from spec_prism_flow.chunk import Chunk, chunk_from_dict, chunk_to_dict
from spec_prism_flow.errors import ChunkError
from spec_prism_flow.generator import GeneratorResult, Leaf, Split

JOURNAL_FILENAME = "decompose_journal.jsonl"

_EVENT_RESULT = "result"
_EVENT_TREE_WRITTEN = "tree_written"


def _slice_digest(chunk: Chunk) -> str:
    return hashlib.sha256(chunk.requirements_slice.encode()).hexdigest()


def _result_to_dict(result: GeneratorResult) -> dict:
    if isinstance(result, Leaf):
        return {"kind": "leaf", "doc": result.doc}
    return {"kind": "split", "children": [chunk_to_dict(c) for c in result.children]}


def _result_from_dict(data: dict) -> GeneratorResult:
    if data["kind"] == "leaf":
        return Leaf(doc=data["doc"])
    return Split(children=[chunk_from_dict(c) for c in data["children"]])


class DecomposeJournal:
    """Append-only record of every generator result `plan decompose` has received.

    Each hand-off costs a human round-trip, so a run interrupted partway through the
    recursion would otherwise lose all of it. Replaying the journal lets a fresh process
    rebuild the same in-memory tree with no repeat hand-offs.

    Entries are keyed by (chunk path, forced_split) and carry a digest of the chunk's
    requirements slice, so a result recorded against a since-edited slice is never replayed.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._results: dict[tuple[str, bool], dict] = {}
        self.tree_written = False
        self._load()

    @property
    def result_count(self) -> int:
        return len(self._results)

    def _load(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # A kill mid-append leaves a torn final line; everything before it is intact.
                continue
            if entry.get("event") == _EVENT_TREE_WRITTEN:
                self.tree_written = True
            elif entry.get("event") == _EVENT_RESULT:
                self._results[(entry["chunk_path"], entry["forced_split"])] = entry

    def lookup(self, chunk: Chunk, *, forced_split: bool) -> GeneratorResult | None:
        entry = self._results.get((chunk.path, forced_split))
        if entry is None or entry["slice_sha256"] != _slice_digest(chunk):
            return None
        try:
            return _result_from_dict(entry["result"])
        except (KeyError, TypeError, ChunkError):
            return None

    def record(self, chunk: Chunk, *, forced_split: bool, result: GeneratorResult) -> None:
        entry = {
            "event": _EVENT_RESULT,
            "chunk_path": chunk.path,
            "forced_split": forced_split,
            "slice_sha256": _slice_digest(chunk),
            "result": _result_to_dict(result),
        }
        self._append(entry)
        self._results[(chunk.path, forced_split)] = entry

    def mark_tree_written(self) -> None:
        self._append({"event": _EVENT_TREE_WRITTEN})
        self.tree_written = True

    def discard(self) -> None:
        self._path.unlink(missing_ok=True)
        self._results.clear()
        self.tree_written = False

    def _append(self, entry: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
