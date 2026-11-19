"""Live recording: start/stop, retries, disk and duration limits, recovery.

yt-dlp is replaced by fake process handles and time by a fake clock, so these
tests run in milliseconds without network access. A few tests at the end use
real ffmpeg (tiny generated clips) and real child processes.
"""
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import live
import projects

URL = "https://www.youtube.com/watch?v=live123"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeHandle:
    """Pretends to be yt-dlp: grows its output file on every poll."""

    def __init__(self, cmd, exit_after=None, code=0, grow=True, on_poll=None,
                 log=None, log_text="", split=False):
        ext = "ts" if "--hls-use-mpegts" in cmd else "mkv"
        out = cmd[cmd.index("-o") + 1].replace("%(ext)s", ext)
        if split:
            # Real from-start behaviour when stopped: separate video/audio
            # stream files plus yt-dlp bookkeeping, never a merged file.
            stem = out[: -len(".mkv")]
            out = stem + ".f399.mkv"
            Path(stem + ".f140.mkv").write_bytes(b"\0" * 64)
            Path(stem + ".f140.mkv.ytdl").write_text("{}")
            Path(stem + ".f140.mkv-Frag7").write_bytes(b"\0" * 8)
        self.path = Path(out)
        self.cmd = cmd
        if log is not None and log_text:
            log.write(log_text)
            log.flush()
        self.exit_after, self.code, self.grow = exit_after, code, grow
        self.on_poll = on_poll
        self.polls = 0
        self.returncode = None
        self.stopped = False
        self.closed = False
        self.pid = 4242

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        self.polls += 1
        if self.grow:
            with self.path.open("ab") as handle:
                handle.write(b"\x47" * 188)
        if self.on_poll:
            self.on_poll(self)
        if self.exit_after is not None and self.polls >= self.exit_after:
            self.returncode = self.code
        return self.returncode

    def stop(self, grace):
        self.stopped = True
        self.returncode = -1
        return -1

    def close(self):
        self.closed = True


class Harness:
    """Builds a recording project and a Recorder wired to fakes."""

    def __init__(self, tmp_path, settings=None, statuses=("is_live",),
                 handles=None, free=10 * 1024**4):
        self.root = tmp_path / "projects"
        state = live.start_recording_project(self.root, URL, settings or {})
        self.path = projects.project_path(self.root, state["id"])
        self.clock = Clock()
        self.stop = threading.Event()
        self.abort = threading.Event()
        self.statuses = list(statuses)
        self.handle_specs = list(handles or [{}])
        self.spawned = []
        self.free = free if callable(free) else (lambda: free)
        self.finalized = []
        self.finalize_error = None

    def probe(self, url):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"url": True, "live_status": status, "title": "Stream"}

    def spawn(self, cmd, log=None, cwd=None):
        spec = self.handle_specs.pop(0) if len(self.handle_specs) > 1 else self.handle_specs[0]
        handle = FakeHandle(cmd, log=log, **spec)
        self.spawned.append(handle)
        return handle

    def finalize(self, project_dir, keep_parts=False):
        if self.finalize_error:
            raise self.finalize_error
        self.finalized.append(sorted(p.name for p in live.part_files(project_dir)))
        out = Path(project_dir) / "recording.mp4"
        out.write_bytes(b"joined")
        return out, 123.0

    def recorder(self):
        return live.Recorder(
            self.path, self.stop, self.abort, spawn=self.spawn, probe=self.probe,
            finalize=self.finalize, free_bytes=lambda: self.free(),
            clock=self.clock, wall=time.time, wait=self.clock.advance,
            poll_seconds=2.0, wait_poll_seconds=60.0, stop_grace=1.0)

    def state(self):
        return projects.read_project(self.path)

    def rec(self):
        return self.state()["recording"]


def stop_at(event, polls):
    def hook(handle):
        if handle.polls >= polls:
            event.set()
    return hook


def test_record_from_now_then_stop_hands_off_to_processing(tmp_path):
    h = Harness(tmp_path)
    h.handle_specs = [{"on_poll": stop_at(h.stop, 5)}]
    assert h.recorder().run() == "recorded"
    handle = h.spawned[0]
    assert handle.stopped, "manual stop must stop the downloader"
    assert "--hls-use-mpegts" in handle.cmd and "--live-from-start" not in handle.cmd
    assert handle.cmd[handle.cmd.index("-o") + 1].endswith("part001.%(ext)s")
    # YouTube live serves separate video/audio HLS streams: ask for both.
    fmt = handle.cmd[handle.cmd.index("-f") + 1]
    assert fmt.startswith("bv*[height<=1080]+ba")
    state = h.state()
    assert state["recording"]["state"] == "recorded"
    assert state["recording"]["stop_reason"] == "stopped"
    assert state["source"] == str((h.path / "recording.mp4").resolve())
    assert state["source_url"] == URL
    assert state["status"] == "new" and state["error"] is None
    assert h.finalized == [["part001.ts"]]


def test_from_start_mode_uses_dvr_flags():
    settings = live.validate_settings({"from_start": True, "max_height": 720})
    cmd = live.build_command(URL, Path("rec/part001"), settings)
    assert "--live-from-start" in cmd
    assert cmd[cmd.index("-o") + 1].endswith("part001.%(ext)s")
    assert "--merge-output-format" in cmd and "mkv" in cmd
    fmt = cmd[cmd.index("-f") + 1]
    assert "height<=720" in fmt
    # H.264 first: YouTube otherwise picks AV1 (seen on a real stream).
    assert fmt.startswith("bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/")


# ------------------- regression: stopped "record from the beginning" (bug)

def test_part_groups_recognize_unmerged_from_start_streams(tmp_path):
    directory = tmp_path / "recording"
    directory.mkdir()
    for name in ("part001.f399.mkv", "part001.f140.mkv", "part002.ts"):
        (directory / name).write_bytes(b"media")
    for junk in ("part001.f140.mkv.ytdl", "part001.f140.mkv-Frag214",
                 "part001.f399.mkv.part", "concat.txt", "merged_part001.mkv"):
        (directory / junk).write_bytes(b"x")
    (directory / "part003.ts").write_bytes(b"")  # empty: nothing recorded
    groups = [[p.name for p in g] for g in live.part_groups(tmp_path)]
    assert groups == [["part001.f140.mkv", "part001.f399.mkv"], ["part002.ts"]]


def test_stopped_from_start_recording_is_finalized_not_reported_empty(tmp_path):
    h = Harness(tmp_path, settings={"from_start": True})
    h.handle_specs = [{"split": True, "on_poll": stop_at(h.stop, 4)}]
    assert h.recorder().run() == "recorded"
    assert h.finalized == [["part001.f140.mkv", "part001.f399.mkv"]]
    rec = h.rec()
    assert rec["state"] == "recorded"
    assert "nothing was recorded" not in rec["message"]
    assert h.state()["source"].endswith("recording.mp4")


def test_from_start_reconnect_and_resume_do_not_redownload_the_beginning(tmp_path):
    h = Harness(tmp_path, settings={"from_start": True})
    h.handle_specs = [{"split": True, "exit_after": 3, "code": 1},
                      {"on_poll": stop_at(h.abort, 3)}]
    assert h.recorder().run() == "interrupted"
    h.abort.clear()
    h.handle_specs = [{"on_poll": stop_at(h.stop, 3)}]
    assert h.recorder().run() == "recorded"
    flags = ["--live-from-start" in handle.cmd for handle in h.spawned]
    assert flags == [True, False, False]
    assert h.finalized == [["part001.f140.mkv", "part001.f399.mkv",
                            "part002.ts", "part003.ts"]]


def test_downloaded_bytes_without_usable_media_are_not_called_empty(tmp_path):
    h = Harness(tmp_path)
    h.handle_specs = [{"on_poll": stop_at(h.stop, 3)}]

    def vanish(handle):
        stop_at(h.stop, 3)(handle)
        if h.stop.is_set():
            # e.g. yt-dlp removed its own temporary file on exit
            handle.path.unlink()
    h.handle_specs = [{"on_poll": vanish}]
    assert h.recorder().run() == "failed"
    rec = h.rec()
    assert rec["bytes"] > 0
    assert "nothing was recorded" not in (rec["message"] or "")
    assert "no playable media" in rec["error"]


def test_find_clips_is_refused_until_the_recording_is_finalized(tmp_path):
    import dashboard
    h = Harness(tmp_path, settings={"from_start": True})
    h.handle_specs = [{"split": True, "on_poll": stop_at(h.abort, 3)}]
    assert h.recorder().run() == "interrupted"
    studio = dashboard.Studio(h.root)
    try:
        with pytest.raises(ValueError, match="not finalized"):
            studio.enqueue(h.path.name, "prepare")
        detail = dashboard.project_detail(h.root, h.path.name)
        assert detail["recording_parts"] == 1  # Finalize button is offered
    finally:
        studio.close()


def test_upcoming_stream_waits_then_records(tmp_path):
    h = Harness(tmp_path, statuses=["is_upcoming", "is_upcoming", "is_live"])
    h.handle_specs = [{"on_poll": stop_at(h.stop, 3)}]
    started = h.clock()
    assert h.recorder().run() == "recorded"
    assert h.clock() - started >= 120, "waited two polling intervals"
    assert len(h.spawned) == 1


def test_upcoming_stream_that_never_starts_times_out(tmp_path):
    h = Harness(tmp_path, settings={"max_wait": 120}, statuses=["is_upcoming"])
    assert h.recorder().run() == "failed"
    rec = h.rec()
    assert rec["state"] == "failed" and rec["stop_reason"] == "timeout"
    assert h.spawned == []


def test_regular_video_is_not_recorded(tmp_path):
    h = Harness(tmp_path, statuses=["not_live"])
    assert h.recorder().run() == "failed"
    assert "not a live stream" in h.rec()["error"]
    assert h.spawned == []


def test_network_interruption_reconnects_into_a_new_part(tmp_path):
    h = Harness(tmp_path, statuses=["is_live"])
    h.handle_specs = [{"exit_after": 3, "code": 1},
                      {"on_poll": stop_at(h.stop, 4)}]
    assert h.recorder().run() == "recorded"
    assert len(h.spawned) == 2
    rec = h.rec()
    assert rec["retries"] == 1
    assert [p["file"] for p in rec["parts"]] == ["part001", "part002"]
    assert rec["parts"][0]["reason"] == "exited" and rec["parts"][0]["exit"] == 1
    assert h.finalized == [["part001.ts", "part002.ts"]]


def test_permanent_errors_stop_without_pointless_retries(tmp_path):
    # Seen on a real stream: yt-dlp exits at once with a format error.
    h = Harness(tmp_path, statuses=["is_live"])
    h.handle_specs = [{"exit_after": 1, "code": 1, "grow": False,
                       "log_text": "ERROR: [youtube] x: Requested format is "
                                   "not available.\n"}]
    assert h.recorder().run() == "failed"
    rec = h.rec()
    assert len(h.spawned) == 1, "a permanent error is not retried"
    assert rec["stop_reason"] == "fatal"
    assert "no recordable format" in rec["error"]


def test_failed_attempts_do_not_count_toward_max_duration(tmp_path):
    h = Harness(tmp_path, settings={"max_duration": 60, "max_retries": 3})
    h.handle_specs = [{"exit_after": 10, "code": 1, "grow": False},
                      {"exit_after": 10, "code": 1, "grow": False},
                      {"on_poll": stop_at(h.stop, 3)}]
    assert h.recorder().run() == "recorded"
    parts = h.rec()["parts"]
    assert [p["seconds"] for p in parts[:2]] == [0.0, 0.0]
    assert h.rec()["stop_reason"] == "stopped", "not cut short by empty attempts"


def test_stream_ending_finalizes_the_recording(tmp_path):
    h = Harness(tmp_path, statuses=["is_live", "was_live"])
    h.handle_specs = [{"exit_after": 4, "code": 0}]
    assert h.recorder().run() == "recorded"
    assert h.rec()["stop_reason"] == "ended"


def test_reconnects_give_up_after_max_retries_but_keep_media(tmp_path):
    h = Harness(tmp_path, settings={"max_retries": 2}, statuses=["is_live"])
    h.handle_specs = [{"exit_after": 2, "code": 1}]
    assert h.recorder().run() == "recorded"
    assert len(h.spawned) == 3  # first try + 2 reconnects
    assert h.rec()["stop_reason"] == "retries_exhausted"


def test_stalled_download_is_restarted(tmp_path):
    h = Harness(tmp_path, settings={"stall_timeout": 30})
    h.handle_specs = [{"grow": False}, {"on_poll": stop_at(h.stop, 2)}]
    # The stalled first part still needs bytes on disk to count as media.
    assert h.recorder().run() in {"recorded", "failed"}
    first = h.spawned[0]
    assert first.stopped
    assert h.rec()["parts"][0]["reason"] == "stalled"
    assert len(h.spawned) == 2


def test_maximum_duration_stops_safely(tmp_path):
    h = Harness(tmp_path, settings={"max_duration": 60})
    assert h.recorder().run() == "recorded"
    rec = h.rec()
    assert rec["stop_reason"] == "max_duration"
    assert h.spawned[0].stopped
    # Wall-clock recording time stops at the limit (2 s polling granularity);
    # rec["seconds"] then becomes the duration ffprobe verified after joining.
    assert 60 <= rec["parts"][0]["seconds"] <= 64


def test_low_disk_space_stops_recording_and_keeps_it(tmp_path):
    calls = {"n": 0}

    def free():
        calls["n"] += 1
        return 100 * 1024**3 if calls["n"] < 6 else 1 * 1024**3

    h = Harness(tmp_path, settings={"min_free_gb": 2}, free=free)
    assert h.recorder().run() == "recorded"
    assert h.rec()["stop_reason"] == "disk"
    assert h.spawned[0].stopped


def test_low_disk_space_refuses_to_start(tmp_path):
    h = Harness(tmp_path, settings={"min_free_gb": 2}, free=1024**3)
    assert h.recorder().run() == "failed"
    assert h.rec()["stop_reason"] == "disk"
    assert h.spawned == []


def test_studio_closing_interrupts_and_keeps_parts(tmp_path):
    h = Harness(tmp_path)
    h.handle_specs = [{"on_poll": stop_at(h.abort, 3)}]
    assert h.recorder().run() == "interrupted"
    assert h.rec()["state"] == "interrupted"
    assert [p.name for p in live.part_files(h.path)] == ["part001.ts"]
    assert h.finalized == []


def test_interrupted_recording_resumes_into_the_next_part(tmp_path):
    h = Harness(tmp_path)
    h.handle_specs = [{"on_poll": stop_at(h.abort, 3)}]
    h.recorder().run()
    h.abort.clear()
    h.handle_specs = [{"on_poll": stop_at(h.stop, 3)}]
    assert h.recorder().run() == "recorded"
    assert h.finalized == [["part001.ts", "part002.ts"]]


def test_interrupted_recording_can_be_finalized_without_recording_more(tmp_path):
    h = Harness(tmp_path)
    h.handle_specs = [{"on_poll": stop_at(h.abort, 3)}]
    h.recorder().run()
    assert live.finalize_existing(h.path, finalize=h.finalize) == "recorded"
    assert h.state()["source"].endswith("recording.mp4")


def test_failed_join_keeps_every_part_for_retry(tmp_path):
    h = Harness(tmp_path)
    h.handle_specs = [{"on_poll": stop_at(h.stop, 3)}]
    h.finalize_error = live.RecordingError("ffmpeg could not join.")
    assert h.recorder().run() == "failed"
    rec = h.rec()
    assert "kept" in rec["error"] and rec["state"] == "failed"
    assert live.part_files(h.path), "parts must survive a failed join"


def test_studio_restart_marks_active_recordings_interrupted(tmp_path):
    import dashboard
    h = Harness(tmp_path)
    projects.update_project(h.path, lambda s: (
        s.update(status="recording"), s["recording"].update(state="recording")))
    studio = dashboard.Studio(h.root)
    try:
        state = h.state()
        assert state["recording"]["state"] == "interrupted"
        assert state["status"] == "interrupted"
    finally:
        studio.close()


def test_recording_settings_are_validated():
    assert live.validate_settings({})["after"] == "prepare"
    for bad in ({"max_duration": 5}, {"after": "upload"}, {"from_start": "yes"},
                {"min_free_gb": 0}, {"surprise": 1}):
        with pytest.raises(ValueError):
            live.validate_settings(bad)


# ------------------------------------------------------- real ffmpeg joins

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not installed")


def make_ts(path, seconds, size="320x240"):
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        "-f", "mpegts", str(path)], check=True)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


@needs_ffmpeg
def test_finalize_joins_parts_including_a_truncated_crash_part(tmp_path):
    directory = tmp_path / "recording"
    directory.mkdir()
    make_ts(directory / "part001.ts", 2)
    make_ts(directory / "part002.ts", 2)
    make_ts(directory / "part003.ts", 2)
    # Simulate a crash: chop the last part mid-file. MPEG-TS stays readable.
    data = (directory / "part003.ts").read_bytes()
    (directory / "part003.ts").write_bytes(data[: len(data) // 2])
    out, seconds = live.finalize_parts(tmp_path)
    assert out.name == "recording.mp4" and out.is_file()
    assert duration(out) == pytest.approx(seconds, abs=1.0)
    assert duration(out) > 4.0
    assert live.part_files(tmp_path) == [], "verified parts are cleaned up"


@needs_ffmpeg
def test_finalize_reencodes_parts_with_different_quality(tmp_path):
    directory = tmp_path / "recording"
    directory.mkdir()
    make_ts(directory / "part001.ts", 2, "320x240")
    make_ts(directory / "part002.ts", 2, "160x120")
    out, _ = live.finalize_parts(tmp_path, keep_parts=True)
    assert duration(out) > 3.5
    assert len(live.part_files(tmp_path)) == 2, "keep_parts keeps the parts"


def make_stream_file(path, seconds, kind):
    """Fragmented MP4 holding only video or only audio, like YouTube DASH."""
    if kind == "video":
        inputs = ["-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
                  "-c:v", "libx264", "-preset", "ultrafast"]
    else:
        inputs = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                  "-c:a", "aac"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs,
                    "-movflags", "frag_keyframe+empty_moov", "-f", "mp4",
                    str(path)], check=True)


@needs_ffmpeg
def test_finalize_merges_a_stopped_from_start_recording(tmp_path):
    directory = tmp_path / "recording"
    directory.mkdir()
    make_stream_file(directory / "part001.f137.mkv", 3, "video")
    make_stream_file(directory / "part001.f140.mkv", 5, "audio")
    (directory / "part001.f140.mkv.ytdl").write_text("{}")
    (directory / "part001.f140.mkv-Frag9").write_bytes(b"partial")
    out, seconds = live.finalize_parts(tmp_path)
    streams = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(out)], capture_output=True, text=True,
        check=True).stdout.split()
    assert sorted(streams) == ["audio", "video"]
    assert duration(out) == pytest.approx(3.0, abs=0.6)  # cut to the shorter
    assert list(directory.iterdir()) == [], "parts and yt-dlp leftovers removed"


def test_finalize_without_media_raises(tmp_path):
    with pytest.raises(live.RecordingError, match="No playable media"):
        live.finalize_parts(tmp_path)


# ------------------------------------------------- real process termination

TREE_SCRIPT = r'''
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(f"{child.pid}\n")
time.sleep(120)
'''


def start_tree(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    group = live.ProcessGroup([sys.executable, "-c", TREE_SCRIPT, str(pid_file)])
    deadline = time.time() + 30
    while time.time() < deadline and not (
            pid_file.exists() and pid_file.read_text().strip()):
        time.sleep(0.1)
    grandchild = int(pid_file.read_text())
    assert live.pid_alive(group.pid) and live.pid_alive(grandchild)
    return group, grandchild


def wait_dead(pid, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline and live.pid_alive(pid):
        time.sleep(0.1)
    return not live.pid_alive(pid)


def test_stop_terminates_the_whole_process_tree(tmp_path):
    group, grandchild = start_tree(tmp_path)
    group.stop(grace=2)
    assert wait_dead(group.pid), "downloader must stop"
    assert wait_dead(grandchild), "its ffmpeg-like child must not be orphaned"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")
def test_windows_job_object_kills_children_when_studio_lets_go(tmp_path):
    group, grandchild = start_tree(tmp_path)
    assert group.job is not None, "recorder runs inside a Job Object"
    group.close()  # what happens when Studio's process exits or crashes
    assert wait_dead(group.pid) and wait_dead(grandchild)
