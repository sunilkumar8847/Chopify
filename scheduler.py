"""Persistent five-source scheduler (Phase 2).

A *source* is one live session or one recorded video (a project). At most
``max_sources`` (5) sources are active at once; more wait in a queue (live
sessions first) or are rejected with a reason when the queue is full.

Each source owns a chain of *tasks*. Every task has a *stage* with its own
worker pool, so a slow stage never blocks another:

    ingest    acquiring media (live sessions, probing files)     default 5
    analysis  transcription, scoring (local analysis)            default 2
    ai        cloud/local AI judgement requests                  default 2
    render    encoding final clips                               default 1 (max 2)

Tasks also name *resources* (``gpu``: local Whisper on the 4 GB card) with
their own limits, so two GPU-heavy tasks never run together.

Everything is stored in SQLite (WAL). A worker *leases* a task for
``lease_seconds`` and renews the lease while it runs. When Studio restarts,
leases held by the previous process are released: the task is queued again
(counted as an attempt, so a task that keeps crashing Studio stops after
``max_attempts``) and any child process it left behind is stopped first, so
the same work never runs twice. Failures retry with backoff up to the task's
``max_failures``. Cancelling a source cancels its queued tasks and stops its
running ones; files a task already produced (final clips) are never touched
here. Heavy stages wait while free disk or memory is below the configured
floor.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import applog

STAGES = ("ingest", "analysis", "ai", "render")
HEAVY = {"analysis", "render"}           # wait for disk/memory headroom
DEFAULTS = {
    "max_sources": 5,
    "max_waiting": 20,
    "pools": {"ingest": 5, "analysis": 2, "ai": 2, "render": 1},
    "resources": {"gpu": 1},
    "lease_seconds": 60,
    "poll_seconds": 1.0,
    "max_attempts": 3,
    "retry_backoff_seconds": 30,
    "min_free_disk_gb": 5.0,
    "min_free_memory_mb": 1500,
}
LIMITS = {"max_sources": (1, 5), "max_waiting": (0, 200), "ingest": (1, 5),
          "analysis": (1, 4), "ai": (1, 4), "render": (1, 2), "gpu": (1, 2),
          "lease_seconds": (10, 3600), "min_free_disk_gb": (0, 500),
          "min_free_memory_mb": (0, 16000)}
PRIORITY = {"live": 0, "vod": 10}
SETTINGS_FILE = "scheduler.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT, state TEXT NOT NULL,
    priority INTEGER NOT NULL, created REAL NOT NULL, started REAL, finished REAL,
    message TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, stage TEXT NOT NULL,
    state TEXT NOT NULL, seq INTEGER NOT NULL, after TEXT, resources TEXT,
    payload TEXT, result TEXT, error TEXT, priority INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
    max_failures INTEGER NOT NULL DEFAULT 1, not_before REAL NOT NULL DEFAULT 0,
    lease_owner TEXT, lease_expires REAL, pid INTEGER,
    created REAL NOT NULL, updated REAL NOT NULL, dedupe TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS tasks_pick ON tasks(state, stage, priority, created);
"""
SOURCE_ACTIVE = {"waiting", "active"}
TASK_OPEN = {"queued", "leased"}


class SchedulerError(ValueError):
    """A request the scheduler refuses (full queue, unknown stage...)."""


class Cancelled(Exception):
    """Raised inside a handler when its source was cancelled."""


def default_db():
    import youtube_auth
    return youtube_auth.config_dir() / "scheduler.db"


def load_settings(path=None):
    import youtube_auth
    path = Path(path) if path else youtube_auth.config_dir() / SETTINGS_FILE
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    return validate_settings(stored if isinstance(stored, dict) else {})


def validate_settings(changes, base=None):
    out = json.loads(json.dumps(base or DEFAULTS))
    for key, value in (changes or {}).items():
        if key in ("pools", "resources") and isinstance(value, dict):
            for name, n in value.items():
                if name not in out[key]:
                    raise SchedulerError(f"Unknown {key[:-1]} '{name}'.")
                low, high = LIMITS.get(name, (1, 8))
                if isinstance(n, bool) or not isinstance(n, int) or not low <= n <= high:
                    raise SchedulerError(f"{name} must be a whole number from {low} to {high}.")
                out[key][name] = n
        elif key in out and key not in ("pools", "resources"):
            low, high = LIMITS.get(key, (0, 10 ** 6))
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not low <= value <= high:
                raise SchedulerError(f"{key} must be from {low} to {high}.")
            out[key] = value
        else:
            raise SchedulerError(f"Unknown scheduler setting '{key}'.")
    return out


def save_settings(changes, path=None):
    import youtube_auth
    path = Path(path) if path else youtube_auth.config_dir() / SETTINGS_FILE
    settings = validate_settings(changes, load_settings(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, indent=1), encoding="utf-8")
    os.replace(temp, path)
    return settings


# ---------------------------------------------------------------- health

def free_disk_gb(path):
    try:
        return shutil.disk_usage(str(path)).free / 1e9
    except OSError:
        return None


def free_memory_mb():
    """Available physical memory (MB), or None when unknown."""
    if sys.platform == "win32":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullAvailPhys / 1e6
        return None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1000
    except OSError:
        pass
    return None


def stop_leftover(pid):
    """Stop a child a previous Studio left running (only Chopify's own workers)."""
    if not pid:
        return False
    try:
        if sys.platform == "win32":
            query = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                capture_output=True, text=True, timeout=20).stdout
            if "chopify" not in query.lower():
                return False
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                           capture_output=True, timeout=20)
            return True
        cmdline = Path(f"/proc/{int(pid)}/cmdline").read_bytes().decode(errors="replace")
        if "chopify" not in cmdline.lower():
            return False
        os.kill(int(pid), 9)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------- engine

class Scheduler:
    def __init__(self, db_path=None, settings=None, handlers=None, disk_path=None,
                 clock=time.time, health=None, leftover=stop_leftover, paused=None,
                 on_finish=None):
        self.path = Path(db_path or default_db())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = settings or load_settings()
        self.handlers = dict(handlers or {})
        self.disk_path = disk_path or self.path.parent
        self.clock = clock
        self.health = health or self._health
        self.leftover = leftover
        self.paused = paused or (lambda stage: None)     # stage -> reason or None
        self.on_finish = on_finish                       # (source_id, state) -> None
        self._finished = []
        self.owner = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.cancel_events = {}            # task_id -> Event
        self.cancel_callbacks = {}         # source_id -> [callable]
        self.threads = []
        self.stopping = threading.Event()
        self.wake = threading.Condition()
        self.paused_reason = None
        with self._db() as db:
            db.executescript(SCHEMA)

    # ------------------------------------------------------------- sql
    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _tx(self, fn):
        with self.lock:
            db = self._db()
            try:
                db.execute("BEGIN IMMEDIATE")
                result = fn(db)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                self._finished.clear()
                raise
            finally:
                db.close()
            finished, self._finished = self._finished, []
        for source_id, state in finished:
            self.cancel_callbacks.pop(source_id, None)
            if self.on_finish:
                try:
                    self.on_finish(source_id, state)
                except Exception as exc:       # noqa: BLE001 - a hook must not break the queue
                    applog.warning("scheduler.finish", f"Finish hook failed: {exc}",
                                   source_id=source_id)
        return result

    # -------------------------------------------------------- sources
    def submit(self, source_id, kind, tasks, label=None, meta=None):
        """Add a source and its task chain. ``tasks``: [{stage, payload, resources,
        max_failures, dedupe}] run in order. Returns the source's info.

        Raises SchedulerError when the source is already active or the waiting
        queue is full.
        """
        if kind not in PRIORITY:
            raise SchedulerError("A source is either live or vod.")
        for task in tasks:
            if task.get("stage") not in STAGES:
                raise SchedulerError(f"Unknown stage {task.get('stage')}.")
        now = self.clock()

        def add(db):
            row = db.execute("SELECT state FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row and row["state"] in SOURCE_ACTIVE:
                raise SchedulerError("This source is already queued or running.")
            active = db.execute("SELECT COUNT(*) FROM sources WHERE state = 'active'").fetchone()[0]
            waiting = db.execute("SELECT COUNT(*) FROM sources WHERE state = 'waiting'").fetchone()[0]
            limit = int(self.settings["max_sources"])
            if active >= limit and waiting >= int(self.settings["max_waiting"]):
                raise SchedulerError(f"All {limit} source slots are busy and the waiting "
                                     f"queue is full ({waiting}). Try again when a source "
                                     "finishes, or cancel one.")
            db.execute("DELETE FROM tasks WHERE source_id = ? AND state NOT IN ('queued','leased')",
                       (source_id,))
            db.execute("INSERT OR REPLACE INTO sources (id, kind, label, state, priority, "
                       "created, started, finished, message, meta) VALUES "
                       "(?, ?, ?, 'waiting', ?, ?, NULL, NULL, NULL, ?)",
                       (source_id, kind, label, PRIORITY[kind], now, json.dumps(meta or {})))
            previous = None
            for seq, task in enumerate(tasks):
                previous = self._insert_task(db, source_id, kind, task, seq, previous, now)
            self._promote(db)
        self._tx(add)
        self._notify()
        return self.source(source_id)

    def add_task(self, source_id, stage, payload=None, resources=(), max_failures=1,
                 dedupe=None, after=None):
        """Add one more task to an active source (live sessions add clips as they go).
        A task with the same ``dedupe`` key is never added twice."""
        if stage not in STAGES:
            raise SchedulerError(f"Unknown stage {stage}.")
        now = self.clock()

        def add(db):
            src = db.execute("SELECT kind, state FROM sources WHERE id = ?",
                             (source_id,)).fetchone()
            if not src or src["state"] not in SOURCE_ACTIVE:
                raise SchedulerError("That source is not active.")
            if dedupe and db.execute("SELECT 1 FROM tasks WHERE dedupe = ?",
                                     (dedupe,)).fetchone():
                return None
            seq = db.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM tasks WHERE source_id = ?",
                             (source_id,)).fetchone()[0]
            return self._insert_task(db, source_id, src["kind"], {
                "stage": stage, "payload": payload, "resources": list(resources),
                "max_failures": max_failures, "dedupe": dedupe, "independent": True},
                seq, after, now)
        task_id = self._tx(add)
        self._notify()
        return task_id

    def _insert_task(self, db, source_id, kind, task, seq, previous, now):
        task_id = uuid.uuid4().hex[:16]
        after = None if task.get("independent") else previous
        db.execute("INSERT INTO tasks (id, source_id, stage, state, seq, after, resources, "
                   "payload, priority, max_failures, created, updated, dedupe) VALUES "
                   "(?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (task_id, source_id, task["stage"], seq, after,
                    json.dumps(list(task.get("resources") or [])),
                    json.dumps(task.get("payload")), PRIORITY[kind],
                    int(task.get("max_failures", 1)), now, now, task.get("dedupe")))
        return task_id

    def _promote(self, db):
        """Fill free source slots from the waiting queue (live first)."""
        limit = int(self.settings["max_sources"])
        active = db.execute("SELECT COUNT(*) FROM sources WHERE state = 'active'").fetchone()[0]
        rows = db.execute("SELECT id FROM sources WHERE state = 'waiting' "
                          "ORDER BY priority, created").fetchall()
        now = self.clock()
        for row in rows:
            if active >= limit:
                break
            db.execute("UPDATE sources SET state = 'active', started = ?, message = NULL "
                       "WHERE id = ?", (now, row["id"]))
            active += 1
        waiting = db.execute("SELECT id FROM sources WHERE state = 'waiting' "
                             "ORDER BY priority, created").fetchall()
        for position, row in enumerate(waiting, 1):
            db.execute("UPDATE sources SET message = ? WHERE id = ?",
                       (f"All {limit} source slots are busy; waiting (position {position}).",
                        row["id"]))

    def _finish_sources(self, db):
        """Close sources whose tasks are all done; free their slots."""
        now = self.clock()
        rows = db.execute("SELECT id, kind, meta FROM sources WHERE state = 'active'").fetchall()
        for row in rows:
            if json.loads(row["meta"] or "{}").get("open"):
                continue                     # a live session stays open until stopped
            open_tasks = db.execute("SELECT COUNT(*) FROM tasks WHERE source_id = ? AND "
                                    "state IN ('queued','leased')", (row["id"],)).fetchone()[0]
            if open_tasks:
                continue
            failed = db.execute("SELECT error FROM tasks WHERE source_id = ? AND "
                                "state = 'failed' ORDER BY updated DESC LIMIT 1",
                                (row["id"],)).fetchone()
            db.execute("UPDATE sources SET state = ?, finished = ?, message = ? WHERE id = ?",
                       ("failed" if failed else "done", now,
                        failed["error"] if failed else None, row["id"]))
            self._finished.append((row["id"], "failed" if failed else "done"))
        self._promote(db)

    def close_source(self, source_id):
        """A live session was stopped: let it finish once its tasks are done."""
        def close(db):
            row = db.execute("SELECT meta FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row:
                meta = json.loads(row["meta"] or "{}")
                meta["open"] = False
                db.execute("UPDATE sources SET meta = ? WHERE id = ?",
                           (json.dumps(meta), source_id))
            self._finish_sources(db)
        self._tx(close)
        self._notify()

    def cancel(self, source_id):
        """Cancel a source: queued tasks never start, running ones are stopped.
        Files already produced are not touched."""
        def cancel(db):
            row = db.execute("SELECT state FROM sources WHERE id = ?", (source_id,)).fetchone()
            if not row or row["state"] not in SOURCE_ACTIVE:
                raise SchedulerError("Nothing is queued or running for this source.")
            running = [r["id"] for r in db.execute(
                "SELECT id FROM tasks WHERE source_id = ? AND state = 'leased'", (source_id,))]
            db.execute("UPDATE tasks SET state = 'cancelled', updated = ? WHERE source_id = ? "
                       "AND state IN ('queued','leased')", (self.clock(), source_id))
            db.execute("UPDATE sources SET state = 'cancelled', finished = ?, message = "
                       "'Cancelled. Clips already made are kept.' WHERE id = ?",
                       (self.clock(), source_id))
            self._finished.append((source_id, "cancelled"))
            self._promote(db)
            return running
        callbacks = list(self.cancel_callbacks.get(source_id, []))
        running = self._tx(cancel)
        for task_id in running:
            event = self.cancel_events.get(task_id)
            if event:
                event.set()
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:     # noqa: BLE001 - one callback must not stop others
                applog.warning("scheduler.cancel", f"Cancel callback failed: {exc}",
                               source_id=source_id)
        self._notify()
        return {"cancelled": source_id, "stopped_tasks": len(running)}

    def on_cancel(self, source_id, callback):
        self.cancel_callbacks.setdefault(source_id, []).append(callback)

    # --------------------------------------------------------- recovery
    def recover(self):
        """At startup: release leases of a previous process; stop its leftovers."""
        now = self.clock()

        def release(db):
            rows = db.execute("SELECT id, pid, attempts FROM tasks WHERE state = 'leased' "
                              "AND (lease_owner IS NULL OR lease_owner != ?)",
                              (self.owner,)).fetchall()
            out = []
            for row in rows:
                out.append((row["id"], row["pid"]))
                if row["attempts"] >= int(self.settings["max_attempts"]):
                    db.execute("UPDATE tasks SET state = 'failed', error = ?, lease_owner = NULL, "
                               "pid = NULL, updated = ? WHERE id = ?",
                               ("Stopped by Studio restarts too many times.", now, row["id"]))
                else:
                    db.execute("UPDATE tasks SET state = 'queued', lease_owner = NULL, "
                               "lease_expires = NULL, pid = NULL, updated = ? WHERE id = ?",
                               (now, row["id"]))
            self._fail_dependents(db)
            self._finish_sources(db)
            return out
        released = self._tx(release)
        for task_id, pid in released:
            if pid and self.leftover(pid):
                applog.warning("scheduler.recover", "Stopped a worker left by the previous "
                               "Studio", task_id=task_id, pid=pid)
        if released:
            applog.info("scheduler.recover", f"{len(released)} interrupted task(s) re-queued")
        return [task_id for task_id, _ in released]

    def _expire_leases(self, db):
        """A lease that ran out (its worker hung or died) is queued again."""
        now = self.clock()
        db.execute("UPDATE tasks SET state = 'queued', lease_owner = NULL, lease_expires = NULL, "
                   "updated = ? WHERE state = 'leased' AND lease_expires < ? AND "
                   "lease_owner != ?", (now, now, self.owner))

    def _fail_dependents(self, db):
        """Tasks after a failed or cancelled task can never run."""
        while True:
            changed = db.execute(
                "UPDATE tasks SET state = 'cancelled', error = 'An earlier step did not finish.', "
                "updated = ? WHERE state = 'queued' AND after IN (SELECT id FROM tasks WHERE "
                "state IN ('failed','cancelled'))", (self.clock(),)).rowcount
            if not changed:
                return

    # ------------------------------------------------------------ lease
    def _usage(self, db):
        stages, resources = {}, {}
        for row in db.execute("SELECT stage, resources FROM tasks WHERE state = 'leased'"):
            stages[row["stage"]] = stages.get(row["stage"], 0) + 1
            for name in json.loads(row["resources"] or "[]"):
                resources[name] = resources.get(name, 0) + 1
        return stages, resources

    def _health(self):
        disk = free_disk_gb(self.disk_path)
        memory = free_memory_mb()
        if disk is not None and disk < float(self.settings["min_free_disk_gb"]):
            return f"Only {disk:.1f} GB free on disk (minimum {self.settings['min_free_disk_gb']} GB)."
        if memory is not None and memory < float(self.settings["min_free_memory_mb"]):
            return f"Only {memory:.0f} MB memory free (minimum {self.settings['min_free_memory_mb']} MB)."
        return None

    def lease(self, stage):
        """Lease the next runnable task for ``stage`` (or None)."""
        low = self.health() if stage in HEAVY else None
        self.paused_reason = low if stage in HEAVY else self.paused_reason
        if low or self.paused(stage):
            return None
        now = self.clock()

        def pick(db):
            self._expire_leases(db)
            stages, resources = self._usage(db)
            if stages.get(stage, 0) >= int(self.settings["pools"][stage]):
                return None
            rows = db.execute(
                "SELECT t.* FROM tasks t JOIN sources s ON s.id = t.source_id "
                "WHERE t.stage = ? AND t.state = 'queued' AND s.state = 'active' "
                "AND t.not_before <= ? AND (t.after IS NULL OR t.after IN "
                "(SELECT id FROM tasks WHERE state = 'done')) "
                "ORDER BY t.priority, s.started, t.seq LIMIT 20", (stage, now)).fetchall()
            for row in rows:
                needs = json.loads(row["resources"] or "[]")
                if any(resources.get(n, 0) >= int(self.settings["resources"].get(n, 1))
                       for n in needs):
                    continue
                db.execute("UPDATE tasks SET state = 'leased', lease_owner = ?, lease_expires = ?, "
                           "attempts = attempts + 1, updated = ? WHERE id = ?",
                           (self.owner, now + float(self.settings["lease_seconds"]), now,
                            row["id"]))
                return {**dict(row), "attempts": row["attempts"] + 1, "state": "leased"}
            return None
        task = self._tx(pick)
        if task:
            task["payload"] = json.loads(task["payload"] or "null")
            task["resources"] = json.loads(task["resources"] or "[]")
        return task

    def renew(self, task_id, pid=None):
        def renew(db):
            db.execute("UPDATE tasks SET lease_expires = ?, pid = COALESCE(?, pid) "
                       "WHERE id = ? AND lease_owner = ?",
                       (self.clock() + float(self.settings["lease_seconds"]), pid, task_id,
                        self.owner))
        self._tx(renew)

    def complete(self, task_id, result=None):
        def done(db):
            row = db.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row or row["state"] != "leased":
                return                       # cancelled meanwhile: keep "cancelled"
            db.execute("UPDATE tasks SET state = 'done', result = ?, lease_owner = NULL, "
                       "pid = NULL, error = NULL, updated = ? WHERE id = ?",
                       (json.dumps(result), self.clock(), task_id))
            self._finish_sources(db)
        self._tx(done)
        self._notify()

    def fail(self, task_id, error, retry=True):
        now = self.clock()

        def failed(db):
            row = db.execute("SELECT state, failures, max_failures, source_id FROM tasks "
                             "WHERE id = ?", (task_id,)).fetchone()
            if not row or row["state"] != "leased":
                return
            failures = row["failures"] + 1
            if retry and failures < row["max_failures"]:
                delay = min(600.0, float(self.settings["retry_backoff_seconds"])
                            * (2 ** (failures - 1)))
                db.execute("UPDATE tasks SET state = 'queued', failures = ?, error = ?, "
                           "not_before = ?, lease_owner = NULL, pid = NULL, updated = ? "
                           "WHERE id = ?", (failures, str(error)[:500], now + delay, now, task_id))
            else:
                db.execute("UPDATE tasks SET state = 'failed', failures = ?, error = ?, "
                           "lease_owner = NULL, pid = NULL, updated = ? WHERE id = ?",
                           (failures, str(error)[:500], now, task_id))
                self._fail_dependents(db)
            self._finish_sources(db)
        self._tx(failed)
        self._notify()

    def release(self, task_id):
        """Give a leased task back unchanged (e.g. Studio is closing)."""
        def give_back(db):
            db.execute("UPDATE tasks SET state = 'queued', lease_owner = NULL, "
                       "attempts = MAX(0, attempts - 1), pid = NULL, updated = ? "
                       "WHERE id = ? AND state = 'leased'", (self.clock(), task_id))
        self._tx(give_back)

    # ------------------------------------------------------------ workers
    def run_one(self, stage):
        """Lease and run one task of ``stage`` in this thread. True if one ran."""
        task = self.lease(stage)
        if not task:
            return False
        handler = self.handlers.get(stage)
        event = threading.Event()
        self.cancel_events[task["id"]] = event
        stop_renew = threading.Event()

        def keep_alive():
            interval = max(1.0, float(self.settings["lease_seconds"]) / 3)
            while not stop_renew.wait(interval):
                try:
                    self.renew(task["id"])
                except sqlite3.Error:
                    pass
        renewer = threading.Thread(target=keep_alive, daemon=True)
        renewer.start()
        try:
            if handler is None:
                raise RuntimeError(f"No handler for {stage}.")
            result = handler(task, event, lambda pid: self.renew(task["id"], pid))
            self.complete(task["id"], result)          # a cancelled task stays cancelled
        except Cancelled:
            pass
        except Exception as exc:                        # noqa: BLE001 - recorded on the task
            if self.stopping.is_set():
                self.release(task["id"])
            else:
                retry = getattr(exc, "retryable", True)
                self.fail(task["id"], exc, retry=retry)
                applog.warning("scheduler.task", f"{stage} task failed: {exc}",
                               task_id=task["id"], source_id=task["source_id"])
        finally:
            stop_renew.set()
            self.cancel_events.pop(task["id"], None)
        return True

    def start(self):
        """Start one worker thread per pool slot."""
        self.recover()
        for stage in STAGES:
            for index in range(int(LIMITS[stage][1])):
                thread = threading.Thread(target=self._worker, args=(stage, index),
                                          name=f"scheduler-{stage}-{index}", daemon=True)
                thread.start()
                self.threads.append(thread)

    def _worker(self, stage, index):
        while not self.stopping.is_set():
            if index >= int(self.settings["pools"][stage]):
                ran = False                 # slot above the configured pool size
            else:
                try:
                    ran = self.run_one(stage)
                except sqlite3.Error as exc:
                    applog.warning("scheduler.db", f"Scheduler database busy: {exc}")
                    ran = False
            if not ran:
                with self.wake:
                    self.wake.wait(float(self.settings["poll_seconds"]))

    def _notify(self):
        with self.wake:
            self.wake.notify_all()

    def stop(self, timeout=5):
        self.stopping.set()
        self._notify()
        for thread in self.threads:
            thread.join(timeout)

    def configure(self, changes, path=None):
        self.settings = save_settings(changes, path)
        self._notify()
        return self.settings

    # ------------------------------------------------------------- views
    def source(self, source_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if not row:
                return None
            tasks = [dict(t) for t in db.execute(
                "SELECT id, stage, state, seq, attempts, failures, error, created, updated "
                "FROM tasks WHERE source_id = ? ORDER BY seq", (source_id,))]
        return {**dict(row), "meta": json.loads(row["meta"] or "{}"), "tasks": tasks}

    def busy(self, source_id):
        with self._db() as db:
            row = db.execute("SELECT state FROM sources WHERE id = ?", (source_id,)).fetchone()
        return bool(row and row["state"] in SOURCE_ACTIVE)

    def status(self):
        with self._db() as db:
            sources = [dict(r) for r in db.execute(
                "SELECT id, kind, label, state, priority, created, started, finished, message "
                "FROM sources WHERE state IN ('active','waiting') OR finished > ? "
                "ORDER BY state = 'waiting', priority, created", (self.clock() - 86400,))]
            counts = {}
            for row in db.execute("SELECT stage, state, COUNT(*) n FROM tasks "
                                  "WHERE state IN ('queued','leased') GROUP BY stage, state"):
                counts.setdefault(row["stage"], {})[row["state"]] = row["n"]
        return {"settings": self.settings, "sources": sources,
                "active": sum(1 for s in sources if s["state"] == "active"),
                "waiting": sum(1 for s in sources if s["state"] == "waiting"),
                "stages": {s: {"running": counts.get(s, {}).get("leased", 0),
                               "queued": counts.get(s, {}).get("queued", 0),
                               "slots": self.settings["pools"][s]} for s in STAGES},
                "paused_reason": self.paused_reason}
