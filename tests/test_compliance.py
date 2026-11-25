"""YouTube access after the download switch was removed (2026-11-25).

YouTube URLs (videos, youtu.be, Shorts, live) reach yt-dlp like any other
source. Monitoring other channels stays off unless explicitly enabled; the
connected channel is monitored through the official API.
"""
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
import live_ingest
import projects
import subtitles
import ytdlp

pytestmark = pytest.mark.compliance_default

URLS = ["https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://youtu.be/aaaaaaaaaaa",
        "https://m.youtube.com/live/aaaaaaaaaaa", "https://youtube.com/shorts/aaaaaaaaaaa",
        "https://www.youtube.com/@SomeChannel", "https://music.youtube.com/watch?v=x"]
URL = URLS[0]


def runner_spy():
    calls = []

    def run(cmd, **kwargs):
        calls.append([str(c) for c in cmd])
        raise AssertionError("yt-dlp runner was called")
    return run, calls


# --------------------------------------------------------------- policy

def test_youtube_downloads_are_enabled_with_no_switch():
    status = compliance.status()
    assert status["youtube_downloads"] is True and status["message"] is None
    caps = {k: v["state"] for k, v in status["capabilities"].items()}
    assert caps["audiovisual_download"] == "enabled" and caps["url_live_recording"] == "enabled"
    assert caps["own_channel_monitoring"] == "official_api"
    assert not hasattr(compliance, "require") and not hasattr(compliance, "YouTubeBlocked")


@pytest.mark.parametrize("url", URLS)
def test_youtube_addresses_are_recognized(url):
    assert compliance.is_youtube(url)


@pytest.mark.parametrize("url", ["https://vimeo.com/123", r"D:\Videos\clip.mp4",
                                 "https://notyoutube.com/watch?v=1", "", None])
def test_other_sources_are_not_youtube(url):
    assert not compliance.is_youtube(url)


# -------------------------------------------------- every yt-dlp path

@pytest.mark.parametrize("call", [
    lambda run, tmp: ingest.probe_source(URL, runner=run),
    lambda run, tmp: ingest.download_discovery(URL, tmp, {"has_audio": True}, runner=run),
    lambda run, tmp: ingest.download_visual_discovery(URL, tmp, runner=run),
    lambda run, tmp: ingest.download_section(URL, 10, 40, tmp / "s.mp4", runner=run),
    lambda run, tmp: ingest.download_full(URL, tmp / "f.mp4", runner=run),
    lambda run, tmp: subtitles.fetch(URL, tmp, "en-orig", "automatic", runner=run),
    lambda run, tmp: ingest._probe_url(URL, run),
])
def test_youtube_urls_reach_yt_dlp(tmp_path, call):
    run, calls = runner_spy()
    try:
        call(run, tmp_path)
    except AssertionError:
        pass
    assert calls and calls[0][1:3] == ["-m", "yt_dlp"]
    assert any(URL in part for part in calls[0])


def test_every_yt_dlp_command_gets_the_javascript_runtime(monkeypatch):
    ytdlp.runtime_args.cache_clear()
    monkeypatch.setattr(ytdlp.shutil, "which", lambda name: "node.exe" if name == "node" else None)
    assert ytdlp.base()[3:] == ["--js-runtimes", "node"]
    ytdlp.runtime_args.cache_clear()
    monkeypatch.setattr(ytdlp.shutil, "which", lambda name: "deno.exe" if name == "deno" else None)
    assert ytdlp.base()[3:] == [], "deno is yt-dlp's default"
    ytdlp.runtime_args.cache_clear()


def test_youtube_live_recording_builds_a_yt_dlp_command(tmp_path):
    cmd = live.build_command(URL, tmp_path / "part001", live.validate_settings({}))
    flat = [str(c) for c in (cmd[0] if isinstance(cmd, tuple) else cmd)]
    assert "yt_dlp" in flat and URL in flat


def test_full_source_resolution_downloads_youtube_urls(tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append([str(c) for c in cmd])
        (tmp_path / "source.mp4").write_bytes(b"video")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(dt.subprocess, "run", fake_run)
    try:
        dt.resolve_source(URL, tmp_path)
    except (Exception, SystemExit):        # noqa: BLE001 - only the command matters here
        pass
    assert seen and "yt_dlp" in seen[0] and URL in seen[0]


# ------------------------------------------------ live sessions (Phase 5)

def test_youtube_live_session_needs_a_stream_that_is_live(monkeypatch):
    monkeypatch.setattr(live_ingest, "youtube_live_status", lambda url: "is_live")
    assert live_ingest.validate_source("url", URL) == URL
    with pytest.raises(live_ingest.LiveError, match="Direct media URL"):
        live_ingest.validate_source("srt", URL)
    monkeypatch.setattr(live_ingest, "youtube_live_status", lambda url: "not_live")
    with pytest.raises(live_ingest.LiveError, match="not live right now"):
        live_ingest.validate_source("url", "https://youtu.be/aaaaaaaaaaa")


def test_youtube_live_session_resolves_a_fresh_stream_address(tmp_path, monkeypatch):
    resolved = []
    monkeypatch.setattr(live_ingest, "resolve_youtube_stream",
                        lambda url: resolved.append(url) or ["https://manifest.example/video.m3u8",
                                                             "https://manifest.example/audio.m3u8"])
    state = projects.create_project(tmp_path / "p", URL, {})
    session = live_ingest.Session(projects.project_path(tmp_path / "p", state["id"]),
                                  {"kind": "url", "source": URL, "started": 0.0})
    cmd = session._command(tmp_path / "list.csv", 0)
    assert resolved == [URL]
    assert "https://manifest.example/video.m3u8" in cmd and "https://manifest.example/audio.m3u8" in cmd
    assert URL not in cmd, "ffmpeg reads the resolved HLS addresses"
    maps = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-map"]
    assert maps == ["0:v?", "1:a?"], "video from the first stream, audio from the second"


# ------------------------------------------------------- Studio and CLI

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard.Studio, "_run", lambda *a, **k: 0)
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


def test_find_clips_accepts_youtube_urls(server, tmp_path):
    httpd, base = server
    token = httpd.app.token
    status, body = post(base, "/api/batch", {"sources": [URL, "https://youtube.com/shorts/bbbbbbbbbbb"]},
                        token)
    assert status == 201 and len(body["projects"]) == 2 and not body.get("errors")
    sources = sorted(p["source"] for p in httpd.app.list_projects())
    assert sources == sorted([URL, "https://youtube.com/shorts/bbbbbbbbbbb"])
    config = json.load(urllib.request.urlopen(base + "/api/config"))
    assert config["compliance"]["youtube_downloads"] is True
    assert config["compliance"]["message"] is None


def test_source_validation_accepts_youtube_urls(tmp_path):
    for url in URLS:
        assert dashboard.source_problem(url) is None, url
    assert "does not exist" in dashboard.source_problem(str(tmp_path / "missing.mp4"))


def test_batch_and_cli_create_youtube_projects(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(projects, "run_project", lambda path, **k: ran.append(path) or
                        {"status": "review"})
    results = projects.run_batch([URL], tmp_path / "batch", {})
    assert results[0]["id"] and results[0]["status"] != "failed"
    assert ran, "the project went into the normal pipeline"


def test_check_source_reads_youtube_with_yt_dlp(monkeypatch):
    monkeypatch.setattr(ingest, "probe_source", lambda source: {
        "source": source, "live_status": "not_live", "duration": 150.0, "title": "Llamigos",
        "is_url": True})
    info = dashboard.detect_source(URL)
    assert info["title"] == "Llamigos" and info["duration"] == 150.0


# ------------------------------- monitoring other channels: still opt-in

def test_external_monitoring_is_off_by_default_and_reversible(monkeypatch):
    monkeypatch.delenv(compliance.MONITOR_ENV, raising=False)
    assert compliance.external_monitoring_allowed() is False
    features = compliance._config_file()
    features.parent.mkdir(parents=True, exist_ok=True)
    features.write_text(json.dumps({"youtube_monitoring": True}), encoding="utf-8")
    assert compliance.external_monitoring_allowed() is True
    features.write_text(json.dumps({"youtube_monitoring": "yes"}), encoding="utf-8")
    assert compliance.external_monitoring_allowed() is False, "only a real true enables it"
    monkeypatch.setenv(compliance.MONITOR_ENV, "1")
    assert compliance.external_monitoring_allowed() is True


@pytest.mark.parametrize("content", [
    b"", b"{broken", b"[true]", b"true", b"null", b'{"youtube_monitoring": 1}',
    b'{"youtube_monitoring": "true"}', b"\xff\xfe\x00{"])
def test_malformed_config_keeps_external_monitoring_off(content, monkeypatch):
    monkeypatch.delenv(compliance.MONITOR_ENV, raising=False)
    path = compliance._config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    assert compliance.external_monitoring_allowed() is False


def test_other_channels_are_not_monitored_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(compliance.MONITOR_ENV, raising=False)
    settings = automation.Settings(tmp_path)
    monitor = automation.Monitor(settings)            # no official client connected
    with pytest.raises(ValueError, match="Connect your YouTube channel"):
        monitor.add("https://www.youtube.com/@SomeChannel")
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


# -------------------------------------------- existing projects stay usable

def test_existing_youtube_project_reuses_its_files(tmp_path, monkeypatch):
    from tests import test_fast_transcription as fast
    path = _made_project(tmp_path, monkeypatch, fast)
    before = {p.name: p.stat().st_mtime_ns for p in path.iterdir() if p.is_file()
              and p.name not in {"project.json", "job.log"}}
    detail = dashboard.project_detail(path.parent, path.name)
    assert detail["project"]["id"] == path.name and detail["transcript"]["words"]
    assert projects.run_project(path, action="prepare")["status"] == "review", \
        "cached source, transcript and candidates are reused"
    after = {p.name: p.stat().st_mtime_ns for p in path.iterdir() if p.is_file()
             and p.name not in {"project.json", "job.log"}}
    assert {k: after[k] for k in before} == before, "stored data untouched"


def _made_project(tmp_path, monkeypatch, fast):
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
    return path


def test_find_clips_projects_follow_the_ai_routes(server):
    """New projects from Studio's Find clips use the AI settings' routes."""
    from ai import policy
    from ai.registry import REGISTRY
    policy.update({"routes": {"transcribe": {"provider": "groq", "model": "whisper-large-v3-turbo"},
                              "generate": {"provider": "groq", "model": "openai/gpt-oss-20b"}}},
                  REGISTRY)
    httpd, base = server
    status, body = post(base, "/api/batch", {"sources": [URL], "options": {"min_score": 4}},
                        httpd.app.token)
    assert status == 201
    options = httpd.app.list_projects()[0]["options"]
    assert options["transcriber"] == "groq:whisper-large-v3-turbo"
    assert (options["llm_provider"], options["llm"]) == ("gateway", "groq:openai/gpt-oss-20b")
    assert options["min_score"] == 4
    assert options["aspect"] == "9:16", "new projects are Shorts unless the form says otherwise"
    status, body = post(base, "/api/batch", {"sources": ["https://youtu.be/ccccccccccc"],
                                             "options": {"transcriber": "local"}},
                        httpd.app.token)
    mine = [p for p in httpd.app.list_projects() if p["source"].endswith("ccccccccccc")][0]
    assert mine["options"]["transcriber"] == "local", "an explicit choice wins"
