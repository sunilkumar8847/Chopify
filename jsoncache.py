"""Small JSON caches shared by several processes (Studio jobs, CLI runs).

* ``locked(path)``  - exclusive access across threads *and* processes: a
  lock file created with O_EXCL (works on Windows and POSIX) plus a thread
  lock. A lock file older than ``stale`` seconds was left by a crash and is
  removed.
* ``write_atomic(path, data)`` - unique temp file, fsync, os.replace; a crash
  mid-write leaves the previous file intact.
* ``merge(path, add, remove, version)`` - re-read under the lock, merge,
  write; concurrent writers never lose each other's entries.
"""
import contextlib
import json
import os
import threading
import time
from pathlib import Path

_THREAD_LOCK = threading.Lock()
LOCK_TIMEOUT = 30.0
LOCK_STALE = 120.0


@contextlib.contextmanager
def locked(path, timeout=None, stale=None):
    timeout = LOCK_TIMEOUT if timeout is None else timeout
    stale = LOCK_STALE if stale is None else stale
    lock = Path(str(path) + ".lock")
    deadline = time.monotonic() + timeout
    with _THREAD_LOCK:
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                break
            except (FileExistsError, PermissionError):
                # Windows: PermissionError while another process deletes it.
                try:
                    if time.time() - lock.stat().st_mtime > stale:
                        lock.unlink()
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{lock.name} is held by another process")
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                lock.unlink()
            except OSError:
                pass


def write_atomic(path, data):
    path = Path(path)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(20):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                # Windows: a reader has the file open for a moment.
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def load_entries(path, version):
    """The ``entries`` dict of a versioned cache file ({} if absent/stale)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("version") == version and isinstance(data.get("entries"), dict):
            return data["entries"]
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def merge(path, version, add=None, remove=(), timeout=None):
    """Merge into a versioned cache file. Returns the entries, or None if the
    lock could not be taken or the disk refused (callers carry on uncached)."""
    add = dict(add or {})
    remove = [key for key in remove if key not in add]
    if not add and not remove:
        return load_entries(path, version)
    try:
        with locked(path, timeout=timeout):
            entries = load_entries(path, version)
            before = dict(entries)
            entries.update(add)
            for key in remove:
                entries.pop(key, None)
            if entries != before:
                write_atomic(path, {"version": version, "entries": entries})
            return entries
    except (TimeoutError, OSError):
        return None
