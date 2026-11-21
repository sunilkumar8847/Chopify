"""Structured logging, request ids and readable failures."""
import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

import applog
import automation
import dashboard
import ingest
import jobs
import projects


# ------------------------------------------------------------- applog

def test_lines_are_structured_with_ids(tmp_path):
    with applog.bound(project_id="p1", request_id="r1"):
        applog.info("project.create", "made it", job_id="j1", stage="source")
    entry = applog.read(1)[0]
    for key in ("time", "level", "process", "operation", "message",
                "project_id", "job_id", "request_id", "stage"):
        assert key in entry, key
    assert (entry["project_id"], entry["job_id"], entry["request_id"]) == ("p1", "j1", "r1")


@pytest.mark.parametrize("secret", [
    '{"access_token": "ya29.a0AfH6SMBxxxxxxxxxxxxxxxx", "x": 1}',
    "refresh_token=1//0gAbCdEfGhIjKlMnOpQrStUv",
    "client_secret: GOCSPX-abcdefghijklmnop",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123",
    "callback ?state=abc&code=4/0AeanS0abcdefghijklmnop",
    '"X-Chopify-Token": "SuperSecretStudioToken123"',
])
def test_secrets_never_reach_the_log(secret):
    applog.info("oauth", f"debug {secret}", detail=secret)
    raw = "".join(p.read_text(encoding="utf-8") for p in applog.log_dir().glob("*.jsonl"))
    for fragment in ("ya29.a0AfH6", "1//0gAbCd", "GOCSPX-abc", "abcdefghijklmnopqrstuvwxyz0123",
                     "4/0AeanS0abc", "SuperSecretStudioToken123"):
        assert fragment not in raw
    assert "[redacted" in raw


def test_step_logs_start_completion_and_failures_with_traceback():
    with applog.step("transcribe", "Transcribe"):
        pass
    with pytest.raises(ValueError):
        with applog.step("render", "Render"):
            raise ValueError("ffmpeg exploded")
    events = [(e["operation"], e.get("event")) for e in reversed(applog.read(10))]
    assert events == [("transcribe", "started"), ("transcribe", "completed"),
                      ("render", "started"), ("render", "failed")]
    failed = applog.read(1)[0]
    assert failed["level"] == "ERROR" and "ffmpeg exploded" in failed["traceback"]


def test_files_rotate_by_size_and_old_files_are_removed(monkeypatch):
    import os
    import time
    monkeypatch.setattr(applog, "MAX_BYTES", 400)
    for i in range(30):
        applog.info("x", f"line {i} " + "y" * 40)
    files = sorted(p.name for p in applog.log_dir().glob("chopify-*.jsonl"))
    assert len(files) > 2 and any(".1.jsonl" in f for f in files)
    assert applog.read(1)[0]["message"].startswith("line 29")
    old = applog.log_dir() / "chopify-2000-01-01.jsonl"
    old.write_text("{}\n")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    applog._cleaned["done"] = False
    applog.info("x", "trigger cleanup")
    assert not old.exists()


def test_child_processes_inherit_ids_from_the_environment(monkeypatch):
    monkeypatch.setenv("CHOPIFY_PROJECT_ID", "abc123abc123")
    monkeypatch.setenv("CHOPIFY_JOB_ID", "job-7")
    monkeypatch.setenv("CHOPIFY_REQUEST_ID", "req9")
    applog.info("worker", "hello")
    entry = applog.read(1)[0]
    assert (entry["project_id"], entry["job_id"], entry["request_id"]) == \
        ("abc123abc123", "job-7", "req9")


def test_job_exceptions_are_logged_not_swallowed():
    manager = jobs.JobManager({"process": 1})
    try:
        def boom():
            raise RuntimeError("worker crashed")
        job = manager.submit("process", "abcdefabcdef", boom)
        job.future.result(5)
        assert job.state == "failed" and job.error == "worker crashed"
        entry = next(e for e in applog.read(10) if e["operation"] == "job.exception")
        assert entry["job_id"] == job.id and "worker crashed" in entry["traceback"]
    finally:
        manager.shutdown()


# ------------------------------------------------- Studio request trail

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard.Studio, "_run", lambda *a: 0)
    httpd = dashboard.make_server(tmp_path / "projects", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()
    httpd.app.close()
    thread.join()


def post(base, path, data, token):
    request = urllib.request.Request(base + path, data=json.dumps(data).encode(), headers={
        "Content-Type": "application/json", "X-Chopify-Token": token})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, dict(response.headers), json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), json.load(exc)


def test_find_clips_leaves_a_complete_log_trail(server):
    httpd, base = server
    status, headers, body = post(base, "/api/batch", {"sources": ["https://youtu.be/CukYTtF72KY"]},
                                 httpd.app.token)
    assert status == 201 and headers.get("X-Request-ID")
    request_id = headers["X-Request-ID"]
    trail = [(e["operation"], e.get("event")) for e in
             reversed(applog.read(50, request_id=request_id))]
    expected = [("http.request", None), ("findclips.request", None),
                ("source.validate", "started"), ("source.validate", "completed"),
                ("project.create", "started"), ("project.create", "completed"),
                ("job.submit", None), ("http.response", None)]
    assert [t for t in trail if t in expected] == expected, trail
    submit = next(e for e in applog.read(50, request_id=request_id) if e["operation"] == "job.submit")
    assert submit["project_id"] == body["projects"][0]["id"] and submit["job_id"].startswith("job-")


def test_invalid_sources_are_rejected_before_creating_anything(server):
    httpd, base = server
    status, _, body = post(base, "/api/batch", {"sources": [r"D:\nope\missing.mp4", "ftp://x/y"]},
                           httpd.app.token)
    assert status == 400 and "does not exist" in body["error"] and body["request_id"]
    assert [e["source"] for e in body["errors"]] == [r"D:\nope\missing.mp4", "ftp://x/y"]
    assert httpd.app.list_projects() == []
    mixed = post(base, "/api/batch", {"sources": ["https://youtu.be/aaaaaaaaaaa", "nope.mp4"]},
                 httpd.app.token)
    assert mixed[0] == 201 and len(mixed[2]["projects"]) == 1 and len(mixed[2]["errors"]) == 1


def test_unexpected_errors_answer_500_with_a_request_id(server, monkeypatch):
    httpd, base = server
    monkeypatch.setattr(httpd.app, "overview", lambda: 1 / 0)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/api/overview")
    assert exc.value.code == 500
    body = json.load(exc.value)
    assert "ZeroDivisionError" in body["error"] and body["request_id"] in body["error"]
    crash = next(e for e in applog.read(10) if e["operation"] == "http.unexpected")
    assert crash["request_id"] == body["request_id"] and "ZeroDivisionError" in crash["traceback"]


def test_logs_endpoint_filters_by_project(server):
    httpd, base = server
    _, _, body = post(base, "/api/batch", {"sources": ["https://youtu.be/bbbbbbbbbbb"]},
                      httpd.app.token)
    project_id = body["projects"][0]["id"]
    with urllib.request.urlopen(f"{base}/api/logs?project={project_id}") as response:
        entries = json.load(response)["entries"]
    assert entries and {e["project_id"] for e in entries} == {project_id}
    assert all("traceback" not in e for e in entries)


def test_browser_disconnect_is_logged_without_a_traceback():
    handler = dashboard.Handler.__new__(dashboard.Handler)

    class Gone:
        def write(self, data):
            raise ConnectionAbortedError(10053, "aborted")

        def flush(self):
            pass
    handler.wfile, handler.request_version = Gone(), "HTTP/1.1"
    handler.requestline, handler.command, handler.close_connection = "GET / HTTP/1.1", "GET", False
    handler.headers, handler.path = {}, "/media/x"
    handler.send_json({"ok": True})
    entry = applog.read(1)[0]
    assert entry["operation"] == "http.client_gone" and "traceback" not in entry


# --------------------------------------------------- readable failures

def test_worker_errors_are_taken_from_its_output(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("===== old run =====\nERROR: old\n===== 2026 prepare =====\n"
                   "PREFLIGHT OK (ffmpeg + libx264/aac)\n"
                   "PREFLIGHT FAILED - fix these and re-run: 'ffmpeg' not found on PATH\n",
                   encoding="utf-8")
    message = dashboard.error_from_log(log, 2)
    assert "'ffmpeg' not found" in message and "exit code 2" in message and "old" not in message
    assert "before writing any output" in dashboard.error_from_log(tmp_path / "none.log", 1)


def test_ytdlp_reasons_reach_the_user(tmp_path):
    exc = subprocess.CalledProcessError(1, ["yt-dlp"], stderr=(
        "WARNING: something\nERROR: [youtube] CukYTtF72KY: This video is unavailable\n"))
    assert ingest.ytdlp_reason(exc) == "This video is unavailable"

    def fake_run(cmd, **kwargs):
        raise exc
    meta = ingest.probe_source("https://youtu.be/CukYTtF72KY", runner=fake_run)
    described = ingest.describe_source(meta)
    assert described["label"] == "Cannot read this video"
    assert "This video is unavailable" in described["message"]
    with pytest.raises(RuntimeError) as raised:
        ingest.download_visual_discovery("https://youtu.be/CukYTtF72KY", tmp_path,
                                         runner=fake_run)
    assert str(raised.value) == "Download failed: This video is unavailable"
    diagnosis = automation.diagnose({"status": "failed", "stage": "source",
                                     "error": str(raised.value)})
    assert diagnosis["problem"] == "The video is unavailable." and "Check the URL" in diagnosis["advice"]


def test_pipeline_stage_failures_are_logged_with_traceback(tmp_path):
    state = projects.create_project(tmp_path, str(tmp_path / "gone.mp4"), {})
    path = projects.project_path(tmp_path, state["id"])
    with pytest.raises(ValueError):
        projects.run_project(path, action="prepare")
    entries = applog.read(20, project_id=state["id"])
    started = [e for e in entries if e.get("event") == "started"]
    failed = [e for e in entries if e.get("event") == "failed"]
    assert started and started[-1]["operation"] == "source.download"
    assert failed and failed[0]["stage"] == "source" and "does not exist" in failed[0]["traceback"]
