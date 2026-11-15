"""Prompt template rendering. The host supplies the variable values (``Discussion.prompt_variables``)."""

import re
from collections.abc import Mapping

# The valid prompt variable names. Every host supplies a value for each, templates may declare only these, and
# code that iterates or validates variable keys uses ``HOST_VARIABLES`` rather than its own list.
HOST_CLI = "host-cli"
HOST_DIRECT_REPLY_CMDS = "host-direct-reply-cmds"
HOST_ISSUE_COMMENT_CMD = "host-issue-comment-cmd"
HOST_MINIMIZE_COMMENT_CMD = "host-minimize-comment-cmd"
HOST_POST_COMMENT_CMD = "host-post-comment-cmd"
HOST_PR_COMMENT_CMD = "host-pr-comment-cmd"
HOST_POST_INLINE_COMMENT_CMD = "host-post-inline-comment-cmd"
HOST_REPLY_INLINE_CMD = "host-reply-inline-cmd"
HOST_RESOLVE_THREAD_CMD = "host-resolve-thread-cmd"
HOST_VARIABLES: frozenset[str] = frozenset({
    HOST_CLI,
    HOST_DIRECT_REPLY_CMDS,
    HOST_ISSUE_COMMENT_CMD,
    HOST_MINIMIZE_COMMENT_CMD,
    HOST_POST_COMMENT_CMD,
    HOST_PR_COMMENT_CMD,
    HOST_POST_INLINE_COMMENT_CMD,
    HOST_REPLY_INLINE_CMD,
    HOST_RESOLVE_THREAD_CMD,
})


class PromptVariableError(ValueError):
    """A prompt template and the host disagree about the variables the template uses."""

    def __init__(self, declared: set[str], used: set[str], missing: set[str]) -> None:
        super().__init__(f"declared {sorted(declared)}, used {sorted(used)}, without a host value {sorted(missing)}")


_PREAMBLE_RE = re.compile(r"\A<!-- host-variables: ([^>]*?) -->\n\n")
_VARIABLE_RE = re.compile(r"<(host-[a-z-]+)>")


def render_prompt(template: str, variables: Mapping[str, str]) -> str:
    """Return ``template`` with its preamble removed and each declared ``<variable>`` replaced by the host's value.

    A template without a preamble is returned unchanged, so rendering is idempotent. It raises ``PromptVariableError`` when
    the body uses a variable the preamble does not declare, the preamble declares one the body does not use, the
    host supplies no value for a declared one, or a declared name is not in ``HOST_VARIABLES``: an agent is never given a command the host does not provide.
    """
    match = _PREAMBLE_RE.match(template)
    if match is None:
        return template
    declared = {name.strip() for name in match.group(1).split(",")}
    body = template[match.end() :]
    used = set(_VARIABLE_RE.findall(body))
    missing = declared - variables.keys()
    if used != declared or missing or not declared <= HOST_VARIABLES:
        raise PromptVariableError(declared, used, missing)
    return _VARIABLE_RE.sub(lambda m: variables[m.group(1)], body)
