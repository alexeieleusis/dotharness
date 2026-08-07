import pytest

from harness.lock import acquire_lock


def test_lock_acquired(tmp_xdg):
    with acquire_lock("acme-frontend"):
        lock_file = tmp_xdg / "locks" / "acme-frontend.lock"
        assert lock_file.exists()


def test_concurrent_lock_raises(tmp_xdg):
    with pytest.raises(SystemExit, match="already running"), acquire_lock("acme-frontend"):  # noqa: SIM117
        with acquire_lock("acme-frontend"):
            pass


def test_lock_released_after_context(tmp_xdg):
    with acquire_lock("acme-frontend"):
        pass
    # Can be re-acquired after release
    with acquire_lock("acme-frontend"):
        pass


def test_different_slugs_dont_conflict(tmp_xdg):
    with acquire_lock("acme-frontend"), acquire_lock("acme-backend"):
        pass  # no exception
