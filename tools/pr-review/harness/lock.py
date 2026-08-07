import fcntl
from contextlib import contextmanager
from pathlib import Path

XDG_RUNTIME = Path.home() / ".local/share/dotharness"


@contextmanager
def acquire_lock(repo_slug: str):
    """Repo-wide lock shared by every runner (address-comments, review-prs,
    review-requested, focused-review, self-review). All of them mutate the same
    checked-out working directory via detach/checkout/restore, so two different
    commands running against the same repo at once can stomp each other's checkout
    mid-commit. Keying the lock on repo_slug alone (not per-command) makes them
    mutually exclusive regardless of which command each side is running."""
    lock_dir = XDG_RUNTIME / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / f"{repo_slug}.lock"
    fd = None
    try:
        fd = lock_file.open("w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except BlockingIOError as ex:
        raise SystemExit(f"Another instance is already running for {repo_slug}") from ex  # noqa: TRY003
    finally:
        if fd is not None:
            fd.close()
