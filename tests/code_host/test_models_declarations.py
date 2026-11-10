"""Tests for code-host models, declarations and interfaces."""

import dataclasses
import inspect
import subprocess
import sys
from datetime import UTC, datetime

import pytest

import dotharness.code_host as ch
from dotharness.code_host import declarations, models
from dotharness.code_host.declarations import (
    Anchor,
    AnchorRules,
    Capability,
    HostDeclaration,
    HostLimits,
    ListBounds,
    OperationTimeouts,
)
from dotharness.code_host.models import (
    ChangeRequest,
    ChangeRequestState,
    Credential,
    InlineComment,
    RepositoryRef,
    ReviewPostResult,
)


def _cr(**kw: object) -> ChangeRequest:
    base: dict[str, object] = {
        "repository": RepositoryRef("r"),
        "number": 1,
        "title": "t",
        "body": "b",
        "url": "u",
        "state": ChangeRequestState.OPEN,
        "is_draft": False,
        "author": None,
        "head_reference": "h",
        "head_revision": "abc",
        "base_reference": "main",
        "is_cross_repository": False,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(kw)
    return ChangeRequest(**base)  # ty: ignore


def test_exports() -> None:
    assert ch.CODE_HOST_INTERFACE_VERSION == 1
    for name in (
        "CodeHost",
        "HostDeclaration",
        "Session",
        "Repository",
        "ChangeRequests",
        "Discussion",
        "Checks",
        "WorkItems",
    ):
        assert hasattr(ch, name)
    for name in ch.__all__:
        assert hasattr(ch, name)


def test_change_request_defaults_and_frozen() -> None:
    cr = _cr()
    assert cr.base_reference == "main"
    assert cr.mergeable is None
    assert cr.auto_merge_enabled is False
    assert cr.requested_reviewers == ()
    assert cr.additions is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        cr.number = 2  # ty: ignore[invalid-assignment]


def test_review_post_result_reason_default() -> None:
    r = ReviewPostResult(fell_back=False, review=None, comment=None)
    assert r.reason is None
    assert ReviewPostResult(True, None, None, reason="bad anchor").reason == "bad anchor"


def test_inline_comment_frozen() -> None:
    ic = InlineComment("b", Anchor("a.py", 3, "RIGHT"))
    assert ic.anchor.line == 3
    with pytest.raises(dataclasses.FrozenInstanceError):
        ic.body = "x"  # ty: ignore[invalid-assignment]


def test_credential_repr_hides_token() -> None:
    assert "<token>" not in repr(Credential("<token>"))


def test_repository_ref_hashable() -> None:
    assert {RepositoryRef("a"): 1}[RepositoryRef("a")] == 1


def test_anchor_single_definition() -> None:
    assert models.Anchor is declarations.Anchor


def test_declaration_reference_values() -> None:
    d = HostDeclaration(
        capabilities=frozenset(Capability),
        limits=HostLimits(
            max_inline_comments_per_review=50,
            max_review_body_chars=60000,
            max_change_request_body_chars=60000,
            timeouts=OperationTimeouts(
                30, {"Session.available": 10, "ChangeRequests.find_by_branch": 30, "Discussion.post_review": 60}
            ),
            list_bounds=ListBounds(100, {"Discussion.comments": None}),
        ),
        anchors=AnchorRules(True, frozenset({"RIGHT"}), True, False),
    )
    assert len(d.capabilities) == 6
    assert d.anchors.fallback == "top_level_comment"
    assert d.limits.list_bounds.per_operation["Discussion.comments"] is None
    assert Capability.CHECKS == "Checks"
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.anchors = d.anchors  # ty: ignore[invalid-assignment]


def test_protocols_are_async() -> None:
    for proto in (ch.Session, ch.Repository, ch.ChangeRequests, ch.Discussion, ch.Checks, ch.WorkItems):
        methods = [m for n, m in vars(proto).items() if inspect.iscoroutinefunction(m)]
        assert methods
        for n, m in vars(proto).items():
            if callable(m) and not n.startswith("_"):
                assert inspect.iscoroutinefunction(m), f"{proto.__name__}.{n}"


def test_post_review_signature() -> None:
    sig = inspect.signature(ch.Discussion.post_review)
    params = sig.parameters
    for name in ("body", "inline_comments", "head_revision", "fallback_body"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["head_revision"].default is None
    assert params["fallback_body"].default is None
    assert list(params)[:3] == ["self", "repo", "number"]


def test_stub_discussion_conforms() -> None:
    class StubDiscussion:
        async def post_review(
            self, repo, number, *, body, inline_comments, head_revision=None, fallback_body=None
        ) -> ReviewPostResult:
            return ReviewPostResult(False, None, None)

    import asyncio

    stub = StubDiscussion()
    res = asyncio.run(stub.post_review(RepositoryRef("r"), 1, body="b", inline_comments=[]))
    assert res.fell_back is False


def test_change_requests_has_for_current_branch() -> None:
    assert inspect.iscoroutinefunction(ch.ChangeRequests.for_current_branch)


def test_import_loads_no_vibe_heal() -> None:
    code = "import sys, dotharness.code_host; bad=[m for m in sys.modules if m.startswith('dotharness.vibe_heal')]; print(bad); sys.exit(1 if bad else 0)"
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
