"""Shared fixtures for orchestrator tests."""

import pytest

from dotharness.code_host.testing import InMemoryCodeHost
from tests.vibe_heal.shared_orchestrator_tests import temp_project  # noqa: F401


@pytest.fixture(autouse=True)
def fake_code_host(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> InMemoryCodeHost:
    """Start the CLI with an in-memory code host, so no test depends on gh or on a mocked config's host fields.

    A test module opts out with a module attribute ``REAL_CODE_HOST_STARTUP = True``.
    """
    fake = InMemoryCodeHost()
    fake.set_current_change_request(1)
    if not getattr(request.module, "REAL_CODE_HOST_STARTUP", False):
        monkeypatch.setattr("dotharness.vibe_heal.cli._start_code_host", lambda _config: fake.code_host())
        monkeypatch.setattr("dotharness.vibe_heal.cli._start_code_host_for_post", lambda _env_file: fake.code_host())
    return fake
