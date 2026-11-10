# ruff: noqa: S101, TRY003  A shipped test suite: plain asserts and AssertionError messages are the point.
"""In-memory code host and conformance suite for the review flow.

``InMemoryCodeHost`` serves a consumer test without a real host. ``CodeHostConformance`` is a base class of
behavior tests that every host must pass. A host author subclasses it and overrides ``make_target``.

This module imports no ``pytest``: the suite uses plain ``assert`` and the base class holds ``async def test_...``
coroutines, so whatever runner collects the subclass runs them. It imports nothing from ``vibe_heal``.
Examples use the placeholder token ``<token>``.
"""

import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, cast

from dotharness.code_host import interfaces
from dotharness.code_host.declarations import Anchor, Capability, HostDeclaration
from dotharness.code_host.errors import (
    AuthenticationError,
    CodeHostError,
    CodeHostTimeoutError,
    ConfigurationError,
    NoChangeRequestError,
    UnsupportedCapabilityError,
)
from dotharness.code_host.interfaces import CodeHost
from dotharness.code_host.models import (
    ChangeRequest,
    ChangeRequestState,
    InlineComment,
    RepositoryRef,
    ReviewPostResult,
    ReviewPreview,
)
from dotharness.code_host.reference import DECLARATION

SECRET = "<token>"  # noqa: S105  Sentinel: it must never appear in an error, log or repr.
NO_CHANGE_REQUEST_TEXT = "No open pull request found for the current branch"
_MAX_REVIEW_BODY_CHARS = DECLARATION.limits.max_review_body_chars
_TRUNCATION_NOTICE = "\n\n_...review body truncated to fit the host size limit._"
_BASE_DATE = "2026-01-01T00:00:00Z"

# --------------------------------------------------------------------------------------------------------------------
# Host-neutral report (duck-typed by hosts)
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportIssue:
    rule: str
    message: str
    line: int
    on_changed_line: bool = True
    doc_url: str | None = None
    root_cause: str | None = None


@dataclass(frozen=True)
class ReportFile:
    file_path: str
    issues: Sequence[ReportIssue] = ()
    duplications: Sequence[Any] = ()
    resolved_duplications: Sequence[Any] = ()
    coverage_pct: float | None = None
    covered_lines: int = 0
    instrumented_changed_lines: int = 0


@dataclass(frozen=True)
class ReportResult:
    """A review result in the shape every host reads by duck typing."""

    files: Sequence[ReportFile]
    base_branch: str = "main"
    project_label: str | None = None
    head_sha: str | None = None


# --------------------------------------------------------------------------------------------------------------------
# Seeder protocol and target
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PostedItem:
    """One successful post: a ``review`` (with inline comments) or a top-level ``comment``."""

    kind: str
    body: str
    inline_count: int = 0


class HostSeeder(Protocol):
    """Test-side handle that sets the state of one host and reads back what it received.

    ``InMemoryCodeHost`` is its own seeder. A real host supplies a seeder that stubs its transport.
    """

    def set_available(self, available: bool) -> None: ...

    def set_current_change_request(self, number: int | None, base: str = "main") -> None:
        """Set the change request of the checked-out branch, or None for no change request."""
        ...

    def reject_inline_reviews(self, reason: str) -> None:
        """The host rejects reviews that carry inline comments. A top-level comment is accepted."""
        ...

    def reject_all_posts(self, reason: str) -> None:
        """The host rejects every post."""
        ...

    def fail_posts_with(self, error: Exception) -> None:
        """Every post attempt raises ``error`` (a timeout, an authentication or an unsupported-capability error)."""
        ...

    def posts(self) -> list[PostedItem]:
        """Successful posts, in order."""
        ...

    def post_attempts(self) -> int:
        """All post attempts, successful or not."""
        ...


@dataclass(frozen=True)
class ConformanceTarget:
    host: CodeHost
    seeder: HostSeeder
    repository: RepositoryRef = field(default_factory=lambda: RepositoryRef("example/repo"))


# --------------------------------------------------------------------------------------------------------------------
# In-memory fake
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class _Failure:
    error: Exception | None = None
    reject_inline: str | None = None
    reject_all: str | None = None


class _AbsentCapability:
    """Stand-in for an undeclared capability. Every method raises UnsupportedCapabilityError."""

    def __init__(self, capability: Capability, methods: Sequence[str]) -> None:
        self._capability = capability
        self._methods = frozenset(methods)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_") or name not in self._methods:
            raise AttributeError(name)
        capability = self._capability

        async def _unsupported(*_args: object, **_kwargs: object) -> Any:
            msg = f"{capability.value}.{name} is not supported by the in-memory code host"
            raise UnsupportedCapabilityError(msg, capability=capability.value)

        return _unsupported


def _public_methods(protocol: type) -> list[str]:
    return [n for n, _ in inspect.getmembers(protocol, inspect.isfunction) if not n.startswith("_")]


def _unsupported(capability: Capability, operation: str) -> UnsupportedCapabilityError:
    msg = f"{capability.value}.{operation} is not supported by the in-memory code host"
    return UnsupportedCapabilityError(msg, capability=capability.value)


class InMemoryCodeHost:
    """A host that keeps everything in memory. It serves the review flow and is its own seeder.

    Declared capabilities: Session, Repository, ChangeRequests, Discussion. Checks and WorkItems raise
    ``UnsupportedCapabilityError``. Limits and anchors are the reference host values. No network, no subprocess,
    no clock.
    """

    def __init__(self, *, repository: str = "example/repo", user: str = "fake-user", token: str = SECRET) -> None:
        self._repository = RepositoryRef(repository)
        self._user = user
        self._token = token
        self._available = True
        self._change_request: tuple[int, str] | None = None
        self._failure = _Failure()
        self._posts: list[PostedItem] = []
        self._attempts = 0
        self.declaration: HostDeclaration = DECLARATION

    # -- seeder --------------------------------------------------------------------------------------------------

    def set_repository(self, identity: str) -> None:
        self._repository = RepositoryRef(identity)

    def set_available(self, available: bool) -> None:
        self._available = available

    def set_current_change_request(self, number: int | None, base: str = "main") -> None:
        self._change_request = None if number is None else (number, base)

    def reject_inline_reviews(self, reason: str) -> None:
        self._failure = _Failure(reject_inline=reason)

    def reject_all_posts(self, reason: str) -> None:
        self._failure = _Failure(reject_all=reason)

    def fail_posts_with(self, error: Exception) -> None:
        self._failure = _Failure(error=error)

    def require_client(self) -> None:
        """Raise OSError when the host client is unavailable, as the reference host does."""
        if not self._available:
            msg = "The in-memory code host client is not available"
            raise OSError(msg)

    def clear_failures(self) -> None:
        self._failure = _Failure()

    def posts(self) -> list[PostedItem]:
        return list(self._posts)

    def post_attempts(self) -> int:
        return self._attempts

    # -- bundle --------------------------------------------------------------------------------------------------

    def code_host(self) -> CodeHost:
        return CodeHost(
            session=_FakeSession(self),
            repository=_FakeRepository(self),
            change_requests=cast("interfaces.ChangeRequests", _FakeChangeRequests(self)),
            discussion=cast("interfaces.Discussion", _FakeDiscussion(self)),
            checks=cast("interfaces.Checks", _AbsentCapability(Capability.CHECKS, _public_methods(interfaces.Checks))),
            work_items=cast(
                "interfaces.WorkItems", _AbsentCapability(Capability.WORK_ITEMS, _public_methods(interfaces.WorkItems))
            ),
            declaration=self.declaration,
        )

    def target(self) -> ConformanceTarget:
        return ConformanceTarget(host=self.code_host(), seeder=self, repository=self._repository)


class _FakeSession:
    def __init__(self, host: InMemoryCodeHost) -> None:
        self._host = host

    async def available(self) -> bool:
        return self._host._available

    async def credential(self) -> Any:
        raise _unsupported(Capability.SESSION, "credential")

    async def current_user(self) -> str:
        return self._host._user

    async def agent_environment_names(self) -> Sequence[str]:
        raise _unsupported(Capability.SESSION, "agent_environment_names")

    async def agent_environment(self, base: Mapping[str, str] | None = None) -> Mapping[str, str]:
        raise _unsupported(Capability.SESSION, "agent_environment")


class _FakeRepository:
    def __init__(self, host: InMemoryCodeHost) -> None:
        self._host = host

    async def current(self) -> RepositoryRef:
        return self._host._repository

    async def resolve(self, identity: str) -> RepositoryRef:
        if not identity:
            msg = "Repository identity is empty"
            raise ConfigurationError(msg)
        return RepositoryRef(identity)

    async def default_branch(self, repo: RepositoryRef) -> str:
        return "main"

    async def display_name(self, repo: RepositoryRef) -> str:
        return repo.value


class _FakeChangeRequests:
    def __init__(self, host: InMemoryCodeHost) -> None:
        self._host = host

    async def for_current_branch(self, repo: RepositoryRef) -> ChangeRequest:
        self._host.require_client()
        current = self._host._change_request
        if current is None:
            raise NoChangeRequestError(NO_CHANGE_REQUEST_TEXT)
        number, base = current
        return ChangeRequest(
            repository=repo,
            number=number,
            title="",
            body="",
            url="",
            state=ChangeRequestState.OPEN,
            is_draft=False,
            author=None,
            head_reference="",
            head_revision="",
            base_reference=base,
            is_cross_repository=False,
            created_at=datetime.fromisoformat(_BASE_DATE),
        )

    async def find_by_branch(
        self, repo: RepositoryRef, branch: str, *, state: ChangeRequestState = ChangeRequestState.OPEN
    ) -> Sequence[ChangeRequest]:
        return []

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)

        async def _unsupported_op(*_args: object, **_kwargs: object) -> Any:
            raise _unsupported(Capability.CHANGE_REQUESTS, name)

        return _unsupported_op


def _build_preview(report: Any, max_inline: int) -> ReviewPreview:
    comments: list[InlineComment] = []
    nearby: list[str] = []
    for file_review in report.files:
        for issue in file_review.issues:
            if issue.on_changed_line:
                comments.append(
                    InlineComment(
                        f"**{issue.rule}** {issue.message}", Anchor(file_review.file_path, issue.line, "RIGHT")
                    )
                )
            else:
                nearby.append(f"- **{issue.rule}** ({file_review.file_path}:{issue.line}) {issue.message}")
    shown, extra = comments[:max_inline], len(comments) - max_inline
    parts = [f"Review: {len(shown)} inline, {max(extra, 0)} capped, {len(nearby)} near changed lines."]
    if nearby:
        parts.append("**Issues near changed lines:**\n" + "\n".join(nearby))
    body = "\n\n".join(parts)
    if len(body) > _MAX_REVIEW_BODY_CHARS:
        body = body[: _MAX_REVIEW_BODY_CHARS - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE
    return ReviewPreview(body=body, inline_comments=shown)


def _fallback_text(report: Any) -> str:
    lines = [f"- **{i.rule}** ({f.file_path}:{i.line}) {i.message}" for f in report.files for i in f.issues]
    text = "Fallback review\n" + "\n".join(lines)
    if len(text) > _MAX_REVIEW_BODY_CHARS:
        text = text[: _MAX_REVIEW_BODY_CHARS - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE
    return text


class _FakeDiscussion:
    def __init__(self, host: InMemoryCodeHost) -> None:
        self._host = host

    async def preview_review(self, report: object) -> ReviewPreview:
        return _build_preview(report, self._host.declaration.limits.max_inline_comments_per_review)

    def _attempt(self, *, inline: bool) -> str | None:
        """Count one post attempt. Raise an injected error. Return a rejection reason, or None to accept."""
        host = self._host
        host._attempts += 1
        failure = host._failure
        if failure.error is not None:
            raise failure.error
        if failure.reject_all is not None:
            return failure.reject_all
        if inline and failure.reject_inline is not None:
            return failure.reject_inline
        return None

    async def post_review(
        self,
        repo: RepositoryRef,
        number: int,
        *,
        body: str,
        inline_comments: Sequence[InlineComment],
        head_revision: str | None = None,
        report: object | None = None,
    ) -> ReviewPostResult:
        self._host.require_client()
        reason = self._attempt(inline=bool(inline_comments))
        if reason is None:
            self._host._posts.append(PostedItem("review", body, len(inline_comments)))
            return ReviewPostResult(fell_back=False, review=None, comment=None)
        fallback = body if report is None else _fallback_text(report)
        fallback_reason = self._attempt(inline=False)
        if fallback_reason is not None:
            msg = f"Review posting failed: {fallback_reason}"
            raise CodeHostError(msg)
        self._host._posts.append(PostedItem("comment", fallback))
        return ReviewPostResult(fell_back=True, review=None, comment=None, reason=reason)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)

        async def _unsupported_op(*_args: object, **_kwargs: object) -> Any:
            raise _unsupported(Capability.DISCUSSION, name)

        return _unsupported_op


# --------------------------------------------------------------------------------------------------------------------
# Conformance suite
# --------------------------------------------------------------------------------------------------------------------


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(record.getMessage())
        if record.exc_text:
            self.text.append(record.exc_text)


def _chain(exc: BaseException | None) -> list[BaseException]:
    seen: list[BaseException] = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def _make_report(*, on_changed: int = 0, nearby_chars: int = 0) -> ReportResult:
    issues = [ReportIssue(rule=f"rule:{i}", message=f"message {i}", line=i + 1) for i in range(on_changed)]
    if nearby_chars:
        issues.append(ReportIssue(rule="rule:big", message="x" * nearby_chars, line=1, on_changed_line=False))
    return ReportResult(files=[ReportFile("src/a.py", issues)], project_label="proj", head_sha="abcdef123456")


def _dummy_args(method: Callable[..., Any]) -> tuple[list[Any], dict[str, Any]]:
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for p in inspect.signature(method).parameters.values():
        if p.name == "self" or p.default is not inspect.Parameter.empty or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        value: Any = RepositoryRef("example/repo") if p.name == "repo" else 1 if p.name == "number" else "x"
        if p.kind is p.KEYWORD_ONLY:
            kwargs[p.name] = value
        else:
            args.append(value)
    return args, kwargs


class CodeHostConformance:
    """Behavior tests every host must pass for the review flow.

    Subclass it and override ``make_target``. Behavior ids B-NCR, B-REV, B-PRV, B-LIM, B-ERR and B-SEC are listed in
    docs/design/code-host.md, "Behaviors asserted".
    """

    async def make_target(self) -> ConformanceTarget:
        """Return a fresh host and its seeder for one test."""
        raise NotImplementedError

    async def _post(
        self, target: ConformanceTarget, *, report: object | None = None, inline: int = 1, body: str = "given body"
    ) -> ReviewPostResult:
        comments = [InlineComment(f"c{i}", Anchor("src/a.py", i + 1, "RIGHT")) for i in range(inline)]
        return await target.host.discussion.post_review(
            target.repository, 7, body=body, inline_comments=comments, report=report
        )

    # -- session and change request ------------------------------------------------------------------------------

    async def test_available_returns_bool(self) -> None:
        """B-NCR-1: available() returns a bool and does not raise, available or not."""
        target = await self.make_target()
        assert await target.host.session.available() is True
        target.seeder.set_available(False)
        assert await target.host.session.available() is False

    async def test_for_current_branch_returns_number_and_base(self) -> None:
        """B-NCR-2: for_current_branch returns the change request number and base."""
        target = await self.make_target()
        target.seeder.set_current_change_request(73, "release")
        cr = await target.host.change_requests.for_current_branch(target.repository)
        assert cr.number == 73
        assert cr.base_reference == "release"

    async def test_no_change_request_error_text(self) -> None:
        """B-NCR-3: no change request raises NoChangeRequestError (a CodeHostError) with the standard text."""
        target = await self.make_target()
        target.seeder.set_current_change_request(None)
        try:
            await target.host.change_requests.for_current_branch(target.repository)
        except NoChangeRequestError as exc:
            assert isinstance(exc, CodeHostError)
            assert NO_CHANGE_REQUEST_TEXT in str(exc)
        else:
            raise AssertionError("NoChangeRequestError not raised")

    async def test_unavailable_client_raises_oserror(self) -> None:
        """B-NCR-4: an unavailable host client raises OSError from for_current_branch and post_review."""
        target = await self.make_target()
        target.seeder.set_available(False)
        for call in (
            target.host.change_requests.for_current_branch(target.repository),
            self._post(target),
        ):
            try:
                await call
            except OSError:
                pass
            else:
                raise AssertionError("OSError not raised")
        assert target.seeder.posts() == []

    # -- post_review ---------------------------------------------------------------------------------------------

    async def test_post_review_without_fallback(self) -> None:
        """B-REV-1: an accepted review posts body and inline comments as given; fell_back false; reason None."""
        target = await self.make_target()
        result = await self._post(target, inline=2)
        assert result.fell_back is False
        assert result.reason is None
        assert target.seeder.posts() == [PostedItem("review", "given body", 2)]

    async def test_post_review_with_fallback(self) -> None:
        """B-REV-2: a rejected review falls back to one top-level comment; fell_back true; reason has the cause."""
        target = await self.make_target()
        target.seeder.reject_inline_reviews("line could not be resolved")
        result = await self._post(target)
        assert result.fell_back is True
        assert result.reason is not None
        assert "line could not be resolved" in result.reason
        posts = target.seeder.posts()
        assert [p.kind for p in posts] == ["comment"]
        assert posts[0].inline_count == 0
        assert posts[0].body == "given body"  # report is None: the given body is the comment

    async def test_fallback_text_built_from_report(self) -> None:
        """B-REV-3: with a report the fallback comment is built from the report and lists its findings."""
        target = await self.make_target()
        target.seeder.reject_inline_reviews("rejected")
        result = await self._post(target, report=_make_report(on_changed=2))
        assert result.fell_back is True
        (post,) = target.seeder.posts()
        assert post.kind == "comment"
        assert "rule:0" in post.body
        assert "rule:1" in post.body

    async def test_reason_only_when_fell_back(self) -> None:
        """B-REV-4: reason is set if and only if fell_back is true."""
        target = await self.make_target()
        ok = await self._post(target)
        assert ok.fell_back is False
        assert ok.reason is None
        target.seeder.reject_inline_reviews("nope")
        fell = await self._post(target)
        assert fell.fell_back is True
        assert fell.reason

    async def test_no_fallback_after_timeout_auth_or_unsupported(self) -> None:
        """B-REV-5: CodeHostTimeoutError, AuthenticationError and UnsupportedCapabilityError propagate without fallback."""
        errors: list[Callable[[], Exception]] = [
            lambda: CodeHostTimeoutError("post timed out", write=True),
            lambda: AuthenticationError("not authenticated"),
            lambda: UnsupportedCapabilityError("not supported", capability="Discussion"),
        ]
        for make_error in errors:
            target = await self.make_target()
            expected = make_error()
            target.seeder.fail_posts_with(expected)
            try:
                await self._post(target)
            except CodeHostError as exc:
                assert type(exc) is type(expected)
            else:
                raise AssertionError(f"{type(expected).__name__} not raised")
            assert target.seeder.post_attempts() == 1, type(expected).__name__
            assert target.seeder.posts() == []

    async def test_single_fallback_only(self) -> None:
        """B-REV-6: when the fallback also fails there is no third attempt; a CodeHostError propagates."""
        target = await self.make_target()
        target.seeder.reject_all_posts("everything rejected")
        try:
            await self._post(target)
        except CodeHostError as exc:
            assert not isinstance(exc, (CodeHostTimeoutError, AuthenticationError, UnsupportedCapabilityError))
        else:
            raise AssertionError("CodeHostError not raised")
        assert target.seeder.post_attempts() == 2
        assert target.seeder.posts() == []

    # -- preview and limits --------------------------------------------------------------------------------------

    async def test_preview_matches_post(self) -> None:
        """B-PRV-1: the body in ReviewPreview is what post_review posts when it does not fall back."""
        target = await self.make_target()
        report = _make_report(on_changed=3)
        preview = await target.host.discussion.preview_review(report)
        assert target.seeder.post_attempts() == 0  # a preview makes no host call
        result = await target.host.discussion.post_review(
            target.repository, 7, body=preview.body, inline_comments=preview.inline_comments, report=report
        )
        assert result.fell_back is False
        (post,) = target.seeder.posts()
        assert post == PostedItem("review", preview.body, len(preview.inline_comments))
        assert len(preview.inline_comments) == 3

    async def test_declared_limits(self) -> None:
        """B-LIM-1: the host declares 50 inline comments and 60000 body characters."""
        target = await self.make_target()
        limits = target.host.declaration.limits
        assert limits.max_inline_comments_per_review == 50
        assert limits.max_review_body_chars == 60_000

    async def test_inline_comments_capped(self) -> None:
        """B-LIM-2: the preview carries at most the declared number of inline comments."""
        target = await self.make_target()
        cap = target.host.declaration.limits.max_inline_comments_per_review
        preview = await target.host.discussion.preview_review(_make_report(on_changed=cap + 10))
        assert len(preview.inline_comments) == cap

    async def test_body_truncated_with_notice(self) -> None:
        """B-LIM-3: a body above the declared limit is cut to the limit and ends with a notice."""
        target = await self.make_target()
        limit = target.host.declaration.limits.max_review_body_chars
        preview = await target.host.discussion.preview_review(_make_report(nearby_chars=limit + 5_000))
        assert len(preview.body) <= limit
        assert "truncated" in preview.body[-200:].lower()

    # -- errors --------------------------------------------------------------------------------------------------

    async def test_undeclared_capability_substitution(self) -> None:
        """B-ERR-4: every method of an undeclared capability raises UnsupportedCapabilityError(capability=name)."""
        target = await self.make_target()
        host = target.host
        objects = {
            Capability.SESSION: host.session,
            Capability.REPOSITORY: host.repository,
            Capability.CHANGE_REQUESTS: host.change_requests,
            Capability.DISCUSSION: host.discussion,
            Capability.CHECKS: host.checks,
            Capability.WORK_ITEMS: host.work_items,
        }
        undeclared = [c for c in Capability if c not in host.declaration.capabilities]
        for capability in undeclared:
            protocol = getattr(interfaces, capability.value)
            for name in _public_methods(protocol):
                method = getattr(objects[capability], name)
                args, kwargs = _dummy_args(getattr(protocol, name))
                try:
                    await method(*args, **kwargs)
                except UnsupportedCapabilityError as exc:
                    assert exc.capability == capability.value, (capability, name)
                else:
                    raise AssertionError(f"{capability.value}.{name} did not raise")

    async def test_errors_are_code_host_errors(self) -> None:
        """B-ERR-6: every error the review flow raises is a CodeHostError."""
        for error in (
            NoChangeRequestError("a"),
            CodeHostTimeoutError("b", write=False),
            AuthenticationError("c"),
            UnsupportedCapabilityError("d", capability="Checks"),
            ConfigurationError("e"),
        ):
            assert isinstance(error, CodeHostError)

    # -- secrets -------------------------------------------------------------------------------------------------

    async def test_secret_sentinel_never_leaks(self) -> None:
        """B-SEC-1: the token `<token>` is in no exception message, __cause__ chain, log record or repr."""
        handler = _LogCapture()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        seen: list[str] = []
        try:
            scenarios: list[Callable[[ConformanceTarget], None]] = [
                lambda t: t.seeder.set_current_change_request(None),
                lambda t: t.seeder.reject_inline_reviews("rejected"),
                lambda t: t.seeder.reject_all_posts("all rejected"),
                lambda t: t.seeder.fail_posts_with(CodeHostTimeoutError("timed out", write=True)),
                lambda t: t.seeder.fail_posts_with(AuthenticationError("not authenticated")),
                lambda t: t.seeder.set_available(False),
            ]
            for arrange in scenarios:
                target = await self.make_target()
                arrange(target)
                seen.extend([repr(target.host), repr(target.host.declaration)])
                for call in (
                    target.host.change_requests.for_current_branch(target.repository),
                    self._post(target, report=_make_report(on_changed=1)),
                ):
                    try:
                        result = await call
                    except Exception as exc:
                        for item in _chain(exc):
                            seen.extend([str(item), repr(item), repr(item.args)])
                    else:
                        seen.append(repr(result))
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        seen.extend(handler.text)
        assert seen
        leaks = [s for s in seen if SECRET in s]
        assert not leaks, leaks
