import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path

XDG_RUNTIME = Path.home() / ".local/share/dotharness"


def working_dir_lock_key(prefix: str, working_dir: Path) -> str:
    """Lock key scoped to a specific checked-out working directory rather than just
    a repo name, so two separate clones of the same origin (e.g. a second clone
    dedicated to a cheaper/faster backend for one runner) get independent locks
    instead of contending for the same one. The key is derived solely from the
    resolved path — prefix is accepted for call-site readability but must not
    affect the key, otherwise two configs pointing at the same checkout under
    different repo.name/alias values would get different lock files and could
    concurrently detach/checkout/restore that checkout.

    hashlib, not the builtin hash(): str hashing is randomized per-process
    (PYTHONHASHSEED), so hash() would give two concurrent harness processes two
    different lock filenames for the same path — no actual mutual exclusion."""
    return hashlib.sha256(str(working_dir.resolve()).encode()).hexdigest()[:12]


@contextmanager
def acquire_lock(lock_key: str, *, blocking: bool = False):
    """Lock shared by every runner (address-comments, review-prs, review-requested,
    focused-review, self-review) that operates against a given checked-out working
    directory. All of them mutate that directory via detach/checkout/restore, so two
    different commands running against the same checkout at once can stomp each
    other's checkout mid-commit. Keying the lock on the working directory (not just
    the command) makes them mutually exclusive regardless of which command each side
    is running — while two runs against *different* checkouts (e.g. two clones of the
    same repo) don't contend for the same lock at all.

    By default, contention raises SystemExit immediately — appropriate when the lock
    guards a whole second instance of the tool that should just bail out. Pass
    ``blocking=True`` for locks that instead guard a transient operation (e.g. a
    shared git checkout): waiting for the holder to finish is cheap and correct there,
    whereas SystemExit would escape ordinary ``except Exception`` handling and abort
    the entire run instead of just the one operation."""
    lock_dir = XDG_RUNTIME / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / f"{lock_key}.lock"
    if not str(lock_file.resolve()).startswith(str(lock_dir.resolve())):
        raise ValueError(f"Invalid lock_key: {lock_key!r}")  # noqa: TRY003
    fd = lock_file.open("w")
    if blocking:
        fcntl.flock(fd, fcntl.LOCK_EX)
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as ex:
            fd.close()
            raise SystemExit(f"Another instance is already running for {lock_key}") from ex  # noqa: TRY003
    try:
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        fd.close()
