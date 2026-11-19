"""YouTube live/VOD detection and the processing guard (yt-dlp is mocked)."""
import json
import subprocess

import pytest

import ingest
import projects


class FakeResult:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def probe_with(monkeypatch, payload):
    calls = []
    monkeypatch.setattr(ingest, "_run", lambda cmd, **kw: calls.append(cmd)
                        or FakeResult(json.dumps(payload)))
    return ingest.probe_source("https://www.youtube.com/watch?v=abc"), calls


HLS = [{"vcodec": "avc1", "acodec": "mp4a", "height": 1080, "tbr": 5000,
        "protocol": "m3u8_native"}]


def test_active_livestream_is_detected(monkeypatch):
    meta, _ = probe_with(monkeypatch, {"live_status": "is_live", "is_live": True,
                                       "title": "Live now", "formats": HLS})
    assert meta["live_status"] == "is_live" and meta["is_live"] is True
    info = ingest.describe_source(meta)
    assert info["kind"] == "live" and info["can_record"] and not info["can_process"]


def test_live_detected_from_is_live_flag_without_live_status(monkeypatch):
    meta, _ = probe_with(monkeypatch, {"is_live": True, "formats": HLS})
    assert meta["live_status"] == "is_live"


def test_upcoming_stream_detected_through_metadata_retry(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if "--ignore-no-formats-error" not in cmd:
            raise subprocess.CalledProcessError(
                1, cmd, stderr="ERROR: This live event will begin in 3 hours.")
        return FakeResult(json.dumps({
            "live_status": "is_upcoming", "title": "Launch stream",
            "release_timestamp": 1900000000, "formats": []}))

    monkeypatch.setattr(ingest, "_run", run)
    meta = ingest.probe_source("https://www.youtube.com/watch?v=soon")
    assert meta["live_status"] == "is_upcoming"
    assert meta["release_timestamp"] == 1900000000
    assert meta["title"] == "Launch stream"
    assert len(calls) == 2 and calls[1][-1] == "https://www.youtube.com/watch?v=soon"
    info = ingest.describe_source(meta)
    assert info["kind"] == "upcoming" and info["can_record"]
    assert "scheduled for" in info["message"]


def test_upcoming_stream_detected_from_error_text_alone(monkeypatch):
    def run(cmd, **kwargs):
        raise subprocess.CalledProcessError(
            1, cmd, stderr="ERROR: [youtube] x: Premieres in 2 days")

    monkeypatch.setattr(ingest, "_run", run)
    meta = ingest.probe_source("https://www.youtube.com/watch?v=x")
    assert meta["live_status"] == "is_upcoming"


def test_unrelated_probe_failure_stays_unknown(monkeypatch):
    def run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="ERROR: Video unavailable")

    monkeypatch.setattr(ingest, "_run", run)
    meta = ingest.probe_source("https://www.youtube.com/watch?v=gone")
    assert meta["live_status"] is None
    assert ingest.describe_source(meta)["kind"] == "unknown"


def test_completed_livestream_is_processed_like_a_video(monkeypatch):
    meta, _ = probe_with(monkeypatch, {"live_status": "was_live", "duration": 10800,
                                       "formats": HLS})
    info = ingest.describe_source(meta)
    assert meta["live_status"] == "was_live"
    assert info["kind"] == "live_vod" and info["can_process"] and not info["can_record"]
    assert meta["live_status"] not in ingest.BLOCKED_LIVE


def test_recently_ended_stream_is_still_processing(monkeypatch):
    meta, _ = probe_with(monkeypatch, {"live_status": "post_live", "formats": []})
    info = ingest.describe_source(meta)
    assert info["kind"] == "processing" and not info["can_process"]
    assert "still processing" in info["message"]


def test_normal_vod_keeps_existing_fields(monkeypatch):
    payload = {"duration": 600, "title": "Video", "live_status": "not_live",
               "formats": [{"vcodec": "h264", "height": 720, "filesize": 100},
                           {"acodec": "aac", "vcodec": "none", "filesize": 10}]}
    meta, calls = probe_with(monkeypatch, payload)
    assert meta["duration"] == 600 and meta["estimated_size"] == 110
    assert meta["live_status"] == "not_live"
    assert ingest.describe_source(meta)["kind"] == "video"
    assert len(calls) == 1  # a successful probe is never retried
    # Extractors without live fields are treated as normal videos.
    meta, _ = probe_with(monkeypatch, {"duration": 60, "formats": []})
    assert meta["live_status"] == "not_live"


def test_local_files_are_classified_as_local(tmp_path):
    assert ingest.describe_source({"url": False})["kind"] == "local"


# --------------------------------------------------------- processing guard

def make_project(tmp_path, source="https://www.youtube.com/watch?v=abc"):
    state = projects.create_project(tmp_path / "projects", source, {})
    return projects.project_path(tmp_path / "projects", state["id"])


def forbid_downloads(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("a live URL must never start a download")
    monkeypatch.setattr(projects.ingest, "download_discovery", fail)
    monkeypatch.setattr(projects.dt, "resolve_source", fail)


@pytest.mark.parametrize("status", ["is_live", "is_upcoming", "post_live"])
def test_processing_refuses_live_urls_instead_of_downloading_forever(
        tmp_path, monkeypatch, status):
    path = make_project(tmp_path)
    monkeypatch.setattr(projects.ingest, "probe_source", lambda value: {
        "url": True, "duration": None, "live_status": status,
        "estimated_size": None})
    forbid_downloads(monkeypatch)
    with pytest.raises(ValueError) as exc:
        projects.run_project(path, action="prepare")
    state = projects.read_project(path)
    assert state["status"] == "failed"
    assert state["source_meta"]["live_status"] == status
    if status != "post_live":
        assert "Start recording" in str(exc.value)


def test_live_status_is_re_probed_not_cached(tmp_path, monkeypatch):
    path = make_project(tmp_path)
    calls = []

    def probe(value):
        calls.append(value)
        return {"url": True, "duration": None, "live_status": "is_upcoming"}

    monkeypatch.setattr(projects.ingest, "probe_source", probe)
    forbid_downloads(monkeypatch)
    for _ in range(2):
        with pytest.raises(ValueError):
            projects.run_project(path, action="prepare")
    assert len(calls) == 2


def test_completed_livestream_continues_into_the_normal_long_pipeline(
        tmp_path, monkeypatch):
    path = make_project(tmp_path)
    monkeypatch.setattr(projects.ingest, "probe_source", lambda value: {
        "url": True, "duration": 4 * 3600, "live_status": "was_live",
        "has_audio": True, "estimated_size": 1})
    monkeypatch.setattr(projects.ingest, "check_disk", lambda *args: None)
    reached = []

    def discovery(url, workdir, meta=None):
        reached.append(url)
        raise RuntimeError("stop here")

    monkeypatch.setattr(projects.ingest, "download_discovery", discovery)
    with pytest.raises(RuntimeError, match="stop here"):
        projects.run_project(path, action="prepare")
    assert reached == ["https://www.youtube.com/watch?v=abc"]
