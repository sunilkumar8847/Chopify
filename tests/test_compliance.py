"""Phase 0: no unofficial YouTube access while the switch is off (the default)."""
import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

import automation
import compliance
import dashboard
import download_and_transcribe as dt
import ingest
import live
import projects
import sections
import subtitles

pytestmark = pytest.mark.compliance_default

URLS = ["https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://youtu.be/aaaaaaaaaaa",
        "https://m.youtube.com/live/aaaaaaaaaaa", "https://youtube.com/shorts/aaaaaaaaaaa",
        "https://www.youtube.com/@SomeChannel", "https://music.youtube.com/watch?v=x"]


@pytest.fixture
def no_processes(monkeypatch):
    """Fail the test if anything tries to start a process."""
    started = []

    def refuse(*args, **kwargs):
        started.append(args[0] if args else kwargs.get("args"))
        raise AssertionError(f"a process was started: {started[-1]}")
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    return started


def runner_spy():
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        raise AssertionError("yt-dlp runner was called")
    return run, calls


# --------------------------------------------------------------- switch

def test_default_is_off_and_it_is_reversible(tmp_path, monkeypatch):
    assert compliance.youtube_downloads_allowed() is False
    features = compliance._config_file()
    features.parent.mkdir(parents=True, exist_ok=True)
    features.write_text(json.dumps({"youtube_downloads": True}), encoding="utf-8")
    assert compliance.youtube_downloads_allowed() is True
    features.write_text(json.dumps({"youtube_downloads": "yes"}), encoding="utf-8")
    assert compliance.youtube_downloads_allowed() is False, "only a real true enables it"
    features.write_text("{broken", encoding="utf-8")
    assert compliance.youtube_downloads_allowed() is False
    monkeypatch.setenv(compliance.ENV, "1")
    assert compliance.youtube_downloads_allowed() is True
    monkeypatch.setenv(compliance.ENV, "0")
    assert compliance.youtube_downloads_allowed() is False


@pytest.mark.parametrize("url", URLS)
def test_youtube_addresses_are_recognized(url):
    assert compliance.is_youtube(url) and compliance.blocked(url)


@pytest.mark.parametrize("url", ["https://vimeo.com/123", r"D:\Videos\clip.mp4",
                                 "https://notyoutube.com/watch?v=1", "", None])
def test_other_sources_are_not_touched(url):
    assert not compliance.blocked(url)


# -------------------------------------------------- every yt-dlp path

URL = URLS[0]


def test_metadata_probe_is_blocked_before_yt_dlp():
    run, calls = runner_spy()
    with pytest.raises(compliance.YouTubeBlocked):
        ingest.probe_source(URL, runner=run)
    assert calls == []


@pytest.mark.parametrize("call", [
    lambda run, tmp: ingest.download_discovery(URL, tmp, {"has_audio": True}, runner=run),
    lambda run, tmp: ingest.download_visual_discovery(URL, tmp, runner=run),
    lambda run, tmp: ingest.download_section(URL, 10, 40, tmp / "s.mp4", runner=run),
    lambda run, tmp: ingest.download_full(URL, tmp / "f.mp4", runner=run),
    lambda run, tmp: subtitles.fetch(URL, tmp, "en-orig", "automatic", runner=run),
])
def test_downloads_and_captions_are_blocked_before_yt_dlp(tmp_path, call):
    run, calls = runner_spy()
    with pytest.raises(compliance.YouTubeBlocked):
        call(run, tmp_path)
    assert calls == [] and list(tmp_path.iterdir()) == []


def test_full_video_download_and_sections_are_blocked(tmp_path, no_processes):
    with pytest.raises(compliance.YouTubeBlocked):
        dt.resolve_source(URL, tmp_path)
    with pytest.raises(compliance.YouTubeBlocked):
        sections.ensure_section(URL, tmp_path, 1, 10, 40)
    assert no_processes == []
    assert not (tmp_path / "sections").exists() or not any((tmp_path / "sections").iterdir()), \
        "no failed-section marker written"


def test_live_recording_is_blocked_and_creates_no_project(tmp_path, no_processes):
    with pytest.raises(compliance.YouTubeBlocked):
        live.build_command(URL, tmp_path / "part001", live.validate_settings({}))
    with pytest.raises(compliance.YouTubeBlocked):
        live.start_recording_project(tmp_path / "projects", URL)
    assert not (tmp_path / "projects").exists() or not list((tmp_path / "projects").iterdir())
    assert no_processes == []


def test_channel_monitoring_reads_nothing_and_keeps_channels(tmp_path, no_processes):
    settings = automation.Settings(tmp_path)
    with pytest.raises(compliance.YouTubeBlocked):
        automation.ytdlp_lister("https://www.youtube.com/@SomeChannel", "videos")
    monitor = automation.Monitor(settings)            # no official client connected
    with pytest.raises(ValueError, match="Connect your YouTube channel"):
        monitor.add("https://www.youtube.com/@SomeChannel")
    existing = [{"id": "c1", "url": "https://www.youtube.com/@Old", "label": "", "added": 1,
                 "baseline": True, "seen": ["a"], "last_check": 5, "last_error": None, "found": 2}]
    settings.save_channels(existing)
    results = monitor.check(lambda *a: True)
    assert "Connect your YouTube channel" in results[0]["error"]
    assert settings.channels() == existing, "monitored channels are kept as they were"
    assert no_processes == []


def test_non_youtube_urls_still_reach_the_downloader():
    run, calls = runner_spy()
    with pytest.raises(AssertionError):
        ingest.probe_source("https://vimeo.com/123", runner=run)
    assert len(calls) == 1


def test_switch_on_restores_the_old_behaviour(monkeypatch):
    monkeypatch.setenv(compliance.ENV, "1")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.CalledProcessError(1, cmd, stderr="ERROR: offline")
    meta = ingest.probe_source(URL, runner=run)        # probe failures become "unknown"
    assert len(calls) >= 1 and meta["source"] == URL


# ------------------------------------------------------- Studio and CLI

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
    req = urllib.request.Request(base + path, data=json.dumps(data).encode(), headers={
        "Content-Type": "application/json", "X-Chopify-Token": token})
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_studio_explains_and_creates_nothing(server, tmp_path, no_processes):
    httpd, base = server
    token = httpd.app.token
    status, body = post(base, "/api/batch", {"sources": [URL]}, token)
    assert status == 400 and "disabled pending written approval" in body["error"]
    assert httpd.app.list_projects() == []
    status, body = post(base, "/api/detect", {"source": URL}, token)
    # Phase 0.1: official API only; with no connection and no key it explains.
    assert status == 400 and "official API" in body["error"]
    status, body = post(base, "/api/record", {"source": URL}, token)
    assert status == 400 and "disabled pending written approval" in body["error"]
    assert httpd.app.list_projects() == []
    local = tmp_path / "mine.mp4"
    local.write_bytes(b"x")
    status, body = post(base, "/api/batch", {"sources": [str(local), URL]}, token)
    assert status == 201 and len(body["projects"]) == 1, "local files still work"
    assert "disabled pending written approval" in body["errors"][0]["error"]
    config = json.load(urllib.request.urlopen(base + "/api/config"))
    assert config["compliance"]["youtube_downloads"] is False
    assert config["compliance"]["message"] == compliance.MESSAGE
    assert no_processes == []


def test_studio_does_not_arm_the_channel_monitor(tmp_path):
    app = dashboard.Studio(tmp_path / "projects")
    try:
        app.settings.update({"monitor_enabled": True})
        app.settings.save_channels([{"id": "c1", "url": "https://www.youtube.com/@Old",
                                     "label": "", "added": 1, "baseline": True, "seen": [],
                                     "last_check": None, "last_error": None, "found": 0}])
        app._schedule_monitor()
        assert app.monitor_timer is None
    finally:
        app.close()


def test_resuming_a_recording_of_an_existing_project_is_blocked(tmp_path):
    app = dashboard.Studio(tmp_path / "projects")
    try:
        state = projects.create_project(app.root, URL, {})
        with pytest.raises(compliance.YouTubeBlocked):
            app.start_recording(state["id"])
        assert projects.read_project(projects.project_path(app.root, state["id"]))["status"] == "new"
    finally:
        app.close()


def test_cli_refuses_new_youtube_projects(tmp_path, monkeypatch, capsys):
    import chopify
    monkeypatch.setattr("sys.argv", ["chopify.py", URL, "--workdir", str(tmp_path / "w"),
                                     "--no-preflight", "--no-input"])
    with pytest.raises(SystemExit) as exc:
        chopify.main()
    assert exc.value.code == 1 and "disabled pending written approval" in capsys.readouterr().err
    assert not (tmp_path / "w" / "project.json").exists()
    results = projects.run_batch([URL], tmp_path / "batch", {})
    assert results[0]["status"] == "failed" and results[0]["id"] is None
    assert not (tmp_path / "batch").exists() or not list((tmp_path / "batch").iterdir())


# -------------------------------------------- existing projects stay usable

def test_existing_youtube_project_stays_viewable_and_reuses_its_files(tmp_path, monkeypatch):
    """Made while downloads were allowed; afterwards the switch is off."""
    from tests import test_fast_transcription as fast
    monkeypatch.setenv(compliance.ENV, "1")
    path, calls = _made_project(tmp_path, monkeypatch, fast)
    monkeypatch.delenv(compliance.ENV)
    before = {p.name: p.stat().st_mtime_ns for p in path.iterdir() if p.is_file()
              and p.name not in {"project.json", "job.log"}}
    detail = dashboard.project_detail(path.parent, path.name)
    assert detail["project"]["id"] == path.name and detail["transcript"]["words"]
    assert projects.run_project(path, action="prepare")["status"] == "review", \
        "cached source, transcript and candidates are reused; nothing downloads"
    after = {p.name: p.stat().st_mtime_ns for p in path.iterdir() if p.is_file()
             and p.name not in {"project.json", "job.log"}}
    assert {k: after[k] for k in before} == before, "stored data untouched"


def _made_project(tmp_path, monkeypatch, fast):
    calls = {"whisper": 0, "windows": [], "fetch": 0, "discover": 0, "select": 0}
    audio = tmp_path / "audio.webm"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(projects.ingest, "_probe_url",
                        lambda value, run=None: fast.meta())
    monkeypatch.setattr(projects.ingest, "_probe_local",
                        lambda path, run=None: {"duration": 600.0})
    monkeypatch.setattr(projects.ingest, "is_long_source", lambda m: True)
    monkeypatch.setattr(projects.ingest, "check_disk", lambda *a: None)
    monkeypatch.setattr(projects.ingest, "_find_media", lambda workdir, prefix: audio)
    monkeypatch.setattr(projects.ingest, "_run", lambda *a, **k: None)
    monkeypatch.setattr(dt, "cuda_status", lambda: (True, "ok"))
    monkeypatch.setattr(dt, "_load_model", lambda *a: object())
    monkeypatch.setattr(subtitles, "_run_fetch", None, raising=False)
    events = fast.caption_events()

    def fake_fetch_run(cmd, **kwargs):
        out = [c for c in cmd if "captions." in str(c)][0].replace("%(ext)s", "en-orig.json3")
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(events, handle)
    real_fetch = subtitles.fetch
    monkeypatch.setattr(subtitles, "fetch", lambda url, workdir, key, kind, runner=None:
                        real_fetch(url, workdir, key, kind, runner=fake_fetch_run))
    words = subtitles.parse_json3(events)
    monkeypatch.setattr(dt, "transcribe_range", lambda model, source, start, end, workdir,
                        decode=None, name="": ([], [w for w in words if start <= w["start"] < end], "hi"))

    def discover(path, **kwargs):
        projects.atomic_json(path / "candidates.json", {"candidates": []})

    def select(path, **kwargs):
        projects.atomic_json(path / "segments.json",
                             [{"start": 100.0, "end": 130.0, "hook": "h", "overall": 9}])
        projects.atomic_json(path / "selection.json", {"passing": 1})
    monkeypatch.setattr(projects.score_clips, "discover", discover)
    monkeypatch.setattr(projects.score_clips, "select", select)
    state = projects.create_project(tmp_path / "projects", URL, {})
    path = projects.project_path(tmp_path / "projects", state["id"])
    projects.run_project(path, action="prepare")
    assert projects.read_json(path / "transcript.json")["transcript_source"] == "youtube_automatic"
    return path, calls


# ------------------------------------------------ Phase 0 review additions

def test_source_validation_rejects_youtube_urls_while_off(tmp_path, monkeypatch):
    local = tmp_path / "clip.mp4"
    local.write_bytes(b"x")
    for url in URLS:
        assert dashboard.source_problem(url) == compliance.MESSAGE, url
        assert dashboard.source_problem(f"  {url}  ") == compliance.MESSAGE
    assert dashboard.source_problem("https://vimeo.com/123") is None
    assert dashboard.source_problem(str(local)) is None, "local files still validate"
    assert "does not exist" in dashboard.source_problem(str(tmp_path / "missing.mp4"))
    monkeypatch.setenv(compliance.ENV, "1")
    assert dashboard.source_problem(URLS[0]) is None, "switch on: accepted as before"


def test_default_is_off_with_neither_file_nor_variable(monkeypatch):
    monkeypatch.delenv(compliance.ENV, raising=False)
    assert not compliance._config_file().exists()
    assert compliance.youtube_downloads_allowed() is False
    status = compliance.status()
    assert status["youtube_downloads"] is False and status["message"] == compliance.MESSAGE
    caps = {k: v["state"] for k, v in status["capabilities"].items()}
    assert caps == {"metadata_lookup": "official_api", "own_channel_monitoring": "official_api",
                    "discovery": "planned", "caption_download": "deferred",
                    "audiovisual_download": "approval_required",
                    "url_live_recording": "approval_required"}


@pytest.mark.parametrize("content", [
    b"", b"{broken", b"[true]", b"true", b"null", b'"youtube_downloads"',
    b'{"youtube_downloads": 1}', b'{"youtube_downloads": "true"}',
    b'{"youtube_downloads": [true]}', b'{"YOUTUBE_DOWNLOADS": true}',
    b"\xff\xfe\x00{", "{\"youtube_downloads\": true".encode()])
def test_malformed_config_fails_safely_with_the_restriction_on(content):
    path = compliance._config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    assert compliance.youtube_downloads_allowed() is False
    with pytest.raises(compliance.YouTubeBlocked):
        compliance.require(URL, "test")
    assert compliance.status()["youtube_downloads"] is False


def test_config_path_that_is_a_folder_fails_safely():
    path = compliance._config_file()
    path.mkdir(parents=True, exist_ok=True)
    assert compliance.youtube_downloads_allowed() is False


@pytest.mark.parametrize("value,allowed", [("", False), ("0", False), ("false", False),
                                           ("no", False), ("maybe", False), ("1", True),
                                           ("TRUE", True), (" on ", True)])
def test_environment_values(monkeypatch, value, allowed):
    monkeypatch.setenv(compliance.ENV, value)
    assert compliance.youtube_downloads_allowed() is allowed


def test_private_probe_helper_is_guarded_too():
    run, calls = runner_spy()
    with pytest.raises(compliance.YouTubeBlocked):
        ingest._probe_url(URL, run)
    assert calls == []
