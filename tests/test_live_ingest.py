"""Phase 5: live ingestion, ring buffers, reconnects, watchdog, five sessions."""
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

import compliance
import live_ingest
import projects
import scheduler
from live_ingest import LiveError, LiveManager, Ring

FAST = {"rw_timeout_seconds": 3, "max_backoff_seconds": 2}


def write_obs(folder, seconds=20, name="2026-11-22 20-00-00.mkv", rate=15):
    """A simulated OBS recording that grows in real time."""
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-re", "-f", "lavfi", "-i",
         f"testsrc2=size=320x180:rate={rate}", "-f", "lavfi", "-i", "sine=f=300",
         "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-g", str(rate),
         "-c:a", "aac", str(Path(folder) / name)], stdin=subprocess.DEVNULL)


def wait_for(check, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.3)
    return False


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ts_segments(tmp_path):
    """A ring with 10 real 6-second segments on a 0..60 s timeline."""
    ring = Ring(tmp_path / "live")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=160x90:rate=10", "-t", "60", "-c:v", "libx264", "-g", "10",
                    "-f", "segment", "-segment_time", "6", "-reset_timestamps", "1",
                    str(ring.segments / "seg%08d.ts")], check=True)
    for i in range(10):
        ring.add(i, f"seg{i:08d}.ts", i * 6.0, i * 6.0 + 6.0)
    return ring


# ------------------------------------------------------------- validation

@pytest.mark.parametrize("kind, source", [
    ("srt", "https://example.com/feed"),
    ("url", "ftp://example.com/x"),
    ("url", "file:///C:/x.mp4"),
    ("obs_folder", "Z:/no/such/folder"),
    ("rtmp", "rtmp://x/y"),
])
def test_only_authorized_direct_sources_are_accepted(kind, source):
    with pytest.raises(LiveError):
        live_ingest.validate_source(kind, source)
    assert compliance.is_youtube("https://youtu.be/x")


def test_youtube_links_that_are_not_live_are_refused(monkeypatch):
    monkeypatch.setattr(live_ingest, "youtube_live_status", lambda url: "was_live")
    with pytest.raises(LiveError, match="not live right now"):
        live_ingest.validate_source("url", "https://youtube.com/live/abc")
    monkeypatch.setattr(live_ingest, "youtube_live_status", lambda url: "is_live")
    assert live_ingest.validate_source("url", "https://youtube.com/live/abc")


# ------------------------------------------------------------------- ring

def test_ring_keeps_recent_and_pinned_segments(ts_segments):
    ring = ts_segments
    pin = ring.pin(10, 15, "candidate")
    removed, freed = ring.trim(keep_seconds=24)
    kept = [s["seq"] for s in ring.live_segments()]
    assert kept == [1, 2, 6, 7, 8, 9], "pinned 6-18 s and the last 24 s survive"
    assert removed == 4 and freed > 0
    assert not (ring.segments / "seg00000000.ts").exists()
    assert (ring.segments / "seg00000001.ts").exists()
    ring.unpin(pin)
    ring.trim(keep_seconds=24)
    assert [s["seq"] for s in ring.live_segments()] == [6, 7, 8, 9]


def test_extract_joins_segments_and_reports_the_offset(ts_segments, tmp_path):
    out, offset = ts_segments.extract(14.0, 25.0, tmp_path / "clip.ts")
    duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                     "format=duration", "-of", "default=nw=1:nk=1", str(out)],
                                    capture_output=True, text=True).stdout)
    assert offset == pytest.approx(2.0) and duration == pytest.approx(18.0, abs=0.6)
    ts_segments.trim(keep_seconds=6)
    with pytest.raises(LiveError):
        ts_segments.extract(14.0, 25.0, tmp_path / "gone.ts")


def test_disk_watchdog_shrinks_then_stops_but_keeps_pins(ts_segments, tmp_path):
    free = [5.0]
    path = tmp_path
    projects_root = tmp_path / "p"
    state = projects.create_project(projects_root, "srt://127.0.0.1:9", {})
    session = live_ingest.Session(projects.project_path(projects_root, state["id"]),
                                  {"kind": "srt", "source": "srt://127.0.0.1:9",
                                   "started": time.time()}, {"min_free_gb": 10,
                                                             "min_keep_seconds": 6},
                                  free=lambda p: free[0])
    session.ring = ts_segments
    ts_segments.pin(0, 5, "keep me")
    assert session._watchdog() is None, "5 GB of a 10 GB floor: shrink, keep going"
    assert [s["seq"] for s in ts_segments.live_segments()] == [0, 9]
    free[0] = 2.0
    assert "Stopped" in session._watchdog()
    assert (ts_segments.segments / "seg00000000.ts").exists(), "pinned segment kept"
    del path


# --------------------------------------------------------- real sessions

def test_obs_folder_session_follows_the_growing_recording(tmp_path):
    obs = tmp_path / "obs"
    obs.mkdir()
    writer = write_obs(obs, seconds=20)
    manager = LiveManager(tmp_path / "projects", settings=FAST)
    try:
        time.sleep(1.5)
        pid = manager.start("obs_folder", str(obs), "OBS test")["project_id"]
        session = manager.sessions[pid]
        assert wait_for(lambda: session.ring.stats()["segments"] >= 2, 40)
        writer.wait(40)
        assert wait_for(lambda: session.ring.stats()["buffered_seconds"] >= 19, 30)
        # OBS starts a new recording in the same folder: the session switches.
        second = write_obs(obs, seconds=8, name="2026-11-22 20-30-00.mkv")
        assert wait_for(lambda: session.info.get("file", "").endswith("20-30-00.mkv"), 40)
        second.wait(30)
        assert wait_for(lambda: session.ring.stats()["buffered_seconds"] >= 26, 40)
        seqs = [s["seq"] for s in session.ring.live_segments()]
        assert seqs == sorted(set(seqs)), "no duplicate segments"
        starts = [s["start"] for s in session.ring.live_segments()]
        assert starts == sorted(starts), "one continuous session timeline"
    finally:
        writer.kill()
        manager.stop(pid)
    assert projects.read_project(projects.project_path(tmp_path / "projects", pid))["live"][
        "state"] == "stopped"


def test_srt_listener_session_reconnects_when_the_encoder_restarts(tmp_path):
    port = free_port()
    manager = LiveManager(tmp_path / "projects", settings=FAST)
    pid = manager.start("srt", f"srt://127.0.0.1:{port}?mode=listener", "SRT")["project_id"]
    session = manager.sessions[pid]

    def push(seconds):
        return subprocess.Popen(
            ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15",
             "-f", "lavfi", "-i", "sine=f=440", "-t", str(seconds), "-c:v", "libx264",
             "-preset", "ultrafast", "-g", "15", "-c:a", "aac", "-f", "mpegts",
             f"srt://127.0.0.1:{port}?mode=caller"], stdin=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        first = push(14)
        assert wait_for(lambda: session.ring.stats()["segments"] >= 1, 40)
        first.wait(40)
        assert wait_for(lambda: session.info.get("reconnects", 0) >= 1, 40)
        before = session.ring.stats()["segments"]
        time.sleep(1.5)
        second = push(14)
        assert wait_for(lambda: session.ring.stats()["segments"] > before, 40)
        second.wait(40)
    finally:
        manager.stop(pid)
    segs = session.ring.live_segments()
    assert [s["seq"] for s in segs] == sorted({s["seq"] for s in segs})


def test_five_sessions_run_together_and_a_sixth_is_refused(tmp_path):
    sched = scheduler.Scheduler(tmp_path / "s.db", health=lambda: None,
                                leftover=lambda pid: False)
    manager = LiveManager(tmp_path / "projects", scheduler=sched, settings=FAST)
    writers, ids = [], []
    try:
        for i in range(5):
            folder = tmp_path / f"obs{i}"
            folder.mkdir()
            writers.append(write_obs(folder, seconds=20, rate=10))
        time.sleep(1.5)
        for i in range(5):
            ids.append(manager.start("obs_folder", str(tmp_path / f"obs{i}"), f"cam {i}")[
                "project_id"])
        extra = tmp_path / "obs5"
        extra.mkdir()
        with pytest.raises(LiveError, match="source slots are busy"):
            manager.start("obs_folder", str(extra), "sixth")
        assert sched.status()["active"] == 5
        assert wait_for(lambda: all(manager.sessions[p].ring.stats()["segments"] >= 2
                                    for p in ids), 60)
    finally:
        for w in writers:
            w.kill()
        for p in ids:
            manager.stop(p)
    assert sched.status()["active"] == 0, "slots freed when sessions stop"


def test_studio_restart_resumes_active_sessions(tmp_path):
    port = free_port()
    manager = LiveManager(tmp_path / "projects", settings=FAST)
    pid = manager.start("srt", f"srt://127.0.0.1:{port}?mode=listener", "SRT")["project_id"]
    pusher = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15",
         "-t", "40", "-c:v", "libx264", "-preset", "ultrafast", "-g", "15", "-f", "mpegts",
         f"srt://127.0.0.1:{port}?mode=caller"], stdin=subprocess.DEVNULL)
    again = None
    try:
        assert wait_for(lambda: manager.sessions[pid].ring.stats()["segments"] >= 2, 40)
        manager.shutdown()                     # Studio stops; the session stays "active"
        before = Ring(projects.project_path(tmp_path / "projects", pid) / "live").last()["seq"]
        again = LiveManager(tmp_path / "projects", settings=FAST)
        assert again.recover() == [pid]
        pusher.kill()                          # the encoder reconnects (OBS does this)
        time.sleep(1.5)
        pusher = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i",
             "testsrc2=size=320x180:rate=15", "-t", "30", "-c:v", "libx264", "-preset",
             "ultrafast", "-g", "15", "-f", "mpegts", f"srt://127.0.0.1:{port}?mode=caller"],
            stdin=subprocess.DEVNULL)
        assert wait_for(lambda: again.sessions[pid].ring.last()["seq"] > before + 1, 60)
    finally:
        pusher.kill()
        if again and pid in again.sessions:
            again.stop(pid)
    segs = again.sessions[pid].ring.live_segments()
    assert [s["seq"] for s in segs] == sorted({s["seq"] for s in segs}), "no duplicates"


def test_partial_segments_left_by_a_crash_are_removed(ts_segments):
    ring = ts_segments
    (ring.segments / "seg00000010.ts").write_bytes(b"half written")
    (ring.segments / "notes.txt").write_text("not a segment")
    assert ring.drop_unindexed() == 1
    assert not (ring.segments / "seg00000010.ts").exists()
    assert (ring.segments / "seg00000009.ts").exists(), "indexed segments untouched"
    assert (ring.segments / "notes.txt").exists()
