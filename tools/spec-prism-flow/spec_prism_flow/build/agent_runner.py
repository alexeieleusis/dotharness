from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from spec_prism_flow.build import git_ops
from spec_prism_flow.build.claude_backend import ClaudeBackend
from spec_prism_flow.build.opencode_backend import OpencodeBackend
from spec_prism_flow.config import AgentConfig
from spec_prism_flow.phase_file import PhaseFile


class AgentBackend(Protocol):
    def invoke(self, instructions: str, cwd: Path) -> str:
        """Run the backend against `instructions` inside `cwd` and return its
        stdout. Raises on failure -- callers do not catch."""
        ...


@dataclass(frozen=True)
class AgentRunResult:
    branch: str
    commit_sha: str | None
    empty: bool


def build_backend(cfg: AgentConfig) -> AgentBackend:
    """Selects a backend by `cfg.backend`. `cfg.backend` is already validated to
    "claude"/"opencode" at config-load time (spec_prism_flow.config.load_config), so
    this does not re-validate it -- an unrecognized value falls through to
    ClaudeBackend rather than raising."""
    if cfg.backend == "opencode":
        return OpencodeBackend()
    return ClaudeBackend()


def build_prompt(phase: PhaseFile) -> str:
    """Renders the phase file's Scope/Requirements/Acceptance criteria sections into
    the instructions text handed to a backend's invoke(). Deliberately leaves out
    Manual test checklist/Depends on -- those aren't implementation instructions."""
    scope = "\n".join(f"- {item}" for item in phase.scope)
    acceptance = "\n".join(f"- {item}" for item in phase.acceptance_criteria)
    return f"## Scope\n{scope}\n\n## Requirements\n{phase.requirements}\n\n## Acceptance criteria\n{acceptance}\n"


def branch_name(phase: PhaseFile) -> str:
    return f"phase-{phase.number:02d}-{phase.name}"


def commit_message(phase: PhaseFile) -> str:
    return f"phase {phase.number:02d}: {phase.name}"


def run_phase(phase: PhaseFile, backend: AgentBackend, clone: Path, base_branch: str) -> AgentRunResult:
    """No exception from any of the four calls below is caught here -- they all
    propagate uncaught to the caller."""
    branch = branch_name(phase)
    git_ops.checkout_fresh_branch(clone, branch, base_branch)
    backend.invoke(build_prompt(phase), clone)
    if not git_ops.commit_all(clone, commit_message(phase)):
        git_ops.delete_remote_branch_if_exists(clone, branch)
        return AgentRunResult(branch=branch, commit_sha=None, empty=True)
    git_ops.push_branch(clone, branch)
    return AgentRunResult(branch=branch, commit_sha=git_ops.head_sha(clone), empty=False)
