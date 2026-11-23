"""Machine-wide request slots per AI provider.

Studio and every worker process share one upload link. The gateway's
in-process semaphore alone let each process send two requests at once, so
Studio plus two analysis workers could upload six audio pieces together and
all of them crawl on a slow connection. A slot here is an OS file lock
(``<config>/ai_slots/<provider>.<n>.lock``): at most ``limit`` requests per
provider run on this computer at any time, across processes. The operating
system releases a lock when its process exits or is killed, so a crash never
leaves a slot taken.
"""
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import youtube_auth
from ai.base import TemporaryError

if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle):
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(handle):
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(handle):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

_local = threading.Lock()


class SlotTimeout(TemporaryError):
    """No slot became free in time: the gateway treats it as a temporary outage."""


def folder():
    path = youtube_auth.config_dir() / "ai_slots"
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def slot(provider, limit=2, wait=600.0, cancelled=None, poll=0.2):
    """Hold one of ``limit`` machine-wide slots for ``provider``."""
    limit = max(1, int(limit))
    deadline = time.monotonic() + wait
    handle = None
    while handle is None:
        for index in range(limit):
            path = folder() / f"{provider}.{index}.lock"
            candidate = open(path, "a+b")
            with _local:                 # msvcrt locks are per process: serialise threads
                taken = _try_lock(candidate)
            if taken:
                handle = candidate
                break
            candidate.close()
        if handle is None:
            if cancelled is not None and cancelled.is_set():
                raise SlotTimeout("Cancelled while waiting for a free AI request slot.", provider)
            if time.monotonic() >= deadline:
                raise SlotTimeout(f"No free {provider} request slot after {wait:.0f}s.", provider)
            time.sleep(poll)
    try:
        yield
    finally:
        try:
            with _local:
                _unlock(handle)
        except OSError:
            pass
        handle.close()


def busy(provider, limit=2):
    """How many slots are taken right now (for status displays)."""
    taken = 0
    for index in range(max(1, int(limit))):
        path = folder() / f"{provider}.{index}.lock"
        with open(path, "a+b") as handle:
            with _local:
                if _try_lock(handle):
                    _unlock(handle)
                else:
                    taken += 1
    return taken


def _main():                              # used by the cross-process test
    provider, seconds, out = sys.argv[1], float(sys.argv[2]), Path(sys.argv[3])
    with slot(provider, limit=int(sys.argv[4])):
        out.write_text(f"{time.time()} start\n", encoding="utf-8")
        time.sleep(seconds)
        with out.open("a", encoding="utf-8") as f:
            f.write(f"{time.time()} end\n")


if __name__ == "__main__":
    _main()
