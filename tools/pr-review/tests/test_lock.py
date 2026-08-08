from multiprocessing import Process

import pytest

from harness import lock
from harness.lock import acquire_lock


def _child_acquire(xdg_path):
    lock.XDG_RUNTIME = xdg_path
    with acquire_lock("acme-frontend"):
        pass


def test_lock_acquired(tmp_xdg):
    with acquire_lock("acme-frontend"):
        lock_file = tmp_xdg / "locks" / "acme-frontend.lock"
        assert lock_file.exists()


def test_concurrent_lock_raises(tmp_xdg):
    with acquire_lock("acme-frontend"):
        p = Process(target=_child_acquire, args=(str(tmp_xdg),))
        p.start()
        p.join()
        assert p.exitcode == 1


def test_lock_released_after_context(tmp_xdg):
    with acquire_lock("acme-frontend"):
        pass
    # Can be re-acquired after release
    with acquire_lock("acme-frontend"):
        pass


def test_different_slugs_dont_conflict(tmp_xdg):
    with acquire_lock("acme-frontend"), acquire_lock("acme-backend"):
        pass  # no exception


def test_lock_released_on_exception(tmp_xdg):
    with pytest.raises(ValueError), acquire_lock("acme-frontend"):
        raise ValueError("boom")
    # Lock must be released — re-acquisition must succeed
    with acquire_lock("acme-frontend"):
        pass


def test_body_blocking_io_error_is_not_mislabeled(tmp_xdg):
    with pytest.raises(BlockingIOError), acquire_lock("acme-frontend"):
        raise BlockingIOError
