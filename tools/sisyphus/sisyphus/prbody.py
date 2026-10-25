"""Extractors for the pull-request description (work item #109).

Pure functions over paths and strings; no ``gh``, no harness, no spec-prism-flow imports. Git output is passed
in as text by the caller. Each extractor returns a plain string: either the section text or a one-line note
saying why it is missing (degradation). Sanitising, size limits and composition live elsewhere.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path

from sisyphus.intake import parse_resolved_decisions, split_resolved_decision
from sisyphus.plan import leaf_stems as _plan_leaf_stems
from sisyphus.plan import plan_path, read_spf_config

PLAN_NOT_MATCHED_NOTE = (
    "Note: the plan directory could not be matched to this work item; plan-derived sections omitted."
)

# ``git log`` arguments (after ``git``) producing the lines ``parse_commits`` reads.
COMMIT_LOG_FORMAT = "--format=%h%x09%s"


def commit_log_args(base: str) -> list[str]:
    """Arguments for ``git`` listing ``origin/<base>..HEAD``, oldest first, as ``sha<TAB>subject`` lines."""
    return ["log", "--reverse", COMMIT_LOG_FORMAT, f"origin/{base}..HEAD"]


# --------------------------------------------------------------- FR-3 locating


def _locate(spf: Path, repo: Path, key: str) -> Path | None:
    try:
        return plan_path(read_spf_config(spf), repo, key)
    except (OSError, ValueError):
        return None


def locate_plan_dir(spf: Path, repo: Path) -> Path | None:
    """``plan.workspace_dir`` from the spec-prism-flow TOML (relative values resolve against ``repo``), or ``None``."""
    return _locate(spf, repo, "workspace_dir")


def locate_phase_dir(spf: Path, repo: Path) -> Path | None:
    """``plan.phase_dir`` from the TOML (relative values resolve against ``repo``), or ``None``."""
    return _locate(spf, repo, "phase_dir")


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
    """The shared leaf list (``plan.leaf_stems``); raises ``ValueError`` when empty, callers turn that into a note."""
    stems = _plan_leaf_stems(phase_dir)
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


def summary_from_text(text: str) -> str:
    """Sections ``## 1.`` to ``## 4.`` of the requirements text; absent sections are skipped."""
    sections = [s for n in range(1, 5) if (s := _numbered_section(text, n))]
    if not sections:
        return "Note: Summary unavailable: no sections 1-4 found in requirements.md."
    return "\n\n".join(sections)


def extract_summary(plan_dir: Path) -> str:
    """``summary_from_text`` over ``<plan_dir>/requirements.md``; a one-line note when it cannot be read."""
    try:
        text = (plan_dir / "requirements.md").read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return f"Note: Summary unavailable: cannot read requirements.md ({type(exc).__name__})."
    return summary_from_text(text)


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


def _handoff_deviation(stem: str, path: Path) -> tuple[str | None, str | None]:
    """``(entry, note)`` for one handoff file; at most one of them is set."""
    try:
        text = _deviation_text(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, f"Note: cannot read handoff {path.name}."
    if text is None:
        return None, f"Note: no deviations bullet found in handoff {path.name}."
    if text and not _is_none(text):
        return f"- {stem}: {text}", None
    return None, None


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
        for path in files:
            entry, note = _handoff_deviation(stem, path)
            if entry:
                entries.append(entry)
            if note:
                notes.append(note)
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


# ------------------------------------------------------------------ assembly


def build_body(
    item: str,
    *,
    brief_path: Path,
    plan_dir: Path,
    phase_dir: Path,
    base_branch: str,
    workspace: Path,
    run_git: Callable[[Sequence[str]], str | None],
) -> str:
    """Locate the artifacts, check plan ownership, degrade per section and compose the body (FR-12, D-6).

    ``run_git`` runs ``git`` with the given arguments and returns stdout, or ``None`` when the command fails.
    Extractors return notes for missing input; this function adds notes only for the cases it detects itself
    (plan not matched, leaf list unavailable). The caller owns the outer guard against unexpected errors.
    """
    decisions = extract_decisions(brief_path)
    summary = phases = deviations = ""
    requirements = None
    if plan_dir_matches_brief(plan_dir, brief_path):
        requirements = read_requirements(plan_dir)
        summary = (
            summary_from_text(requirements)
            if requirements is not None
            else "Note: Summary unavailable: cannot read requirements.md."
        )
        try:
            stems = leaf_stems(phase_dir)
        except (OSError, ValueError) as exc:
            phases = deviations = f"Note: leaf list unavailable ({type(exc).__name__})."
        else:
            phases = list_phases(phase_dir, run_git(commit_log_args(base_branch)))
            deviations = extract_deviations(workspace / "handoffs", stems)
    else:
        summary = PLAN_NOT_MATCHED_NOTE
    return compose_body(
        item,
        summary=summary,
        phases=phases,
        decisions=decisions,
        deviations=deviations,
        requirements=requirements,
        workspace=workspace,
        plan_dir=plan_dir,
    )


# ---------------------------------------------------------------- pull-request title

MAX_TITLE_CHARS = 256
_TITLE_ELLIPSIS = "..."
_TITLE_LABEL = re.compile(r"(?:Requirements|Brief):\s*", re.IGNORECASE)
_FENCE = re.compile(r"^\s*(?:```|~~~)")


def fallback_title(item: str) -> str:
    """``Sisyphus: <item>``, capped like every title (FR-9, FR-11)."""
    return _cap_title(f"Sisyphus: {item}")


def _cap_title(text: str) -> str:
    if len(text) <= MAX_TITLE_CHARS:
        return text
    return text[: MAX_TITLE_CHARS - len(_TITLE_ELLIPSIS)] + _TITLE_ELLIPSIS


def _first_h1(text: str) -> str | None:
    """Text of the first ``# `` line outside fenced code blocks, or ``None`` (FR-4)."""
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and line.startswith("# "):
            return line[2:]
    return None


def _title_text(markdown: str | None) -> str | None:
    """First H1 without its ``Requirements:`` / ``Brief:`` label, or ``None`` when absent or empty (FR-5, FR-6)."""
    if markdown is None:
        return None
    heading = _first_h1(markdown)
    if heading is None:
        return None
    cleaned = _TITLE_LABEL.sub("", heading.strip(), count=1).strip()
    return cleaned or None


def clean_title(text: str, workspace: Path, plan_dir: Path | None, home: Path | None = None) -> str:
    """Redact paths, collapse whitespace to single spaces, neutralise mentions (not ``#N``), cap the length."""
    text = " ".join(redact_paths(text, workspace, plan_dir, home).split())
    return _cap_title(_MENTION.sub("@" + ZWSP, text))


def derive_title(brief_path: Path, plan_dir: Path | None, workspace: Path, home: Path | None = None) -> str | None:
    """Descriptive PR title from the requirements H1 (owned plan) else the brief H1; ``None`` when neither yields one.

    The caller uses ``fallback_title`` on ``None`` (FR-1 to FR-3).
    """
    title = None
    if plan_dir is not None and plan_dir_matches_brief(plan_dir, brief_path):
        title = _title_text(read_requirements(plan_dir))
    if title is None:
        try:
            title = _title_text(brief_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            title = None
    if title is None:
        return None
    return clean_title(title, workspace, plan_dir, home) or None
