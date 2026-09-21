# Phase 04 — `plan decompose`: recursive chunk decomposition + graph derivation

*Purpose: recursively split the requirements doc into right-sized leaf phases and derive their dependency graph.*

## Scope
- spec_prism_flow/chunk.py
- spec_prism_flow/decompose.py
- spec_prism_flow/generator.py
- spec_prism_flow/linearize.py
- spec_prism_flow/cli.py (addition: `plan decompose` subcommand)
- tests/test_chunk.py
- tests/test_decompose.py
- tests/test_generator.py
- tests/test_linearize.py

## Requirements

Excerpt, chunk A-2-3's mini-requirements doc §4 (Glossary, verbatim from requirements.md §4) and §7 (Detailed functional requirements):

### Glossary (Chunk / Leaf / Generator)
- **Chunk / seed.** A named sub-scope of `requirements.md`, carrying a rough file-scope estimate and a parent path (`A`, then `A-1`, `A-1-1`, ...); the root seed is the whole document. When passed as input to the generator, the same chunk is called a **seed**.
- **Leaf.** A chunk whose own elicitation run produces a requirements doc for an already-correctly-scoped task: its Feature/component-breakdown section comes back trivial (one feature) and it fits the sizing bands. Always a terminal tree node.
- **Generator.** The per-chunk agent call: maps a chunk to either a leaf (termination, with its mini-doc) or a set of 2–4 named child chunks (recursion continues).

### 7.1 Chunk / tree model
A `Chunk` (frozen dataclass): `path: str` (e.g. `"A"`, `"A-1"`, `"A-1-2"`), `name: str`, `file_scope_estimate: list[str]` (the chunk's in-scope file paths, set once at chunk creation/split and never revised afterward — a list, not a count, so Phase 01's `check_file_scope(paths: list[str])`, §7.5's graph-edge/disjoint-scope derivation, and Phase 05's `PhaseFile.scope` (§7.1) can all compare and consume actual paths; there is no separate, later-derived scope for a leaf, so a leaf's `file_scope_estimate` is the authoritative file list for all of those consumers), `requirements_slice: str` (the exact sub-scope text passed as the generator's "rough brief" input — never padded with sibling or parent content, favoring self-containment over inherited context), `depth: int` (root = 0). A `ChunkNode` wraps a `Chunk` with either a `leaf_doc: str` (the generator's finished mini-doc) or `children: list[ChunkNode]` — never both. The root node's chunk is the whole `requirements.md`, `path="A"`.

### 7.2 Generator invocation
One function, `run_generator(chunk: Chunk, *, forced_split: bool = False) -> GeneratorResult`, where `GeneratorResult` is a tagged union of `Leaf(doc: str)` or `Split(children: list[Chunk])` (2–4 entries, each with its own `path` extending the parent's, e.g. `A-1` → `A-1-1`..`A-1-4`). Internally this calls Phase 02's hand-off executor with a prompt file assembled from `requirements-doc-drafting-prompt.md` (verbatim, unabridged) followed by `chunk.requirements_slice` as the "rough brief"; when `forced_split=True`, an appended instruction states the chunk was already judged trivial once and a split is required regardless. The agent's output file is expected to open with an explicit `LEAF` or `SPLIT` marker line the parser keys off; any other content is a `DecomposeError` (malformed generator output), not a silent leaf/split guess.

### 7.3 Trivial-breakdown deadlock (retry-then-escalate)
After a `Leaf(doc)` result, `decompose` checks `doc` against Phase 01's `sizing.py` (`check_file_scope`, `check_word_count`) using the chunk's declared file-scope estimate and the doc's word count. If out-of-band: re-run `run_generator(chunk, forced_split=True)` exactly once. If the retry again returns `Leaf`, the chunk is flagged for human review (recorded, not silently accepted as a leaf, and not retried further). If the retry instead returns `Split`, recursion continues normally with the new children — unless `depth == cap` (§7.4), in which case the chunk is instead flagged for human review per §7.4's escalation path, and the `Split` result is discarded rather than recursed into. Every retry and every escalation is appended to a log (`decompose_log.jsonl`) with the chunk path and reason — no automatic adjustment of the generator prompt or sizing bands follows from this log.

### 7.4 Depth-first asymmetric recursion & termination safeguard
The driver recurses depth-first: for a `Split` result, each child chunk is processed to completion (its own leaf-or-split resolution, recursively) before the next sibling starts — branches terminate independently, at different depths, with no fixed whole-tree depth. A hard recursion-depth cap (default 4, configurable) is enforced per branch: at `depth == cap`, a chunk that would otherwise split is instead flagged for human review (same escalation path/log as §7.3) rather than split further.

### 7.5 DFS linearization & graph derivation
Once the tree is fully resolved (every branch terminated in a leaf or an escalation flag), a single DFS traversal over the tree (children visited left-to-right in the order the generator returned them) yields the ordered leaf list; each leaf's 1-based visit index becomes its phase number, and its `Depends on` is fixed as "Phase N−1 merged" (or "None (first phase)" for the first). The same traversal derives `graph.json` edges: for every pair of leaves, an edge is added if either leaf's mini-doc text references the other's chunk path/name, or their `file_scope_estimate` lists share a file path — this uses Phase 01's `Graph`/`validate_graph` to enforce the disjoint-scope invariant by construction, raising a `DecomposeError` (not silently dropping the pair) if two unlinked leaves' `file_scope_estimate` lists are found to share a file path.

### 7.6 Human checkpoint
Before any leaf's phase file is drafted, `decompose` serializes the full tree (not a flattened list) to a reviewable file — every internal node's path/name/children alongside every leaf's path/name/mini-doc summary — and invokes Phase 02's hand-off protocol to wait for human approval. Edits made to this file during the checkpoint are trusted as-is: after the pause, `decompose` re-reads the tree file from disk (picking up any hand-edit) rather than trusting the in-memory tree it built, and proceeds directly to graph derivation/phase-file drafting without re-running any generator call — this satisfies requirements.md §7.2's explicit "edits made here are trusted as-is" within a single `plan decompose` invocation.

**Deferred (not v1):** root `requirements.md` never mandates a standalone `--resume` CLI mode — only that checkpoint edits are honored, which the re-read-after-pause behavior above already covers. A separate crash-recovery mode (re-parsing an already-written tree file from a *fresh* process invocation, with no generator calls at all, for the case where the process was killed after the checkpoint file was written) is out of scope for this phase; on interruption, re-run `plan decompose` from scratch. Revisit if this proves painful in practice.

## Acceptance criteria
- `Chunk`/`ChunkNode` model the tree exactly as specified: a `ChunkNode` never carries both `leaf_doc` and `children`.
- `run_generator` produces a `LEAF`/`SPLIT`-marker-keyed parse of the hand-off's returned output, raising `DecomposeError` on any other marker/format.
- The trivial-breakdown deadlock path retries exactly once with `forced_split=True`, and flags (does not silently accept or infinitely retry) a chunk that comes back `Leaf` again.
- The depth-first driver processes each `Split` result's children to completion before the next sibling, and enforces the depth cap (default 4) by flagging rather than recursing further at the cap.
- Every retry and escalation is appended to a log file naming the chunk path and reason.
- DFS linearization assigns 1-based phase numbers in left-to-right visit order and produces the fixed `Depends on` chain; graph-edge derivation calls Phase 01's `validate_graph` and raises `DecomposeError` (not silent drop) on a disjoint-scope violation.
- The human-checkpoint tree file preserves full hierarchy (not a flattened list); after the checkpoint pause, `decompose` re-reads the tree file from disk (picking up any hand-edit) before deriving the graph / drafting phase files, without re-invoking any generator call.
- `uv run pytest` passes for all four test files; `ruff check`/`ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/test_chunk.py tests/test_decompose.py tests/test_generator.py tests/test_linearize.py -v` and confirm all cases above pass.
- Against a small fixture requirements slice with a stubbed generator forced to return `Split` then `Leaf` for its children, run `plan decompose` and confirm the resulting tree file shows correct hierarchy and the DFS-derived numbering/edges are correct.
- Construct a fixture where the generator returns `Leaf` twice in a row for the same chunk (simulating a trivial-breakdown deadlock) and confirm exactly one forced-split retry occurs, followed by an escalation flag, logged with the chunk's path.
- Edit the tree file by hand (e.g. reorder two children) during the checkpoint pause; confirm the edit is respected in the resulting phase corpus without any extra generator call.
- Confirm no unhandled exceptions appear in any of the above, and that a disjoint-scope violation raises a named `DecomposeError` rather than silently dropping the conflicting pair.

## Depends on
- Phase 03 merged.
