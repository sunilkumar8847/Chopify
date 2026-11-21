"""Phase 6: automation dashboard, queue control, monitoring and recovery."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import automation
import dashboard
import projects
from test_youtube import (FakeGoogle, Clock, account, clock, connected, google,  # noqa: F401
                          make_clip, queue_for)


# --------------------------------------------------------------- helpers

@pytest.fixture
def studio(tmp_path, monkeypatch):
    """A Studio whose processing jobs run a fast fake instead of chopify.py."""
    runs = []
    gate = {"block": None}

    def fake_run(self, path, action, preview, pick):
        runs.append((path.name, action))
        if gate["block"] is not None:
            gate["block"].wait(10)
        state = projects.read_project(path)
        state.update(status="review" if action == "prepare" else "complete",
                     stage="ready for review" if action == "prepare" else "complete")
        projects.save_project(path, state)
        return 0
    monkeypatch.setattr(dashboard.Studio, "_run", fake_run)
    httpd = dashboard.make_server(tmp_path / "projects", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd.runs, httpd.gate = runs, gate
    yield httpd
    if gate["block"] is not None:
        gate["block"].set()
    httpd.shutdown()
    httpd.server_close()
    httpd.app.close()
    thread.join()


def call(httpd, path, data=None, expect=200):
    base = f"http://127.0.0.1:{httpd.server_port}"
    headers = {"Content-Type": "application/json", "X-Chopify-Token": httpd.app.token}
    request = urllib.request.Request(base + path, headers=headers,
                                     data=json.dumps(data).encode() if data is not None else None)
    try:
        with urllib.request.urlopen(request) as response:
            assert response.status == expect
            return json.load(response)
    except urllib.error.HTTPError as exc:
        assert exc.code == expect, exc.read()
        return json.load(exc)


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------- source keys

@pytest.mark.parametrize("url", [
    "https://youtu.be/169ZzvPxvvM", "https://youtu.be/169ZzvPxvvM?si=abc",
    "https://www.youtube.com/watch?v=169ZzvPxvvM&t=42s",
    "https://m.youtube.com/watch?v=169ZzvPxvvM", "https://www.youtube.com/live/169ZzvPxvvM",
    "https://youtube.com/shorts/169ZzvPxvvM"])
def test_every_youtube_url_form_is_the_same_source(url):
    assert automation.source_key(url) == "youtube:169ZzvPxvvM"


def test_local_paths_are_normalized(tmp_path):
    video = tmp_path / "Clip.mp4"
    video.write_bytes(b"x")
    assert automation.source_key(str(video)) == automation.source_key(
        str(tmp_path / "." / "Clip.mp4"))
    assert automation.source_key("https://example.com/a.mp4") == "url:https://example.com/a.mp4"


# ------------------------------------------------- batch + duplicates

def test_batch_skips_sources_that_already_have_a_project(studio):
    first = call(studio, "/api/batch", {"sources": [
        "https://youtu.be/169ZzvPxvvM", "https://www.youtube.com/watch?v=169ZzvPxvvM",
        "https://youtu.be/aaaaaaaaaaa"]}, expect=201)
    assert len(first["projects"]) == 2 and len(first["duplicates"]) == 1
    again = call(studio, "/api/batch", {"sources": ["https://youtu.be/169ZzvPxvvM?si=x"]},
                 expect=201)
    assert again["projects"] == []
    assert again["duplicates"][0]["project_id"] == first["projects"][0]["id"]
    forced = call(studio, "/api/batch", {"sources": ["https://youtu.be/169ZzvPxvvM"],
                                         "allow_duplicates": True}, expect=201)
    assert len(forced["projects"]) == 1
    assert wait_for(lambda: len(studio.runs) == 3)


def test_batch_processes_every_source_one_at_a_time(studio):
    gate = threading.Event()
    studio.gate["block"] = gate
    made = call(studio, "/api/batch", {"sources": [f"https://youtu.be/{c * 11}" for c in "abc"]},
                expect=201)["projects"]
    assert wait_for(lambda: len(studio.runs) == 1)
    overview = call(studio, "/api/overview")
    assert len(overview["jobs"]["active"]) == 3
    assert sum(j["state"] == "running" for j in overview["jobs"]["active"]) == 1
    gate.set()
    assert wait_for(lambda: len(studio.runs) == 3)
    assert {r[0] for r in studio.runs} == {p["id"] for p in made}


# ----------------------------------------------- pause, resume, cancel

def test_pausing_processing_holds_queued_jobs_until_resumed(studio):
    call(studio, "/api/automation/pause", {"target": "processing", "paused": True})
    made = call(studio, "/api/batch", {"sources": ["https://youtu.be/bbbbbbbbbbb"]},
                expect=201)["projects"][0]
    time.sleep(0.4)
    assert studio.runs == []
    job = call(studio, "/api/overview")["jobs"]["active"][0]
    assert job["state"] == "queued" and job["waiting"] is True
    assert call(studio, "/api/overview")["jobs"]["paused"]["process"] is True
    call(studio, "/api/automation/pause", {"target": "processing", "paused": False})
    assert wait_for(lambda: studio.runs == [(made["id"], "prepare")])


def test_pause_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard.Studio, "_run", lambda *a: 0)
    first = dashboard.make_server(tmp_path / "p", port=0)
    first.app.set_paused("processing", True)
    first.app.close()
    first.server_close()
    second = dashboard.make_server(tmp_path / "p", port=0)
    try:
        assert second.app.jobs.paused("process")
    finally:
        second.app.close()
        second.server_close()


def test_a_waiting_job_can_be_cancelled(studio):
    call(studio, "/api/automation/pause", {"target": "processing", "paused": True})
    made = call(studio, "/api/batch", {"sources": ["https://youtu.be/ccccccccccc"]},
                expect=201)["projects"][0]
    call(studio, f"/api/projects/{made['id']}/cancel", {})
    call(studio, "/api/automation/pause", {"target": "processing", "paused": False})
    time.sleep(0.4)
    assert studio.runs == []


def test_paused_uploads_do_not_start(studio):
    calls = []
    studio.app.jobs.submit = lambda *a, **k: calls.append(a)
    studio.app.uploads.due = lambda: True
    call(studio, "/api/automation/pause", {"target": "uploads", "paused": True})
    studio.app.kick_uploads()
    assert calls == []


# ---------------------------------------------------------- retry + log

def test_failed_project_retries_its_last_action(studio):
    state = projects.create_project(studio.app.root, "https://youtu.be/ddddddddddd", {})
    path = projects.project_path(studio.app.root, state["id"])
    state.update(status="failed", stage="render 1/2", error="ffmpeg exited 1",
                 last_action="all", last_preview=False)
    projects.save_project(path, state)
    detail = call(studio, f"/api/projects/{state['id']}")
    assert detail["diagnosis"]["stage"] == "render"
    assert "encoding" in detail["diagnosis"]["problem"].lower()
    call(studio, f"/api/projects/{state['id']}/retry", {})
    assert wait_for(lambda: studio.runs == [(state["id"], "all")])
    error = call(studio, f"/api/projects/{state['id']}/retry", {}, expect=400)
    assert "Only failed" in error["error"]


class FakeChild:
    """Stands in for the chopify.py child process."""
    code = 0

    def __init__(self, cmd, cwd=None, stdin=None, stdout=None, stderr=None, **kwargs):
        stdout.write("child output\n")
        stdout.flush()

    def wait(self):
        return FakeChild.code

    def poll(self):
        return FakeChild.code


def test_events_record_the_job_history(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", FakeChild)
    app = dashboard.Studio(tmp_path / "p")
    try:
        created, _ = app.create_sources(["https://youtu.be/eeeeeeeeeee"], {})
        project_id = created[0]["id"]
        assert wait_for(lambda: any(e["kind"] == "finished" for e in app.events.tail(20)))
        kinds = [e["kind"] for e in app.events.tail(20, project_id)]
        assert {"created", "queued", "started", "finished"} <= set(kinds)
        FakeChild.code = 1
        app.enqueue(project_id, "render")
        assert wait_for(lambda: any(e["kind"] == "failed" for e in app.events.tail(20)))
        failure = next(e for e in app.events.tail(20) if e["kind"] == "failed")
        assert failure["level"] == "error" and failure["advice"]
    finally:
        FakeChild.code = 0
        app.close()


def test_event_log_rotates(tmp_path, monkeypatch):
    monkeypatch.setattr(automation, "EVENTS_MAX_BYTES", 300)
    log = automation.EventLog(tmp_path)
    for i in range(40):
        log.add("x", f"event {i} " + "y" * 40)
    folder = tmp_path / automation.STATE_DIR
    assert (folder / "events.jsonl.1").exists() and (folder / "events.jsonl.2").exists()
    assert not (folder / "events.jsonl.3").exists()
    assert log.tail(1)[0]["message"].startswith("event 39")


def test_job_log_is_appended_per_run(tmp_path, monkeypatch):
    import subprocess

    class Child:
        def __init__(self, cmd, cwd=None, stdin=None, stdout=None, stderr=None, **kwargs):
            stdout.write("child output\n")
            stdout.flush()

        def wait(self):
            return 0

        def poll(self):
            return 0
    monkeypatch.setattr(subprocess, "Popen", Child)
    app = dashboard.Studio(tmp_path / "p")
    try:
        state = projects.create_project(app.root, "x.mp4", {})
        path = projects.project_path(app.root, state["id"])
        app._run(path, "prepare", False, None)
        app._run(path, "render", False, None)
        log = (path / "job.log").read_text(encoding="utf-8")
        assert log.count("child output") == 2 and "===== " in log and " render =====" in log
    finally:
        app.close()


# ----------------------------------------------------- restart recovery

def interrupted_project(root, status="running", action="all"):
    state = projects.create_project(root, "https://youtu.be/fffffffffff", {})
    path = projects.project_path(root, state["id"])
    state.update(status=status, stage="transcribe", last_action=action)
    projects.save_project(path, state)
    return state["id"]


def test_interrupted_processing_resumes_after_restart(tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(dashboard.Studio, "_run",
                        lambda self, path, action, preview, pick: runs.append((path.name, action)) or 0)
    project_id = interrupted_project(tmp_path / "p")
    httpd = dashboard.make_server(tmp_path / "p", port=0)
    try:
        assert wait_for(lambda: runs == [(project_id, "all")])
        events = httpd.app.events.tail(10, project_id)
        assert any("resumed after restart" in e["message"] for e in events)
    finally:
        httpd.app.close()
        httpd.server_close()


def test_recovery_can_be_switched_off(tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(dashboard.Studio, "_run", lambda *a: runs.append(a) or 0)
    project_id = interrupted_project(tmp_path / "p")
    automation.Settings(tmp_path / "p").update({"resume_after_restart": False})
    httpd = dashboard.make_server(tmp_path / "p", port=0)
    try:
        time.sleep(0.3)
        assert runs == []
        state = projects.read_project(projects.project_path(httpd.app.root, project_id))
        assert state["status"] == "interrupted"
        assert automation.diagnose(state)["advice"].startswith("Press Retry")
    finally:
        httpd.app.close()
        httpd.server_close()


# ------------------------------------------------------------ settings

def test_settings_are_validated_and_saved(tmp_path):
    settings = automation.Settings(tmp_path)
    saved = settings.update({"max_new_sources_per_day": 2, "monitor_interval_minutes": 15})
    assert saved["max_new_sources_per_day"] == 2 and saved["uploads_paused"] is False
    for bad in ({"max_new_sources_per_day": -1}, {"monitor_interval_minutes": 1},
                {"new_source_action": "publish"}, {"monitor_enabled": "yes"},
                {"upload_everything": True}):
        with pytest.raises(ValueError):
            settings.update(bad)
    assert settings.take_source_slot() and settings.take_source_slot()
    assert not settings.take_source_slot(), "daily limit"
    assert settings.usage()["left"] == 0


def test_there_is_no_setting_that_publishes_automatically():
    assert not any("publish" in key or "upload" in key and "paused" not in key
                   for key in automation.DEFAULTS)


# ----------------------------------------------------------- monitoring

class FakeChannel:
    def __init__(self):
        self.tabs = {"videos": [], "streams": []}
        self.fail = None
        self.calls = 0

    def __call__(self, url, tab):
        self.calls += 1
        if self.fail:
            raise RuntimeError(self.fail)
        return list(self.tabs[tab])


def video(video_id, status=None, title="t"):
    return {"id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": title, "live_status": status}


def test_channel_addresses_are_validated():
    assert automation.normalize_channel("@someone") == "https://www.youtube.com/@someone"
    assert automation.normalize_channel(
        "https://www.youtube.com/@someone/videos?x=1") == "https://www.youtube.com/@someone"
    assert automation.normalize_channel(
        "https://youtube.com/channel/UCabc/streams") == "https://www.youtube.com/channel/UCabc"
    for bad in ("https://example.com/@x", "https://youtu.be/169ZzvPxvvM",
                "https://www.youtube.com/watch?v=169ZzvPxvvM", "ftp://youtube.com/@x"):
        with pytest.raises(ValueError):
            automation.normalize_channel(bad)


def test_monitor_skips_the_back_catalogue_then_reports_new_items(tmp_path):
    channel = FakeChannel()
    channel.tabs["videos"] = [video("old00000001"), video("old00000002")]
    monitor = automation.Monitor(automation.Settings(tmp_path), lister=channel)
    monitor.add("@creator")
    seen = []
    take = lambda kind, entry, ch: seen.append((kind, entry["id"])) or True  # noqa: E731
    monitor.check(take)
    assert seen == [], "existing videos are never processed"
    channel.tabs["videos"].insert(0, video("new00000001"))
    channel.tabs["streams"] = [video("live0000001", "is_live"), video("soon0000001", "is_upcoming"),
                               video("new00000001")]                  # same video, two tabs
    monitor.check(take)
    assert seen == [("video", "new00000001"), ("live", "live0000001"), ("upcoming", "soon0000001")]
    seen.clear()
    monitor.check(take)
    assert seen == [], "taken items are never offered again"


def test_items_that_were_not_taken_are_offered_again(tmp_path):
    channel = FakeChannel()
    monitor = automation.Monitor(automation.Settings(tmp_path), lister=channel)
    monitor.add("@creator")
    monitor.check(lambda *a: True)
    channel.tabs["streams"] = [video("soon0000001", "is_upcoming")]
    offers = []
    monitor.check(lambda kind, e, c: offers.append(kind) or False)   # upcoming: wait
    channel.tabs["streams"] = [video("soon0000001", "is_live")]
    monitor.check(lambda kind, e, c: offers.append(kind) or True)
    assert offers == ["upcoming", "live"]


def test_monitor_errors_are_reported_not_raised(tmp_path):
    channel = FakeChannel()
    channel.fail = "HTTP Error 404"
    monitor = automation.Monitor(automation.Settings(tmp_path), lister=channel)
    entry = monitor.add("@gone")
    assert monitor.check(lambda *a: True) == [{"channel": entry["id"], "error": "HTTP Error 404"}]
    assert automation.Settings(tmp_path).channels()[0]["last_error"] == "HTTP Error 404"


def test_studio_monitor_creates_jobs_within_limits_and_never_publishes(studio):
    channel = FakeChannel()
    studio.app.monitor.lister = channel
    studio.app.settings.update({"max_new_sources_per_day": 2, "new_source_action": "all",
                                "record_live": False})
    call(studio, "/api/automation/channels", {"url": "@creator", "label": "Creator"})
    studio.app.check_channels()                                   # baseline
    existing = projects.create_project(studio.app.root, "https://youtu.be/dupe0000001", {})
    channel.tabs["videos"] = [video("dupe0000001"), video("vid00000001"), video("vid00000002"),
                              video("vid00000003")]
    channel.tabs["streams"] = [video("live0000001", "is_live")]
    studio.app.check_channels()
    assert wait_for(lambda: len(studio.runs) == 2)
    sources = {p["source"] for p in studio.app.list_projects()}
    assert "https://www.youtube.com/watch?v=vid00000001" in sources
    assert "https://www.youtube.com/watch?v=vid00000003" not in sources, "daily limit"
    assert len([p for p in studio.app.list_projects()
                if "dupe0000001" in p["source"]]) == 1, "no duplicate project"
    assert existing["id"] in {p["id"] for p in studio.app.list_projects()}
    assert {action for _, action in studio.runs} == {"all"}
    assert studio.app.uploads.entries() == [], "monitoring never drafts or uploads"
    events = " ".join(e["message"] for e in studio.app.events.tail(50))
    assert "Daily limit" in events and "Creator: new video" in events


def test_live_streams_found_by_monitoring_are_recorded(studio, monkeypatch):
    started = []
    monkeypatch.setattr(dashboard.Studio, "start_recording",
                        lambda self, project_id, settings=None: started.append(project_id))
    channel = FakeChannel()
    studio.app.monitor.lister = channel
    studio.app.monitor.add("@streamer")
    studio.app.check_channels()
    channel.tabs["streams"] = [video("live0000001", "is_live")]
    studio.app.check_channels()
    assert len(started) == 1
    state = projects.read_project(projects.project_path(studio.app.root, started[0]))
    assert state["recording"]["settings"]["after"] == "prepare"


# ------------------------------------------------- diagnosis + pipeline

@pytest.mark.parametrize("stage, error, expected", [
    ("source", "ERROR: Sign in to confirm you're not a bot", "refused the download"),
    ("source", "ERROR: Video unavailable", "unavailable"),
    ("transcribe", "RuntimeError: Library cublas64_12.dll is not found", "GPU libraries"),
    ("transcribe", "RuntimeError: mkl_malloc: failed to allocate memory", "out of memory"),
    ("render", "Candidate scores 2.5-6.7. No clips reached 8.0.", "minimum score"),
    ("select", "urlopen error [WinError 10060] connection timed out", "network"),
    ("transcribe", "Studio stopped. Resume this project to continue.", "Studio stopped"),
])
def test_diagnosis_names_the_stage_and_what_to_do(stage, error, expected):
    result = automation.diagnose({"status": "failed", "stage": stage, "error": error})
    assert result["stage"] == stage and expected.lower() in result["problem"].lower()
    assert result["advice"]


def test_diagnosis_is_not_fooled_by_normal_log_lines():
    log = ("PREFLIGHT OK (ffmpeg + libx264/aac, ffprobe, yt-dlp, faster-whisper, opencv)\n"
           "[abc] render\nSection download stalled for 2187-2282s. The source may not "
           "support ranged downloads.\n")
    result = automation.diagnose({"status": "failed", "stage": "render 1/1",
                                  "error": "full-source download blocked"}, log)
    assert "part of the video" in result["problem"]
    plain = automation.diagnose({"status": "failed", "stage": "select", "error": "boom"},
                                "PREFLIGHT OK (ffmpeg + libx264/aac)\n")
    assert plain["problem"] == "boom", "no false 'missing program' or 'encoding' diagnosis"
    missing = automation.diagnose({"status": "failed", "stage": "source",
                                   "error": r"Video file does not exist or URL is invalid: D:\x.mp4"})
    assert missing["problem"] == "The video file was not found."
    full = automation.diagnose({"status": "failed", "stage": "render",
                                "error": "OSError: [Errno 28] No space left on device"})
    assert full["problem"] == "The disk is full."
    broken = automation.diagnose({"status": "failed", "stage": "render 1/2",
                                  "error": "ffmpeg exited with status 1"})
    assert broken["problem"] == "Video encoding failed."


def test_no_diagnosis_for_healthy_projects():
    assert automation.diagnose({"status": "complete", "stage": "complete"}) is None


def test_pipeline_reflects_files_status_and_uploads(tmp_path):
    (tmp_path / "transcript.json").write_text("{}")
    (tmp_path / "candidates.json").write_text("[]")
    state = {"status": "running", "stage": "select", "cache": {"source": "k"}}
    assert automation.pipeline(tmp_path, state) == {
        "source": "done", "transcribe": "done", "discover": "done", "select": "active",
        "render": "pending", "upload": "pending"}
    (tmp_path / "segments.json").write_text("[]")
    (tmp_path / "clips").mkdir()
    (tmp_path / "clips" / "001.mp4").write_bytes(b"x")
    state = {"status": "complete", "stage": "complete", "cache": {"source": "k"}}
    line = automation.pipeline(tmp_path, state, [{"state": "queued"}])
    assert line["render"] == "done" and line["upload"] == "active"
    failed = automation.pipeline(tmp_path, {"status": "failed", "stage": "render 2/3"})
    assert failed["render"] == "failed"
    assert automation.progress({"stage": "render 2/4"})["fraction"] == 0.25
    assert automation.progress({"progress": {"stage": "transcribe", "done": 3,
                                             "total": 5}})["fraction"] == 0.6


# ------------------------------------------- end to end, fake everything

def test_monitor_to_upload_end_to_end_requires_approval(studio, connected, google, clock):
    """New video on a monitored channel -> processed -> draft -> nothing is
    uploaded until the user approves -> approved clip uploads as private."""
    app = studio.app
    app.youtube = connected
    app.uploads = queue_for(connected, clock)
    channel = FakeChannel()
    app.monitor.lister = channel
    app.settings.update({"new_source_action": "all"})
    app.monitor.add("@creator")
    app.check_channels()
    channel.tabs["videos"] = [video("e2e00000001", title="Mera pehla video")]
    app.check_channels()
    assert wait_for(lambda: len(studio.runs) == 1)
    project_id = studio.runs[0][0]
    path = projects.project_path(app.root, project_id)
    clip = make_clip(path, "001-abc-aaj-pehli-baar.mp4")
    clip.with_suffix(".meta.json").write_text(json.dumps({"start": 0.0, "end": 2.0,
                                                          "title": "aaj pehli baar #shorts"}))
    projects.atomic_json(path / "transcript.json", {"language": "hi", "words": [
        {"word": "आज", "start": 0.1, "end": 0.4}, {"word": "पहली", "start": 0.5, "end": 0.9},
        {"word": "बार।", "start": 1.0, "end": 1.2}]})
    entry = app.publish_draft(project_id, clip.name)
    assert entry["state"] == "draft" and entry["metadata"]["privacy"] == "private"
    assert not any("ऀ" <= ch <= "ॿ" for ch in json.dumps(entry["metadata"], ensure_ascii=False))
    app.uploads.process()
    assert google.inserts == [], "a draft never uploads"
    overview = call(studio, "/api/overview")
    row = next(p for p in overview["projects"] if p["id"] == project_id)
    assert row["pipeline"]["render"] == "done" and row["pipeline"]["upload"] == "pending"
    app.uploads.approve(entry["id"], confirm=True)
    app.uploads.process()
    assert len(google.inserts) == 1
    assert google.inserts[0]["status"]["privacyStatus"] == "private"
    row = next(p for p in call(studio, "/api/overview")["projects"] if p["id"] == project_id)
    assert row["pipeline"]["upload"] == "done"
