"""The in-memory fake passes the conformance suite and behaves as a consumer test needs."""

import pytest

from dotharness.code_host import HostSelection, create_code_host
from dotharness.code_host.declarations import Capability
from dotharness.code_host.errors import (
    AuthenticationError,
    CodeHostError,
    CodeHostTimeoutError,
    ConfigurationError,
    NoChangeRequestError,
    UnsupportedCapabilityError,
)
from dotharness.code_host.models import ChangeRequestState, RepositoryRef, User, WorkItemState
from dotharness.code_host.testing import (
    FAKE_REGISTRATION,
    CodeHostConformance,
    ConformanceTarget,
    InMemoryCodeHost,
    PostedItem,
)
from dotharness.code_host.version import CODE_HOST_INTERFACE_VERSION


class TestInMemoryCodeHost(CodeHostConformance):
    async def make_target(self) -> ConformanceTarget:
        return InMemoryCodeHost().target()


class TestInMemoryWithoutWorkItems(CodeHostConformance):
    """B-ERR-4 for an undeclared capability, and no work-item group is run."""

    async def make_target(self) -> ConformanceTarget:
        return InMemoryCodeHost(
            capabilities=frozenset(Capability) - {Capability.WORK_ITEMS, Capability.CHECKS}
        ).target()


class TestInMemorySpecifics:
    async def test_declares_every_implemented_capability(self) -> None:
        host = InMemoryCodeHost().code_host()
        assert host.declaration.capabilities == frozenset(Capability) - {Capability.CHECKS}

    async def test_capabilities_argument_selects_the_declared_subset(self) -> None:
        subset = frozenset({Capability.SESSION, Capability.REPOSITORY})
        host = InMemoryCodeHost(capabilities=subset).code_host()
        assert host.declaration.capabilities == subset
        with pytest.raises(UnsupportedCapabilityError) as caught:
            await host.work_items.list_open(RepositoryRef("example/repo"))
        assert caught.value.capability == "WorkItems"
        with pytest.raises(UnsupportedCapabilityError):
            await host.change_requests.get(RepositoryRef("example/repo"), 1)

    async def test_checks_cannot_be_declared(self) -> None:
        with pytest.raises(ConfigurationError, match="Checks"):
            InMemoryCodeHost(capabilities=frozenset(Capability))

    def test_declaration_capabilities_are_kept(self) -> None:
        from dataclasses import replace

        from dotharness.code_host.testing import DECLARATION

        declaration = replace(DECLARATION, capabilities=frozenset({Capability.SESSION}))
        assert InMemoryCodeHost(declaration=declaration).declaration.capabilities == {Capability.SESSION}
        with pytest.raises(ConfigurationError, match="disagree"):
            InMemoryCodeHost(declaration=declaration, capabilities=frozenset({Capability.REPOSITORY}))
        with pytest.raises(ConfigurationError, match="Checks"):
            InMemoryCodeHost(declaration=replace(DECLARATION, capabilities=frozenset(Capability)))

    async def test_repository_and_user(self) -> None:
        fake = InMemoryCodeHost(repository="o/r", user="me")
        host = fake.code_host()
        repo = await host.repository.current()
        assert repo.value == "o/r"
        assert await host.repository.resolve("a/b") == type(repo)("a/b")
        assert await host.repository.default_branch(repo) == "main"
        assert await host.repository.display_name(repo) == "o/r"
        assert await host.session.current_user() == "me"
        fake.set_repository("x/y")
        assert (await host.repository.current()).value == "x/y"
        fake.set_user("you")
        fake.set_default_branch("trunk")
        assert await host.session.current_user() == "you"
        assert await host.repository.default_branch(repo) == "trunk"

    async def test_resolve_empty_identity_raises(self) -> None:
        with pytest.raises(ConfigurationError):
            await InMemoryCodeHost().code_host().repository.resolve("")

    async def test_unimplemented_methods_raise_unsupported(self) -> None:
        host = InMemoryCodeHost().code_host()
        repo = await host.repository.current()
        with pytest.raises(UnsupportedCapabilityError):
            await host.change_requests.list(repo)
        with pytest.raises(UnsupportedCapabilityError):
            await host.discussion.comments(repo, 1)
        with pytest.raises(UnsupportedCapabilityError):
            await host.session.agent_environment_names()
        with pytest.raises(UnsupportedCapabilityError):
            await host.session.agent_environment()
        with pytest.raises(AttributeError):
            getattr(host.checks, "_private")  # noqa: B009
        with pytest.raises(AttributeError):
            getattr(host.checks, "not_a_method")  # noqa: B009

    async def test_credential_is_the_token_and_hides_it_in_repr(self) -> None:
        credential = await InMemoryCodeHost(token="t0k").code_host().session.credential()  # noqa: S106
        assert credential.token == "t0k"  # noqa: S105
        assert "t0k" not in repr(credential)

    async def test_clear_failures_restores_posting(self) -> None:
        fake = InMemoryCodeHost()
        host = fake.code_host()
        fake.reject_all_posts("no")
        fake.clear_failures()
        result = await host.discussion.post_review(await host.repository.current(), 1, body="b", inline_comments=[])
        assert result.fell_back is False
        assert fake.posts() == [PostedItem("review", "b", 0)]


class TestSeeding:
    async def test_numbers_are_per_repository_and_timestamps_increase(self) -> None:
        fake = InMemoryCodeHost()
        a, b = RepositoryRef("o/a"), RepositoryRef("o/b")
        first = fake.add_work_item(a, title="1")
        second = fake.add_work_item(a, title="2")
        other = fake.add_work_item(b, title="3")
        explicit = fake.add_work_item(a, title="4", number=40)
        assert (first.number, second.number, other.number, explicit.number) == (1, 2, 1, 40)
        assert first.created_at < second.created_at < other.created_at
        assert fake.add_work_item(a, title="5").number == 41
        with pytest.raises(ValueError, match="already used"):
            fake.add_work_item(a, title="dup", number=1)
        assert [w.title for w in fake.work_items[a]] == ["1", "2", "4", "5"]

    async def test_add_work_item_models(self) -> None:
        fake = InMemoryCodeHost()
        repo = RepositoryRef("o/a")
        item = fake.add_work_item(
            repo, title="t", body="b", labels=["x"], assignees=["ann"], state=WorkItemState.CLOSED
        )
        assert item == fake.work_items[repo][0]
        assert (item.title, item.body, item.labels, item.state) == ("t", "b", ("x",), WorkItemState.CLOSED)
        assert item.assignees == (User("ann"),)

    async def test_add_change_request_models_and_defaults(self) -> None:
        fake = InMemoryCodeHost()
        repo = RepositoryRef("o/a")
        cr = fake.add_change_request(repo, head_reference="h", requested_reviewers=["rev"])
        assert cr == fake.change_requests[repo][0]
        assert (cr.number, cr.base_reference, cr.head_revision, cr.is_draft) == (1, "main", "rev-1", False)
        assert cr.author == User("fake-user")
        assert cr.requested_reviewers == (User("rev"),)
        assert cr.state is ChangeRequestState.OPEN
        assert "pull" in cr.url
        explicit = fake.add_change_request(repo, head_reference="g", head_revision="sha", number=9, author="zed")
        assert (explicit.number, explicit.head_revision, explicit.author) == (9, "sha", User("zed"))

    async def test_current_change_request_shares_the_store_read_by_get_and_state(self) -> None:
        host = InMemoryCodeHost()
        target = host.target()
        repo = target.repository
        crs = target.host.change_requests
        host.set_current_change_request(7, "release")
        current = await crs.for_current_branch(repo)
        assert (current.number, current.base_reference) == (7, "release")
        assert (await crs.get(repo, 7)).number == 7
        assert await crs.state(repo, 7) is ChangeRequestState.OPEN
        host.set_current_change_request(None)
        with pytest.raises(NoChangeRequestError):
            await crs.for_current_branch(repo)
        assert (await crs.get(repo, 7)).number == 7

    async def test_link_closing_needs_both_sides(self) -> None:
        fake = InMemoryCodeHost()
        repo = RepositoryRef("o/a")
        wi = fake.add_work_item(repo, title="t")
        cr = fake.add_change_request(repo, head_reference="h")
        with pytest.raises(ValueError, match="work item 9"):
            fake.link_closing(repo, work_item=9, change_request=cr.number)
        with pytest.raises(ValueError, match="change request 9"):
            fake.link_closing(repo, work_item=wi.number, change_request=9)
        fake.link_closing(repo, work_item=wi.number, change_request=cr.number)
        host = fake.code_host()
        assert [c.number for c in await host.work_items.closing_change_requests(repo, wi.number)] == [cr.number]

    async def test_state_views_are_read_only(self) -> None:
        fake = InMemoryCodeHost()
        with pytest.raises(TypeError):
            fake.work_items[RepositoryRef("o/a")] = ()  # type: ignore[index]  # ty: ignore[invalid-assignment]


class TestWorkItemBehavior:
    async def test_empty_filter_strings_mean_no_filter(self) -> None:
        fake = InMemoryCodeHost()
        repo = RepositoryRef("o/a")
        fake.add_work_item(repo, title="t", labels=["x"], assignees=["ann"])
        found = await fake.code_host().work_items.list_open(repo, assignee="", label="")
        assert [w.title for w in found] == ["t"]

    async def test_list_open_uses_the_declared_bound(self) -> None:
        fake = InMemoryCodeHost()
        repo = RepositoryRef("o/a")
        for i in range(105):
            fake.add_work_item(repo, title=str(i))
        assert len(await fake.code_host().work_items.list_open(repo)) == 100

    async def test_remove_label_errors(self) -> None:
        fake = InMemoryCodeHost()
        repo = RepositoryRef("o/a")
        item = fake.add_work_item(repo, title="t", labels=["x"])
        items = fake.code_host().work_items
        with pytest.raises(CodeHostError, match="does not exist"):
            await items.remove_label(repo, item.number, "never-seen")
        with pytest.raises(CodeHostError, match="not found"):
            await items.remove_label(repo, 99, "x")
        await items.remove_label(repo, item.number, "x")
        await items.remove_label(repo, item.number, "x")  # the repository still has the label: a no-op
        assert fake.work_items[repo][0].labels == ()

    async def test_get_and_state_of_missing_change_request(self) -> None:
        host = InMemoryCodeHost().code_host()
        repo = RepositoryRef("o/a")
        with pytest.raises(CodeHostError):
            await host.change_requests.get(repo, 1)
        with pytest.raises(CodeHostError):
            await host.change_requests.state(repo, 1)

    async def test_find_by_branch_lists_the_most_recent_first(self) -> None:
        fake = InMemoryCodeHost()
        repo = RepositoryRef("o/a")
        old = fake.add_change_request(repo, head_reference="h", state=ChangeRequestState.CLOSED)
        new = fake.add_change_request(repo, head_reference="h")
        found = await fake.code_host().change_requests.find_by_branch(repo, "h", state=None)
        assert [c.number for c in found] == [new.number, old.number]

    async def test_create_draft_allowed_again_after_the_first_closed(self) -> None:
        fake = InMemoryCodeHost(user="me")
        repo = RepositoryRef("o/a")
        fake.add_change_request(repo, head_reference="h", state=ChangeRequestState.MERGED)
        created = await fake.code_host().change_requests.create_draft(repo, head="h", base="main", title="t", body="b")
        assert created.author == User("me")
        assert (created.title, created.body, created.is_draft) == ("t", "b", True)


class TestFailureInjectionAndInspection:
    async def test_fail_next_raises_once_and_is_recorded(self) -> None:
        fake = InMemoryCodeHost()
        repo = RepositoryRef("o/a")
        wi = fake.add_work_item(repo, title="t")
        fake.fail_next("WorkItems.get", CodeHostTimeoutError("slow", write=False))
        items = fake.code_host().work_items
        with pytest.raises(CodeHostTimeoutError):
            await items.get(repo, wi.number)
        assert await items.get(repo, wi.number) == wi
        assert [(c.operation, c.outcome) for c in fake.calls] == [
            ("WorkItems.get", "CodeHostTimeoutError"),
            ("WorkItems.get", "ok"),
        ]
        assert fake.calls[1].args == (repo, wi.number)
        assert fake.writes == []

    async def test_fail_next_is_a_queue(self) -> None:
        fake = InMemoryCodeHost()
        fake.fail_next("Repository.current", CodeHostError("one"))
        fake.fail_next("Repository.current", AuthenticationError("two"))
        repository = fake.code_host().repository
        with pytest.raises(CodeHostError, match="one"):
            await repository.current()
        with pytest.raises(AuthenticationError):
            await repository.current()
        await repository.current()

    async def test_fail_always_until_cleared_and_wildcard(self) -> None:
        fake = InMemoryCodeHost()
        host = fake.code_host()
        fake.fail_always("*", AuthenticationError("no"))
        for _ in range(2):
            with pytest.raises(AuthenticationError):
                await host.repository.current()
            with pytest.raises(AuthenticationError):
                await host.session.current_user()
        assert await host.session.available() is True  # available() never raises
        fake.clear_failures()
        await host.repository.current()

    async def test_specific_operation_wins_over_wildcard(self) -> None:
        fake = InMemoryCodeHost()
        fake.fail_always("*", AuthenticationError("any"))
        fake.fail_always("Repository.current", CodeHostError("specific"))
        with pytest.raises(CodeHostError, match="specific"):
            await fake.code_host().repository.current()

    async def test_apply_true_takes_effect_then_raises(self) -> None:
        fake = InMemoryCodeHost()
        repo = RepositoryRef("o/a")
        item = fake.add_work_item(repo, title="t", labels=["x"])
        items = fake.code_host().work_items
        fake.fail_next("WorkItems.remove_label", CodeHostTimeoutError("took effect", write=True), apply=True)
        with pytest.raises(CodeHostTimeoutError):
            await items.remove_label(repo, item.number, "x")
        assert fake.work_items[repo][0].labels == ()
        assert [c.operation for c in fake.writes] == ["WorkItems.remove_label"]
        assert fake.writes[0].outcome == "CodeHostTimeoutError"

    async def test_apply_true_injection_wins_when_the_action_itself_raises(self) -> None:
        fake = InMemoryCodeHost()
        repo = RepositoryRef("o/a")
        fake.add_change_request(repo, head_reference="feature/x", base_reference="main")
        fake.fail_next("ChangeRequests.create_draft", CodeHostTimeoutError("took effect", write=True), apply=True)
        with pytest.raises(CodeHostTimeoutError):
            await fake.code_host().change_requests.create_draft(
                repo, head="feature/x", base="main", title="t", body="b"
            )
        assert fake.calls[-1].outcome == "CodeHostTimeoutError"

    async def test_apply_false_does_not_change_state(self) -> None:
        fake = InMemoryCodeHost()
        repo = RepositoryRef("o/a")
        item = fake.add_work_item(repo, title="t", labels=["x"])
        items = fake.code_host().work_items
        fake.fail_next("WorkItems.remove_label", CodeHostTimeoutError("did not", write=True))
        with pytest.raises(CodeHostTimeoutError):
            await items.remove_label(repo, item.number, "x")
        assert fake.work_items[repo][0].labels == ("x",)
        assert fake.writes == []

    async def test_writes_are_the_calls_that_changed_state(self) -> None:
        fake = InMemoryCodeHost()
        repo = RepositoryRef("o/a")
        item = fake.add_work_item(repo, title="t", labels=["x"])
        host = fake.code_host()
        await host.work_items.list_open(repo)
        await host.work_items.remove_label(repo, item.number, "x")
        await host.work_items.remove_label(repo, item.number, "x")  # a no-op: not a write
        await host.change_requests.create_draft(repo, head="h", base="main", title="t", body="b")
        with pytest.raises(CodeHostError):
            await host.change_requests.create_draft(repo, head="h", base="main", title="t", body="b")
        await host.discussion.post_review(repo, 1, body="b", inline_comments=[])
        assert [c.operation for c in fake.writes] == [
            "WorkItems.remove_label",
            "ChangeRequests.create_draft",
            "Discussion.post_review",
        ]
        assert len(fake.calls) == 6
        assert fake.calls[-1].kwargs["body"] == "b"

    async def test_failed_write_before_effect_is_not_a_write(self) -> None:
        fake = InMemoryCodeHost()
        host = fake.code_host()
        fake.reject_all_posts("x")
        with pytest.raises(CodeHostError):
            await host.discussion.post_review(RepositoryRef("o/a"), 1, body="b", inline_comments=[])
        assert fake.writes == []
        assert fake.calls[0].outcome == "CodeHostError"

    @pytest.mark.parametrize(
        ("operation", "apply", "message"),
        [
            ("WorkItems.nope", False, "unknown operation"),
            ("get", False, "unknown operation"),
            ("WorkItems.get", True, "apply=True"),
            ("*", True, "apply=True"),
        ],
    )
    async def test_bad_injection_is_rejected(self, operation: str, apply: bool, message: str) -> None:
        fake = InMemoryCodeHost()
        with pytest.raises(ValueError, match=message):
            fake.fail_next(operation, CodeHostError("x"), apply=apply)
        with pytest.raises(ValueError, match=message):
            fake.fail_always(operation, CodeHostError("x"), apply=apply)

    async def test_error_text_is_kept_for_each_kind(self) -> None:
        fake = InMemoryCodeHost()
        fake.fail_next("ChangeRequests.find_by_branch", NoChangeRequestError("none"))
        with pytest.raises(NoChangeRequestError):
            await fake.code_host().change_requests.find_by_branch(RepositoryRef("o/a"), "b")


class TestFakeRegistration:
    async def test_registration_matches_the_interface_version(self) -> None:
        assert FAKE_REGISTRATION.interface_version == CODE_HOST_INTERFACE_VERSION

    async def test_selectable_by_dotted_path(self) -> None:
        host = create_code_host(
            HostSelection(
                setting="code_host",
                name="dotharness.code_host.testing:FAKE_REGISTRATION",
                options={"repository": "o/r", "user": "me"},
                token_cmd=["never-run"],
            )
        )
        assert (await host.repository.current()).value == "o/r"
        assert await host.session.current_user() == "me"
        assert Capability.WORK_ITEMS in host.declaration.capabilities

    async def test_default_options(self) -> None:
        host = create_code_host(
            HostSelection(
                setting="code_host", name="dotharness.code_host.testing:FAKE_REGISTRATION", options={}, token_cmd=None
            )
        )
        assert (await host.repository.current()).value == "example/repo"


class _LeakyHost(InMemoryCodeHost):
    def require_client(self) -> None:
        if not self._available:
            msg = f"client unavailable (token={self._token})"
            raise OSError(msg)


class _LeakyConformance(CodeHostConformance):
    async def make_target(self) -> ConformanceTarget:
        return _LeakyHost().target()


class _WrongSecretConformance(CodeHostConformance):
    async def make_target(self) -> ConformanceTarget:
        return InMemoryCodeHost(token="other").target()  # noqa: S106


class TestSecretCheckCanFail:
    async def test_leaking_host_fails(self) -> None:
        with pytest.raises(AssertionError):
            await _LeakyConformance().test_secret_sentinel_never_leaks()

    async def test_host_without_sentinel_fails(self) -> None:
        with pytest.raises(AssertionError, match="SECRET"):
            await _WrongSecretConformance().test_secret_sentinel_never_leaks()
