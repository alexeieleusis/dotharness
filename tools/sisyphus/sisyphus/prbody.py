"""Extractors for the pull-request description (work item #109).

Pure functions over paths and strings; no ``gh``, no harness, no spec-prism-flow imports. Git output is passed
in as text by the caller. Each extractor returns a plain string: either the section text or a one-line note
saying why it is missing (degradation). Sanitising, size limits and composition live elsewhere.
"""

from __future__ import annotations

import contextlib
import json
import re
import tomllib
from collections.abc import Sequence
from pathlib import Path

from sisyphus.intake import parse_resolved_decisions, split_resolved_decision

LEAF_GLOB = "*-leaf.md"
PLAN_NOT_MATCHED_NOTE = (
    "Note: the plan directory could not be matched to this work item; plan-derived sections omitted."
)

# ``git log`` arguments (after ``git``) producing the lines ``parse_commits`` reads.
COMMIT_LOG_FORMAT = "--format=%h%x09%s"


def commit_log_args(base: str) -> list[str]:
    """Arguments for ``git`` listing ``origin/<base>..HEAD``, oldest first, as ``sha<TAB>subject`` lines."""
    return ["log", "--reverse", COMMIT_LOG_FORMAT, f"origin/{base}..HEAD"]


# --------------------------------------------------------------- FR-3 locating


def _plan_path(spf: Path, repo: Path, key: str) -> Path | None:
    try:
        data = tomllib.loads(spf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plan = data.get("plan")
    raw = plan.get(key) if isinstance(plan, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else repo / path


def locate_plan_dir(spf: Path, repo: Path) -> Path | None:
    """``plan.workspace_dir`` from the spec-prism-flow TOML (relative values resolve against ``repo``), or ``None``."""
    return _plan_path(spf, repo, "workspace_dir")


def locate_phase_dir(spf: Path, repo: Path) -> Path | None:
    """``plan.phase_dir`` from the TOML (relative values resolve against ``repo``), or ``None``."""
    return _plan_path(spf, repo, "phase_dir")


def plan_dir_matches_brief(plan_dir: Path, brief_path: Path) -> bool:
    """True when ``<plan_dir>/init_manifest.json`` has a ``"brief"`` resolving to ``brief_path`` (D-1)."""
    try:
        manifest = json.loads((plan_dir / "init_manifest.json").read_text(encoding="utf-8"))
        recorded = manifest.get("brief") if isinstance(manifest, dict) else None
        if not isinstance(recorded, str) or not recorded:
            return False
        return Path(recorded).expanduser().resolve() == brief_path.expanduser().resolve()
    except (OSError, ValueError):
        return False


def leaf_stems(phase_dir: Path) -> list[str]:
    """Leaf stems from ``<phase_dir>/graph.json`` ``nodes``, else the sorted ``*-leaf.md`` stems.

    Raises ``OSError``/``ValueError`` only when neither source is usable; callers turn that into a note.
    """
    graph = phase_dir / "graph.json"
    try:
        nodes = json.loads(graph.read_text(encoding="utf-8")).get("nodes")
        if isinstance(nodes, list) and nodes and all(isinstance(n, str) and n for n in nodes):
            return list(nodes)
    except (OSError, ValueError, AttributeError):
        pass
    stems = sorted(p.stem for p in phase_dir.glob(LEAF_GLOB))
    if not stems:
        raise ValueError(f"no leaves found in {phase_dir}")  # noqa: TRY003
    return stems


def _strip_number(stem: str) -> str:
    return re.sub(r"^\d+-", "", stem)


def _number(stem: str) -> str | None:
    m = re.match(r"^(\d{2})-", stem)
    return m.group(1) if m else None


def select_handoffs(handoff_dir: Path, stems: Sequence[str]) -> dict[str, list[Path]]:
    """Map each leaf stem to its handoff files: ``NN-<stem>.md`` and ``NN-<stem>-followup.md``, ``NN`` two digits.

    ``<stem>`` may be the full leaf stem or the stem without its own number prefix.
    """
    out: dict[str, list[Path]] = {s: [] for s in stems}
    names = sorted(p for p in handoff_dir.iterdir() if p.is_file())
    for stem in stems:
        variants = {stem, _strip_number(stem)}
        for p in names:
            for v in variants:
                if re.fullmatch(rf"\d{{2}}-{re.escape(v)}(?:-followup)?\.md", p.name):
                    out[stem].append(p)
                    break
    return out


# ----------------------------------------------------------------- FR-4 summary


def _numbered_section(text: str, n: int) -> str | None:
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(rf"^## {n}\.", ln)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


def extract_summary(plan_dir: Path) -> str:
    """Sections ``## 1.`` to ``## 4.`` of ``requirements.md``; absent sections are skipped."""
    try:
        text = (plan_dir / "requirements.md").read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return f"Note: Summary unavailable: cannot read requirements.md ({type(exc).__name__})."
    sections = [s for n in range(1, 5) if (s := _numbered_section(text, n))]
    if not sections:
        return "Note: Summary unavailable: no sections 1-4 found in requirements.md."
    return "\n\n".join(sections)


# ----------------------------------------------------------------- FR-5 phases


def parse_commits(log_text: str) -> list[tuple[str, str]]:
    """``(sha, subject)`` pairs from ``sha<TAB>subject`` lines, in input order."""
    commits = []
    for line in log_text.splitlines():
        sha, sep, subject = line.partition("\t")
        if sep and sha.strip():
            commits.append((sha.strip(), subject.strip()))
    return commits


def _match_leaf(subject: str, stems: Sequence[str]) -> str | None:
    """Stem match (longest first) wins over number match; the first leaf in order wins among number matches."""
    low = subject.lower()
    for stem in sorted(stems, key=lambda s: -len(_strip_number(s))):
        if _strip_number(stem).lower() in low or stem.lower() in low:
            return stem
    for stem in stems:
        num = _number(stem)
        if num and re.search(rf"(?<![\w#/]){num}(?!\w)", subject):
            return stem
    return None


def list_phases(phase_dir: Path, log_text: str | None) -> str:
    """One line per leaf with the SHA and subject of its commit(s), then unmatched commits."""
    try:
        stems = leaf_stems(phase_dir)
    except (OSError, ValueError) as exc:
        return f"Note: Phases unavailable: cannot read the leaf list ({type(exc).__name__})."
    if log_text is None:
        return "Note: Phases unavailable: git log failed."
    matched: dict[str, list[tuple[str, str]]] = {s: [] for s in stems}
    unmatched: list[tuple[str, str]] = []
    for sha, subject in parse_commits(log_text):
        leaf = _match_leaf(subject, stems)
        if leaf is None:
            unmatched.append((sha, subject))
        else:
            matched[leaf].append((sha, subject))
    lines = []
    for stem in stems:
        commits = matched[stem]
        if commits:
            lines.append(f"- {stem}: " + "; ".join(f"{sha} {subject}" for sha, subject in commits))
        else:
            lines.append(f"- {stem}")
    if unmatched:
        lines.append("")
        lines.append("Commits not matched to a phase:")
        lines.extend(f"- {sha} {subject}" for sha, subject in unmatched)
    return "\n".join(lines)


# -------------------------------------------------------------- FR-7 deviations

_DEVIATION_BULLET = re.compile(r"^- Deviations from the plan:[ \t]*(.*)$")


def _deviation_text(handoff: str) -> str | None:
    """Text after the bullet up to the next top-level ``- `` bullet; ``None`` when the bullet is absent."""
    lines = handoff.splitlines()
    for i, line in enumerate(lines):
        m = _DEVIATION_BULLET.match(line)
        if m:
            body = [m.group(1)]
            for nxt in lines[i + 1 :]:
                if nxt.startswith("- "):
                    break
                body.append(nxt)
            return "\n".join(body).strip()
    return None


def _is_none(text: str) -> bool:
    return text.strip().rstrip(".!;:,").strip().lower() == "none"


def extract_deviations(handoff_dir: Path, stems: Sequence[str]) -> str:
    """Deviations from each leaf's handoff(s); notes for missing handoffs or bullets."""
    try:
        by_stem = select_handoffs(handoff_dir, stems)
    except OSError:
        return f"Note: Deviations unavailable: handoffs directory {handoff_dir.name} is absent or unreadable."
    entries: list[str] = []
    notes: list[str] = []
    for stem in stems:
        files = by_stem[stem]
        if not files:
            notes.append(f"Note: no handoff found for {stem}.")
            continue
        for path in files:
            try:
                text = _deviation_text(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                notes.append(f"Note: cannot read handoff {path.name}.")
                continue
            if text is None:
                notes.append(f"Note: no deviations bullet found in handoff {path.name}.")
            elif text and not _is_none(text):
                entries.append(f"- {stem}: {text}")
    if not entries and not notes:
        return "None reported."
    parts = list(entries)
    parts.extend(notes)
    return "\n".join(parts)


# ---------------------------------------------------------------- FR-6 decisions


def format_decisions(brief_text: str) -> str:
    """Resolved decisions from the brief, one bullet each; ``""`` when there are none (omit the section)."""
    lines = []
    for d in parse_resolved_decisions(brief_text):
        question, answer = split_resolved_decision(d.text)
        if not question:
            lines.append(f"- {d.number}. {answer}")
        elif answer:
            lines.append(f"- {d.number}. {question} Resolution: {answer}")
        else:
            lines.append(f"- {d.number}. {question}")
    return "\n".join(lines)


def extract_decisions(brief_path: Path) -> str:
    """``format_decisions`` over the brief file; a one-line note when the brief cannot be read."""
    try:
        text = brief_path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return f"Note: Decisions unavailable: cannot read the brief ({type(exc).__name__})."
    return format_decisions(text)


# ------------------------------------------------- sanitise, redact, compose

MAX_BODY_CHARS = 60000
HEADER_NOTICE = "Opened by Sisyphus. Review required; this PR is not approved."
ZWSP = "​"
FULL_FILE_NOTE = (
    "Note: the full requirements are in `requirements.md` in the plan directory "
    "(`<plan-dir>/requirements.md`) on the machine that ran Sisyphus."
)
TRUNCATED_NOTE = (
    "Note: this description was truncated; the full requirements are in `requirements.md` in the plan "
    "directory (`<plan-dir>/requirements.md`) on the machine that ran Sisyphus."
)

_MENTION = re.compile(r"@(?=[A-Za-z0-9])")
_ISSUE_REF = re.compile(r"#(?=\d)")
_DETAILS_CLOSE = re.compile(r"</(?=details)", re.IGNORECASE)


def sanitise(text: str) -> str:
    """Neutralise mentions, ``#N`` references and ``</details>`` in embedded text (FR-10, FR-8)."""
    text = _MENTION.sub("@" + ZWSP, text)
    text = _ISSUE_REF.sub("#" + ZWSP, text)
    return _DETAILS_CLOSE.sub("&lt;/", text)


def redact_paths(text: str, workspace: Path, plan_dir: Path | None, home: Path | None = None) -> str:
    """Replace workspace, plan dir and home-directory paths with placeholders, longest first (FR-11)."""
    pairs: list[tuple[str, str]] = []

    def add(path: Path, placeholder: str) -> None:
        forms = {str(path)}
        with contextlib.suppress(OSError):
            forms.add(str(path.resolve()))
        pairs.extend((form, placeholder) for form in forms if form.rstrip("/"))

    add(workspace, "<workspace>")
    if plan_dir is not None:
        add(plan_dir, "<plan-dir>")
    add(home if home is not None else Path.home(), "~")
    for form in ("${HOME}", "$HOME"):
        pairs.append((form, "~"))
    for form, placeholder in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        text = re.sub(re.escape(form.rstrip("/")) + r"(?![\w-])", lambda _m, p=placeholder: p, text)
    return text


def embed(text: str, workspace: Path, plan_dir: Path | None, home: Path | None = None) -> str:
    """Redact paths, then sanitise: the treatment every embedded text receives."""
    return sanitise(redact_paths(text, workspace, plan_dir, home))


def requirements_block(requirements: str) -> str:
    """The ``<details>`` block holding the (already embedded) requirements text (FR-8)."""
    return f"<details><summary>Full requirements</summary>\n\n{requirements.strip()}\n\n</details>"


def read_requirements(plan_dir: Path | None) -> str | None:
    """Text of ``<plan_dir>/requirements.md`` or ``None`` when missing or unreadable."""
    if plan_dir is None:
        return None
    try:
        return (plan_dir / "requirements.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _truncate(text: str, limit: int) -> str:
    cut = text[: max(limit, 0)]
    if cut.count("```") % 2:
        cut = cut[: max(limit - 4, 0)]
        if cut.count("```") % 2:
            cut += "\n```"
    return cut


def compose_body(
    item: str,
    *,
    summary: str = "",
    phases: str = "",
    decisions: str = "",
    deviations: str = "",
    requirements: str | None = None,
    workspace: Path,
    plan_dir: Path | None = None,
    home: Path | None = None,
) -> str:
    """Assemble the pull-request body from already-extracted section strings (FR-1, G2, FR-9, FR-12).

    Never raises for missing content: empty sections are omitted, ``requirements=None`` becomes a one-line note.
    ``len(result) <= MAX_BODY_CHARS`` always holds; the header is never truncated.
    """
    header = f"Work item: {item}\n\n{HEADER_NOTICE}"

    def emb(text: str) -> str:
        return embed(text, workspace, plan_dir, home).strip()

    parts: list[str] = []
    for title, text in (("Summary", summary), ("Phases", phases), ("Decisions", decisions), ("Deviations", deviations)):
        if text.strip():
            parts.append(f"## {title}\n\n{emb(text)}")

    details: str | None = None
    if requirements is None:
        parts.append("Note: `requirements.md` is missing or unreadable; full requirements omitted.")
    elif requirements.strip():
        details = requirements_block(emb(requirements))

    def assemble(extra: list[str]) -> str:
        return "\n\n".join([header, *parts, *extra])

    body = assemble([details] if details else [])
    if len(body) <= MAX_BODY_CHARS:
        return body
    if details:
        body = assemble([FULL_FILE_NOTE])
        if len(body) <= MAX_BODY_CHARS:
            return body
    rest = "\n\n".join(parts)
    tail = "\n\n" + TRUNCATED_NOTE
    budget = MAX_BODY_CHARS - len(header) - 2 - len(tail)
    body = f"{header}\n\n{_truncate(rest, budget)}{tail}"
    return body[:MAX_BODY_CHARS]
