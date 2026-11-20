"""Background job lanes for Studio: recording, processing and uploads.

Each lane has its own worker pool, so a multi-hour live recording never
blocks clip processing, and future uploads (Phase 5) never block either.

Job states:  queued -> running -> succeeded | failed | cancelled

* Cancel a queued job and it never starts. Cancel a running job and its
  cancel callbacks run (Studio uses them to stop child processes or to ask a
  recorder to stop safely); the job ends as ``cancelled``.
* ``max_attempts`` > 1 retries a failing job automatically after
  ``retry_delay`` seconds. ``retry(job_id)`` re-queues a finished job by hand.
* A lane accepts one active job per project, so the same project is never
  processed twice at once.

Pausing a lane (Phase 6) lets its running job finish but starts nothing
new until it is resumed; a queued job reports ``waiting`` = True meanwhile and
can still be cancelled.

Jobs live in memory. Durable progress is in each project's project.json; on
restart Studio marks unfinished work ``interrupted`` and resumes it (Phase 6
automation setting) or the user does.
"""
from concurrent.futures import ThreadPoolExecutor
import itertools
import threading
import time
import traceback

DEFAULT_LANES = {"record": 4, "process": 1, "upload": 1}
ACTIVE = {"queued", "running"}
FINISHED = {"succeeded", "failed", "cancelled"}
MAX_HISTORY = 200


class JobError(ValueError):
    """A job request was rejected (unknown lane, duplicate, closing...)."""


class Job:
    _ids = itertools.count(1)

    def __init__(self, lane, project_id, fn, args, kwargs, kind,
                 max_attempts, retry_delay):
        self.id = f"job-{next(Job._ids)}"
        self.lane = lane
        self.project_id = project_id
        self.kind = kind or lane
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay = float(retry_delay)
        self.state = "queued"
        self.attempts = 0
        self.error = None
        self.result = None
        self.created = time.time()
        self.started = None
        self.finished = None
        self.future = None
        self.waiting = False          # queued behind a paused lane
        self.cancel_event = threading.Event()
        self._cancel_callbacks = []
        self._lock = threading.Lock()

    @property
    def active(self):
        return self.state in ACTIVE

    def on_cancel(self, callback):
        """Run ``callback()`` if this job is cancelled (now, if it already is)."""
        with self._lock:
            if not self.cancel_event.is_set():
                self._cancel_callbacks.append(callback)
                return
        callback()

    def _run_cancel_callbacks(self):
        with self._lock:
            callbacks, self._cancel_callbacks = self._cancel_callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001 - cancelling must never raise
                traceback.print_exc()

    def info(self):
        return {"id": self.id, "lane": self.lane, "project_id": self.project_id,
                "kind": self.kind, "state": self.state,
                "attempts": self.attempts, "max_attempts": self.max_attempts,
                "error": self.error, "created": self.created,
                "started": self.started, "finished": self.finished,
                "waiting": self.waiting}


class JobManager:
    def __init__(self, lanes=None):
        lanes = dict(DEFAULT_LANES if lanes is None else lanes)
        self.executors = {
            lane: ThreadPoolExecutor(max_workers=int(workers),
                                     thread_name_prefix=f"chopify-{lane}")
            for lane, workers in lanes.items()}
        self.lock = threading.RLock()
        self.jobs = {}
        self.closing = False
        # set = running; cleared = paused (queued jobs wait before starting)
        self.gates = {lane: threading.Event() for lane in lanes}
        for gate in self.gates.values():
            gate.set()

    # ---------------------------------------------------------------- pause
    def pause(self, lane):
        if lane not in self.gates:
            raise JobError(f"Unknown job lane: {lane}")
        self.gates[lane].clear()

    def resume(self, lane):
        if lane not in self.gates:
            raise JobError(f"Unknown job lane: {lane}")
        self.gates[lane].set()

    def paused(self, lane):
        return lane in self.gates and not self.gates[lane].is_set()

    # ---------------------------------------------------------------- query
    def active(self, project_id=None, lane=None):
        with self.lock:
            return [job for job in self.jobs.values() if job.active
                    and (project_id is None or job.project_id == project_id)
                    and (lane is None or job.lane == lane)]

    def busy(self, project_id, lane=None):
        return bool(self.active(project_id, lane))

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise JobError("Unknown job.")
        return job

    def list(self, project_id=None):
        with self.lock:
            jobs = [job for job in self.jobs.values()
                    if project_id is None or job.project_id == project_id]
        return [job.info() for job in sorted(jobs, key=lambda j: j.created)]

    # --------------------------------------------------------------- submit
    def submit(self, lane, project_id, fn, *args, kind=None, max_attempts=1,
               retry_delay=5.0, **kwargs):
        """Queue ``fn(*args, **kwargs)`` on ``lane``. Returns the Job."""
        with self.lock:
            if self.closing:
                raise JobError("Studio is shutting down.")
            if lane not in self.executors:
                raise JobError(f"Unknown job lane: {lane}")
            if self.busy(project_id, lane):
                raise JobError("This project is already queued or running.")
            job = Job(lane, project_id, fn, args, kwargs, kind,
                      max_attempts, retry_delay)
            self.jobs[job.id] = job
            self._trim()
            job.future = self.executors[lane].submit(self._execute, job)
            return job

    def retry(self, job_id):
        """Queue a finished job again with the same work."""
        job = self.get(job_id)
        if job.active:
            raise JobError("This job is still queued or running.")
        return self.submit(job.lane, job.project_id, job.fn, *job.args,
                           kind=job.kind, max_attempts=job.max_attempts,
                           retry_delay=job.retry_delay, **job.kwargs)

    def _execute(self, job):
        gate = self.gates.get(job.lane)
        while gate is not None and not gate.is_set() and not job.cancel_event.is_set():
            job.waiting = True
            gate.wait(0.2)
        job.waiting = False
        with job._lock:
            if job.cancel_event.is_set():
                job.state, job.finished = "cancelled", time.time()
                return None
            job.state, job.started = "running", time.time()
        while True:
            job.attempts += 1
            try:
                job.result = job.fn(*job.args, **job.kwargs)
            except Exception as exc:  # noqa: BLE001 - recorded on the job
                job.error = str(exc) or exc.__class__.__name__
                if (job.attempts < job.max_attempts
                        and not job.cancel_event.wait(job.retry_delay)):
                    continue
                job.state = "cancelled" if job.cancel_event.is_set() else "failed"
            else:
                job.error = None
                job.state = "cancelled" if job.cancel_event.is_set() else "succeeded"
            job.finished = time.time()
            return job.result

    # --------------------------------------------------------------- cancel
    def cancel(self, job_id):
        """Cancel one job. Returns True if it was still active."""
        job = self.get(job_id)
        with job._lock:
            if not job.active:
                return False
            job.cancel_event.set()
            queued = job.state == "queued"
        if queued and job.future is not None and job.future.cancel():
            job.state, job.finished = "cancelled", time.time()
            return True
        job._run_cancel_callbacks()
        return True

    def cancel_project(self, project_id, lane=None):
        """Cancel every active job of a project (optionally one lane)."""
        return [job.id for job in self.active(project_id, lane)
                if self.cancel(job.id)]

    def shutdown(self):
        """Cancel everything and stop accepting work (Studio is closing)."""
        with self.lock:
            self.closing = True
            jobs = [job for job in self.jobs.values() if job.active]
        for gate in self.gates.values():
            gate.set()
        for job in jobs:
            self.cancel(job.id)
        for executor in self.executors.values():
            executor.shutdown(wait=False, cancel_futures=True)

    def _trim(self):
        finished = [job for job in self.jobs.values() if not job.active]
        if len(self.jobs) <= MAX_HISTORY:
            return
        for job in sorted(finished, key=lambda j: j.created)[
                :len(self.jobs) - MAX_HISTORY]:
            self.jobs.pop(job.id, None)
