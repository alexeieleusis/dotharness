"""The in-memory fake passes the conformance suite and behaves as a consumer test needs."""

from dataclasses import replace

import pytest

from dotharness.code_host import HostSelection, create_code_host
from dotharness.code_host.declarations import Anchor, Capability
from dotharness.code_host.errors import (
    AuthenticationError,
    CodeHostError,
    CodeHostTimeoutError,
    ConfigurationError,
    NoChangeRequestError,
    UnsupportedCapabilityError,
)
from dotharness.code_host.models import (
    ChangeRequestState,
    CheckStatus,
    RepositoryRef,
    ReviewState,
    User,
    WorkItemState,
)
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


class _NoJobChecks:
    """A Checks facet that declares ``get_job`` unsupported and delegates the rest."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def get_job(self, repo: RepositoryRef, job_id: str) -> object:
        msg = "no jobs"
        raise UnsupportedCapabilityError(msg, capability="Checks")


class TestInMemoryWithAnUnsupportedOperation(CodeHostConformance):
    """An operation listed in ``ConformanceTarget.unsupported`` must raise, and the rest of the suite still runs."""

    async def make_target(self) -> ConformanceTarget:
        target = InMemoryCodeHost().target()
        host = replace(target.host, checks=_NoJobChecks(target.host.checks))
        return replace(target, host=host, unsupported=frozenset({"Checks.get_job"}))

    async def test_failed_log_and_get_job(self) -> None:
        """The inherited group reads ``get_job``. This target lists it as unsupported, so only ``failed_log`` runs."""
        target = await self.make_target()
        repo = target.repository
        cr = target.seeder.add_change_request(repo, head_reference="feature/a")
        target.seeder.add_job(repo, job_id="900", log="boom")
        target.seeder.add_check(repo, cr.number, name="build", status=CheckStatus.FAIL, job_id="900")
        (check,) = await target.host.checks.list_checks(repo, cr.number)
        assert await target.host.checks.failed_log(repo, check) == "boom"


class TestInMemorySpecifics:
    async def test_declares_all_six_capabilities_by_default(self) -> None:
        host = InMemoryCodeHost().code_host()
        assert host.declaration.capabilities == frozenset(Capability)
        assert len(host.declaration.capabilities) == 6

    async def test_capabilities_argument_selects_the_declared_subset(self) -> None:
        subset = frozenset({Capability.SESSION, Capability.REPOSITORY})
        host = InMemoryCodeHost(capabilities=subset).code_host()
        assert host.declaration.capabilities == subset
        with pytest.raises(UnsupportedCapabilityError) as caught:
            await host.work_items.list_open(RepositoryRef("example/repo"))
        assert caught.value.capability == "WorkItems"
        with pytest.raises(UnsupportedCapabilityError):
            await host.change_requests.get(RepositoryRef("example/repo"), 1)

    async def test_checks_can_be_declared(self) -> None:
        host = InMemoryCodeHost(capabilities=frozenset(Capability)).code_host()
        assert Capability.CHECKS in host.declaration.capabilities
        assert await host.checks.list_workflows(RepositoryRef("o/r")) == []

    async def test_undeclared_checks_raises_unsupported(self) -> None:
        host = InMemoryCodeHost(capabilities=frozenset(Capability) - {Capability.CHECKS}).code_host()
        repo = RepositoryRef("o/r")
        with pytest.raises(UnsupportedCapabilityError) as caught:
            await host.checks.list_checks(repo, 1)
        assert caught.value.capability == "Checks"
        with pytest.raises(UnsupportedCapabilityError):
            await host.checks.get_job(repo, "1")

    def test_declaration_capabilities_are_kept(self) -> None:
        from dataclasses import replace

        from dotharness.code_host.testing import DECLARATION

        declaration = replace(DECLARATION, capabilities=frozenset({Capability.SESSION}))
        assert InMemoryCodeHost(declaration=declaration).declaration.capabilities == {Capability.SESSION}
        with pytest.raises(ConfigurationError, match="disagree"):
            InMemoryCodeHost(declaration=declaration, capabilities=frozenset({Capability.REPOSITORY}))
        full = InMemoryCodeHost(declaration=replace(DECLARATION, capabilities=frozenset(Capability)))
        assert full.declaration.capabilities == frozenset(Capability)

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


REPO = RepositoryRef("o/a")


class TestSessionAndRepository:
    async def test_agent_environment_names(self) -> None:
        session = InMemoryCodeHost().code_host().session
        assert tuple(await session.agent_environment_names()) == ("GITHUB_TOKEN", "GH_TOKEN")

    async def test_agent_environment_base_first_and_credential_wins(self) -> None:
        session = InMemoryCodeHost(token="t0k").code_host().session  # noqa: S106
        env = await session.agent_environment({"PATH": "/bin", "GH_TOKEN": "stale"})
        assert env == {"PATH": "/bin", "GH_TOKEN": "t0k", "GITHUB_TOKEN": "t0k"}
        assert next(iter(env)) == "PATH"
        assert dict(await session.agent_environment()) == {"GITHUB_TOKEN": "t0k", "GH_TOKEN": "t0k"}

    async def test_add_repository_current_and_default_branch(self) -> None:
        fake = InMemoryCodeHost()
        other = fake.add_repository("o/other", default_branch="dev")
        assert other == RepositoryRef("o/other")
        assert (await fake.code_host().repository.current()).value == "example/repo"
        current = fake.add_repository("o/cur", default_branch="trunk", is_current=True)
        host = fake.code_host()
        assert await host.repository.current() == current
        assert await host.repository.default_branch(current) == "trunk"

    async def test_current_raises_when_no_repository_is_marked(self) -> None:
        host = InMemoryCodeHost(repository="").code_host()
        with pytest.raises(ConfigurationError):
            await host.repository.current()


class TestChangeRequestMethods:
    async def test_list_filters_are_logins(self) -> None:
        fake = InMemoryCodeHost()
        mine = fake.add_change_request(REPO, head_reference="a", author="me", requested_reviewers=["rev"])
        fake.add_change_request(REPO, head_reference="b", author="you")
        fake.add_change_request(REPO, head_reference="c", author="me", state=ChangeRequestState.MERGED)
        crs = fake.code_host().change_requests
        assert len(await crs.list(REPO)) == 2
        assert [c.number for c in await crs.list(REPO, author="me")] == [mine.number]
        assert [c.number for c in await crs.list(REPO, requested_reviewer="rev")] == [mine.number]
        assert [c.number for c in await crs.list(REPO, state=ChangeRequestState.MERGED)] == [3]
        assert len(await crs.list(REPO, author="", requested_reviewer="")) == 2
        assert await crs.list(REPO, assignee="me") == []
        assert await crs.list(REPO, author="nobody") == []

    async def test_list_respects_a_declared_bound(self) -> None:
        from dataclasses import replace

        from dotharness.code_host.declarations import ListBounds
        from dotharness.code_host.testing import DECLARATION

        limits = replace(DECLARATION.limits, list_bounds=ListBounds(default=3, per_operation={}))
        fake = InMemoryCodeHost(declaration=replace(DECLARATION, limits=limits))
        for i in range(5):
            fake.add_change_request(REPO, head_reference=str(i))
        assert len(await fake.code_host().change_requests.list(REPO)) == 3

    async def test_find_by_commit_commits_and_reviews(self) -> None:
        fake = InMemoryCodeHost()
        cr = fake.add_change_request(REPO, head_reference="a", head_revision="head")
        other = fake.add_change_request(REPO, head_reference="b")
        first = fake.add_commit(REPO, cr.number, revision="c1", message="one", authors=["ann"])
        second = fake.add_commit(REPO, cr.number, revision="c2")
        review = fake.add_review(REPO, cr.number, author="rev", state=ReviewState.CHANGES_REQUESTED, revision="c1")
        crs = fake.code_host().change_requests
        assert first.authors == (User("ann"),)
        assert first.committed_at is not None
        assert second.committed_at is not None
        assert first.committed_at < second.committed_at
        assert list(await crs.commits(REPO, cr.number)) == [first, second]
        assert list(await crs.commits(REPO, other.number)) == []
        assert list(await crs.reviews(REPO, cr.number)) == [review]
        assert [c.number for c in await crs.find_by_commit(REPO, "c2")] == [cr.number]
        assert [c.number for c in await crs.find_by_commit(REPO, "head")] == [cr.number]
        assert await crs.find_by_commit(REPO, "zzz") == []
        assert fake.reviews[REPO][cr.number] == (review,)
        for call in (crs.commits(REPO, 99), crs.reviews(REPO, 99), crs.viewed_files(REPO, 99)):
            with pytest.raises(CodeHostError):
                await call

    async def test_squash_merge_head_mismatch_and_success(self) -> None:
        fake = InMemoryCodeHost()
        cr = fake.add_change_request(REPO, head_reference="a", head_revision="abc")
        crs = fake.code_host().change_requests
        with pytest.raises(CodeHostError, match="head revision"):
            await crs.squash_merge(REPO, cr.number, expected_head="other")
        assert fake.writes == []
        assert await crs.state(REPO, cr.number) is ChangeRequestState.OPEN
        await crs.squash_merge(REPO, cr.number, expected_head="abc")
        assert await crs.state(REPO, cr.number) is ChangeRequestState.MERGED
        assert [c.operation for c in fake.writes] == ["ChangeRequests.squash_merge"]
        with pytest.raises(CodeHostError, match="not open"):
            await crs.squash_merge(REPO, cr.number, expected_head="abc")

    async def test_request_reviewer_is_idempotent(self) -> None:
        fake = InMemoryCodeHost()
        cr = fake.add_change_request(REPO, head_reference="a")
        crs = fake.code_host().change_requests
        await crs.request_reviewer(REPO, cr.number, "rev")
        await crs.request_reviewer(REPO, cr.number, "rev")
        assert (await crs.get(REPO, cr.number)).requested_reviewers == (User("rev"),)
        assert len(fake.writes) == 1

    async def test_approve_adds_an_approval_at_the_revision(self) -> None:
        fake = InMemoryCodeHost(user="me")
        cr = fake.add_change_request(REPO, head_reference="a")
        crs = fake.code_host().change_requests
        await crs.approve(REPO, cr.number, head_revision="rev-1")
        (review,) = await crs.reviews(REPO, cr.number)
        assert (review.author, review.state, review.revision) == (User("me"), ReviewState.APPROVED, "rev-1")
        with pytest.raises(CodeHostError):
            await crs.approve(REPO, 99, head_revision="x")

    async def test_viewed_files(self) -> None:
        fake = InMemoryCodeHost()
        cr = fake.add_change_request(REPO, head_reference="a")
        crs = fake.code_host().change_requests
        assert dict(await crs.viewed_files(REPO, cr.number)) == {}
        await crs.mark_viewed(REPO, cr.number, "src/a.py")
        await crs.mark_viewed(REPO, cr.number, "src/a.py")  # a no-op
        assert dict(await crs.viewed_files(REPO, cr.number)) == {"src/a.py": True}
        assert len(fake.writes) == 1


class TestDiscussionMethods:
    async def test_comments_threads_and_post(self) -> None:
        fake = InMemoryCodeHost(user="me")
        cr = fake.add_change_request(REPO, head_reference="a")
        seeded = fake.add_comment(REPO, cr.number, author="ann", body="hello")
        inline = fake.add_comment(REPO, cr.number, author="bob", body="inline")
        anchor = Anchor("src/a.py", 3, "RIGHT")
        thread = fake.add_thread(REPO, cr.number, anchor=anchor, comments=[inline], is_resolved=True)
        discussion = fake.code_host().discussion
        assert seeded.author == User("ann")
        assert thread.is_resolved is True
        assert thread.anchor == anchor
        assert list(await discussion.comments(REPO, cr.number)) == [seeded, inline]
        assert list(await discussion.review_threads(REPO, cr.number)) == [thread]
        assert list(await discussion.comments(REPO, 99)) == []
        posted = await discussion.post_comment(REPO, cr.number, "from me")
        assert posted.author == User("me")
        assert posted.body == "from me"
        assert (await discussion.comments(REPO, cr.number))[-1] == posted
        assert fake.comments[REPO][cr.number][-1] == posted
        assert [c.operation for c in fake.writes] == ["Discussion.post_comment"]

    async def test_reply_extends_the_thread(self) -> None:
        fake = InMemoryCodeHost(user="me")
        cr = fake.add_change_request(REPO, head_reference="a")
        root = fake.add_comment(REPO, cr.number, author="bob", body="root")
        thread = fake.add_thread(REPO, cr.number, anchor=None, comments=[root])
        discussion = fake.code_host().discussion
        reply = await discussion.reply(REPO, cr.number, thread.id, "answer")
        (updated,) = await discussion.review_threads(REPO, cr.number)
        assert updated.comments == (root, reply)
        assert fake.threads[REPO][cr.number] == (updated,)
        with pytest.raises(CodeHostError, match="not found"):
            await discussion.reply(REPO, cr.number, "missing", "x")

    async def test_timestamps_increase_across_object_kinds(self) -> None:
        fake = InMemoryCodeHost()
        cr = fake.add_change_request(REPO, head_reference="a")
        one = fake.add_comment(REPO, cr.number, author="a", body="1")
        two = fake.add_comment(REPO, cr.number, author="a", body="2")
        assert cr.created_at < one.created_at < two.created_at

    async def test_seeders_need_an_existing_change_request(self) -> None:
        fake = InMemoryCodeHost()
        calls = (
            lambda: fake.add_commit(REPO, 1, revision="r"),
            lambda: fake.add_review(REPO, 1, author="a", state=ReviewState.COMMENTED),
            lambda: fake.add_comment(REPO, 1, author="a", body="b"),
            lambda: fake.add_thread(REPO, 1, anchor=None, comments=[]),
            lambda: fake.add_check(REPO, 1, name="n", status=CheckStatus.PASS),
        )
        for call in calls:
            with pytest.raises(ValueError, match="does not exist"):
                call()


class TestChecksFacet:
    async def test_checks_workflows_and_jobs(self) -> None:
        fake = InMemoryCodeHost()
        cr = fake.add_change_request(REPO, head_reference="a")
        ok = fake.add_check(REPO, cr.number, name="lint", status=CheckStatus.PASS, required=True)
        bad = fake.add_check(REPO, cr.number, name="test", status=CheckStatus.FAIL, job_id="77")
        wait = fake.add_check(REPO, cr.number, name="deploy", status=CheckStatus.PENDING)
        workflow = fake.add_workflow(REPO, name="ci")
        job = fake.add_job(REPO, job_id="77", failed_step="run tests", log="boom")
        checks = fake.code_host().checks
        assert list(await checks.list_checks(REPO, cr.number)) == [ok, bad, wait]
        assert list(await checks.list_checks(REPO, cr.number, required_only=True)) == [ok]
        assert await checks.list_checks(REPO, 99) == []
        assert (bad.job_id, wait.completed_at) == ("77", None)
        assert ok.completed_at is not None
        assert fake.checks[REPO][cr.number] == (ok, bad, wait)
        assert list(await checks.list_workflows(REPO)) == [workflow]
        assert await checks.list_workflows(RepositoryRef("o/none")) == []
        assert await checks.failed_log(REPO, bad) == "boom"
        assert await checks.get_job(REPO, "77") == job
        assert fake.writes == []

    async def test_failed_log_and_get_job_errors(self) -> None:
        fake = InMemoryCodeHost()
        cr = fake.add_change_request(REPO, head_reference="a")
        no_job = fake.add_check(REPO, cr.number, name="x", status=CheckStatus.FAIL)
        unknown = fake.add_check(REPO, cr.number, name="y", status=CheckStatus.FAIL, job_id="5")
        checks = fake.code_host().checks
        for call in (
            checks.failed_log(REPO, no_job),
            checks.failed_log(REPO, unknown),
            checks.get_job(REPO, "5"),
        ):
            with pytest.raises(CodeHostError):
                await call

    async def test_job_without_failed_step(self) -> None:
        fake = InMemoryCodeHost()
        fake.add_job(REPO, job_id="1")
        job = await fake.code_host().checks.get_job(REPO, "1")
        assert (job.id, job.failed_step) == ("1", None)


class TestNewWriteInjection:
    async def test_squash_merge_timeout_applied_and_not_applied(self) -> None:
        fake = InMemoryCodeHost()
        cr = fake.add_change_request(REPO, head_reference="a", head_revision="h")
        crs = fake.code_host().change_requests
        fake.fail_next("ChangeRequests.squash_merge", CodeHostTimeoutError("t", write=True), apply=False)
        with pytest.raises(CodeHostTimeoutError) as caught:
            await crs.squash_merge(REPO, cr.number, expected_head="h")
        assert caught.value.write is True
        assert await crs.state(REPO, cr.number) is ChangeRequestState.OPEN
        assert fake.writes == []
        fake.fail_next("ChangeRequests.squash_merge", CodeHostTimeoutError("t", write=True), apply=True)
        with pytest.raises(CodeHostTimeoutError):
            await crs.squash_merge(REPO, cr.number, expected_head="h")
        assert await crs.state(REPO, cr.number) is ChangeRequestState.MERGED
        assert [c.operation for c in fake.writes] == ["ChangeRequests.squash_merge"]

    @pytest.mark.parametrize(
        "operation",
        [
            "ChangeRequests.request_reviewer",
            "ChangeRequests.approve",
            "ChangeRequests.mark_viewed",
            "Discussion.post_comment",
            "Discussion.reply",
        ],
    )
    async def test_every_new_write_accepts_apply(self, operation: str) -> None:
        fake = InMemoryCodeHost()
        fake.fail_next(operation, CodeHostTimeoutError("t", write=True), apply=True)

    async def test_reads_reject_apply_and_are_recorded(self) -> None:
        fake = InMemoryCodeHost()
        for operation in ("Checks.list_checks", "ChangeRequests.list", "Session.agent_environment"):
            with pytest.raises(ValueError, match="apply=True"):
                fake.fail_next(operation, CodeHostError("x"), apply=True)
        cr = fake.add_change_request(REPO, head_reference="a")
        host = fake.code_host()
        await host.checks.list_checks(REPO, cr.number)
        await host.change_requests.list(REPO)
        await host.session.agent_environment_names()
        assert [c.operation for c in fake.calls] == [
            "Checks.list_checks",
            "ChangeRequests.list",
            "Session.agent_environment_names",
        ]
        fake.fail_next("Checks.list_checks", CodeHostTimeoutError("slow", write=False))
        with pytest.raises(CodeHostTimeoutError):
            await host.checks.list_checks(REPO, cr.number)

    async def test_apply_reply_takes_effect_then_raises(self) -> None:
        fake = InMemoryCodeHost()
        cr = fake.add_change_request(REPO, head_reference="a")
        thread = fake.add_thread(REPO, cr.number, anchor=None, comments=[])
        fake.fail_next("Discussion.reply", CodeHostTimeoutError("t", write=True), apply=True)
        discussion = fake.code_host().discussion
        with pytest.raises(CodeHostTimeoutError):
            await discussion.reply(REPO, cr.number, thread.id, "x")
        assert len(fake.threads[REPO][cr.number][0].comments) == 1


class TestWorkItemGet:
    async def test_none_for_absent_and_error_for_inconclusive(self) -> None:
        fake = InMemoryCodeHost()
        item = fake.add_work_item(REPO, title="t")
        items = fake.code_host().work_items
        assert await items.get(REPO, item.number) == item
        assert await items.get(REPO, 99) is None
        fake.fail_next("WorkItems.get", CodeHostError("inconclusive"))
        with pytest.raises(CodeHostError, match="inconclusive"):
            await items.get(REPO, item.number)

    async def test_linked_work_items_returns_work_items(self) -> None:
        fake = InMemoryCodeHost()
        item = fake.add_work_item(REPO, title="t")
        cr = fake.add_change_request(REPO, head_reference="a")
        fake.link_closing(REPO, work_item=item.number, change_request=cr.number)
        assert list(await fake.code_host().work_items.linked_work_items(REPO, cr.number)) == [item]


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
