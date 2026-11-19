"""Job lanes, cancellation and retries, plus Studio recording integration."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import jobs


@pytest.fixture
def manager():
    m = jobs.JobManager()
    yield m
    m.shutdown()


def wait_for(predicate, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_long_recording_does_not_block_processing(manager):
    release = threading.Event()
    recording = manager.submit("record", "aaaaaaaaaaaa", release.wait, 30)
    processed = manager.submit("process", "bbbbbbbbbbbb", lambda: "clips")
    assert processed.future.result(timeout=10) == "clips"
    assert processed.state == "succeeded"
    assert recording.state == "running", "recording keeps running alongside"
    release.set()
    recording.future.result(timeout=10)


def test_processing_lane_runs_one_job_at_a_time(manager):
    order, gate = [], threading.Event()

    def work(name):
        order.append(f"start {name}")
        gate.wait(5)
        order.append(f"end {name}")

    first = manager.submit("process", "aaaaaaaaaaaa", work, "a")
    second = manager.submit("process", "bbbbbbbbbbbb", work, "b")
    assert wait_for(lambda: first.state == "running")
    assert second.state == "queued"
    gate.set()
    second.future.result(timeout=10)
    assert order == ["start a", "end a", "start b", "end b"]


def test_one_active_job_per_project_per_lane(manager):
    gate = threading.Event()
    manager.submit("process", "aaaaaaaaaaaa", gate.wait, 5)
    with pytest.raises(jobs.JobError, match="already queued"):
        manager.submit("process", "aaaaaaaaaaaa", lambda: None)
    other_lane = manager.submit("record", "aaaaaaaaaaaa", lambda: "ok")
    assert other_lane.future.result(timeout=5) == "ok"
    gate.set()


def test_cancel_queued_job_never_runs(manager):
    gate, ran = threading.Event(), []
    manager.submit("process", "aaaaaaaaaaaa", gate.wait, 5)
    queued = manager.submit("process", "bbbbbbbbbbbb", ran.append, "x")
    assert manager.cancel(queued.id)
    gate.set()
    assert wait_for(lambda: not manager.active())
    assert queued.state == "cancelled" and ran == []


def test_cancel_running_job_runs_callbacks(manager):
    stop = threading.Event()
    job = manager.submit("record", "aaaaaaaaaaaa", stop.wait, 30)
    job.on_cancel(stop.set)
    assert wait_for(lambda: job.state == "running")
    assert manager.cancel(job.id)
    job.future.result(timeout=5)
    assert job.state == "cancelled"


def test_automatic_retries_then_success(manager):
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("network blip")
        return "done"

    job = manager.submit("upload", "aaaaaaaaaaaa", flaky, max_attempts=3,
                         retry_delay=0)
    assert job.future.result(timeout=5) == "done"
    assert job.state == "succeeded" and job.attempts == 3


def test_failed_job_can_be_retried_by_hand(manager):
    calls = []

    def once_bad():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return "fixed"

    job = manager.submit("process", "aaaaaaaaaaaa", once_bad)
    job.future.result(timeout=5)
    assert job.state == "failed" and job.error == "boom"
    again = manager.retry(job.id)
    assert again.future.result(timeout=5) == "fixed"


def test_shutdown_rejects_new_work(manager):
    manager.shutdown()
    with pytest.raises(jobs.JobError):
        manager.submit("process", "aaaaaaaaaaaa", lambda: None)


# ------------------------------------------------ Studio integration (HTTP)

FAKE_CHOPIFY = r'''
import json, sys
from pathlib import Path
project = Path(sys.argv[sys.argv.index("--project") + 1])
state = json.loads((project / "project.json").read_text(encoding="utf-8"))
(project / "launch.json").write_text(json.dumps(
    {"argv": sys.argv[1:], "source": state["source"]}), encoding="utf-8")
'''


class FakeRecorder:
    """Stands in for live.Recorder: records until stopped, then hands off."""
    started = []

    def __init__(self, path, stop, abort):
        self.path, self.stop, self.abort = path, stop, abort

    def run(self):
        import projects
        FakeRecorder.started.append(self.path.name)
        projects.update_project(self.path, lambda s: (
            s.update(status="recording"),
            s["recording"].update(state="recording")))
        while not (self.stop.is_set() or self.abort.is_set()):
            time.sleep(0.02)
        if self.abort.is_set():
            projects.update_project(self.path, lambda s: (
                s.update(status="interrupted"),
                s["recording"].update(state="interrupted")))
            return "interrupted"
        out = self.path / "recording.mp4"
        out.write_bytes(b"video")
        projects.update_project(self.path, lambda s: (
            s.update(source=str(out), status="new"),
            s["recording"].update(state="recorded", output=str(out))))
        return "recorded"


@pytest.fixture
def studio(tmp_path, monkeypatch):
    import dashboard
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "chopify.py").write_text(FAKE_CHOPIFY, encoding="utf-8")
    monkeypatch.setattr(dashboard, "HERE", bin_dir)
    monkeypatch.setattr(dashboard.live, "Recorder", FakeRecorder)
    httpd = dashboard.make_server(tmp_path / "projects", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()
    httpd.app.close()
    thread.join()


def call(base, path, data=None, token=None):
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["X-Chopify-Token"] = token
    req = urllib.request.Request(
        base + path, data=json.dumps(data).encode() if data is not None else None,
        headers=headers)
    with urllib.request.urlopen(req) as response:
        return json.load(response)


def test_detect_endpoint_reports_live_streams(studio, monkeypatch):
    httpd, base = studio
    import dashboard
    monkeypatch.setattr(dashboard.ingest, "probe_source", lambda source: {
        "url": True, "live_status": "is_live", "title": "Now live",
        "duration": None})
    result = call(base, "/api/detect", {"source": "https://youtu.be/x"},
                  httpd.app.token)
    assert result["kind"] == "live" and result["can_record"]
    assert result["title"] == "Now live"


def test_detect_requires_the_studio_token(studio):
    _, base = studio
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(base, "/api/detect", {"source": "https://youtu.be/x"})
    assert exc.value.code == 403


def test_recording_runs_beside_processing_then_hands_off(studio):
    httpd, base = studio
    app, token = httpd.app, httpd.app.token
    import projects
    recording = call(base, "/api/record", {"source": "https://youtu.be/live",
                                           "settings": {"after": "prepare"}}, token)
    rid = recording["id"]
    assert wait_for(lambda: rid in FakeRecorder.started)
    detail = call(base, f"/api/projects/{rid}")
    assert detail["busy"] and detail["recording_active"]
    # A different project is processed while the recording is still running.
    other = projects.create_project(app.root, "C:/videos/other.mp4", {})
    app.enqueue(other["id"], "prepare")
    app.futures[other["id"]].result(timeout=60)
    assert app.jobs.busy(rid, "record"), "recording still running"
    # Processing the recording itself is refused until it stops.
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(base, f"/api/projects/{rid}/run", {"action": "prepare"}, token)
    assert exc.value.code == 400
    call(base, f"/api/projects/{rid}/record-stop", {}, token)
    # The finished recording is queued for processing automatically.
    assert wait_for(lambda: rid in app.futures)
    app.futures[rid].result(timeout=60)
    path = projects.project_path(app.root, rid)
    launch = json.loads((path / "launch.json").read_text(encoding="utf-8"))
    assert launch["argv"][launch["argv"].index("--action") + 1] == "prepare"
    assert launch["source"].endswith("recording.mp4")


def test_stop_without_recording_is_rejected(studio):
    httpd, base = studio
    import projects
    state = projects.create_project(httpd.app.root, "https://youtu.be/x", {})
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(base, f"/api/projects/{state['id']}/record-stop", {}, httpd.app.token)
    assert exc.value.code == 400


def test_cancel_endpoint_cancels_a_queued_processing_job(studio):
    httpd, base = studio
    app = httpd.app
    import projects
    gate = threading.Event()
    first = projects.create_project(app.root, "a.mp4", {})
    second = projects.create_project(app.root, "b.mp4", {})
    original = app._run
    app._run = lambda *args: gate.wait(10)
    try:
        app.enqueue(first["id"], "prepare")
        app.enqueue(second["id"], "prepare")
        call(base, f"/api/projects/{second['id']}/cancel", {}, app.token)
        assert projects.read_project(
            projects.project_path(app.root, second["id"]))["status"] == "cancelled"
    finally:
        gate.set()
        app._run = original
    assert wait_for(lambda: not app.jobs.active())


def test_closing_studio_interrupts_recordings_without_losing_them(tmp_path, monkeypatch):
    import dashboard
    import live
    import projects
    monkeypatch.setattr(dashboard.live, "Recorder", FakeRecorder)
    app = dashboard.Studio(tmp_path / "projects")
    state = live.start_recording_project(app.root, "https://youtu.be/live")
    job = app.start_recording(state["id"])
    assert wait_for(lambda: state["id"] in FakeRecorder.started)
    app.close()
    job.future.result(timeout=10)
    rec = projects.read_project(projects.project_path(app.root, state["id"]))["recording"]
    assert rec["state"] == "interrupted"
