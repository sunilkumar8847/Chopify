"""Phase 6: continuous live clipping on a simulated feed (AI faked, render real)."""
import json
import subprocess
import threading
import time

import numpy as np
import pytest

import live_clipper
import live_ingest
import scheduler
from live_clipper import LiveClipper, Retry

BURSTS = (25, 65)            # loud moments in the simulated stream (seconds)


def feed(folder, seconds=95):
    """Simulated OBS recording: quiet talk-like tone, two loud bursts."""
    expr = ("0.03*sin(2*PI*220*t)"
            + "".join(f"+if(between(t\\,{b}\\,{b + 3})\\,0.7*sin(2*PI*880*t)\\,0)" for b in BURSTS))
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-re", "-f", "lavfi", "-i",
         "testsrc2=size=480x270:rate=15", "-f", "lavfi", "-i", f"aevalsrc={expr}:s=16000",
         "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
         "-c:a", "aac", str(folder / "2026-11-22 21-00-00.mkv")], stdin=subprocess.DEVNULL)


def wait_for(check, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.5)
    return False


def test_find_peaks_uses_a_robust_baseline():
    quiet = list(np.full(200, 0.03) + np.random.default_rng(1).normal(0, 0.002, 200))
    window = np.full(60, 0.03)
    window[30:34] = 0.5
    peaks = live_clipper.find_peaks(window, quiet)
    assert len(peaks) == 1 and 30 <= peaks[0][0] <= 33
    assert live_clipper.find_peaks(np.full(60, 0.03), quiet) == []
    window[10] = 0.5                       # a single click is not a moment
    assert len(live_clipper.find_peaks(window, quiet)) == 1


def test_simulated_live_feed_produces_clips_while_ingestion_continues(tmp_path):
    obs = tmp_path / "obs"
    obs.mkdir()
    sched = scheduler.Scheduler(tmp_path / "s.db", settings=scheduler.validate_settings(
        {"poll_seconds": 0.2, "retry_backoff_seconds": 1}), health=lambda: None,
        leftover=lambda pid: False)
    calls = {"transcribe": 0, "judge": 0}
    progress_during_ai = []

    def transcribe(file, file_start, project_id):
        calls["transcribe"] += 1
        if calls["transcribe"] <= 2:
            raise Retry("Transcription paused: Groq free-plan rate limit reached")
        session = manager.sessions[project_id]
        before = session.ring.stats()["segments"]
        time.sleep(4)                          # a slow provider
        progress_during_ai.append(session.ring.stats()["segments"] - before)
        duration = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
             "default=nw=1:nk=1", str(file)], capture_output=True, text=True).stdout)
        words, t = [], 0.2
        sentence = "so we push left now. wait watch this jump. oh my god that was insane!".split()
        while t < duration - 0.5:
            word = sentence[len(words) % len(sentence)]
            words.append({"word": word, "start": round(file_start + t, 2),
                          "end": round(file_start + t + 0.4, 2)})
            t += 0.5
        return words, "en"

    def judge(path, seg, text, words, language):
        calls["judge"] += 1
        return (8.5 if calls["judge"] == 1 else 3.0), True

    clipper = LiveClipper(sched, settings={"detect_every": 10, "cooldown": 20, "pin_before": 30,
                                           "pin_after": 12, "pre_context": 15,
                                           "post_context": 6},
                          transcribe=transcribe, judge=judge)
    sched.handlers = {
        "ai": lambda task, cancel, set_pid: clipper.handle_judge(task["payload"], cancel,
                                                                  task["source_id"]),
        "render": lambda task, cancel, set_pid: clipper.handle_render(task["payload"], cancel,
                                                                      task["source_id"])}
    manager = live_ingest.LiveManager(tmp_path / "projects", scheduler=sched,
                                      settings={"rw_timeout_seconds": 4},
                                      on_segment=clipper.on_segment)
    writer = feed(obs)
    sched.start()
    try:
        time.sleep(1.5)
        pid = manager.start("obs_folder", str(obs), "sim")["project_id"]
        session = manager.sessions[pid]
        clips = session.path / "clips"
        assert wait_for(lambda: len(clipper.status(pid)) >= 2, 120), clipper.status(pid)
        assert wait_for(lambda: any(c["state"] == "done" for c in clipper.status(pid))
                        and all(c["state"] in ("done", "rejected") for c in clipper.status(pid)),
                        240), clipper.status(pid)
        items = clipper.status(pid)
        peaks = sorted(c["peak"] for c in items)
        assert len(items) == 2
        for peak, burst in zip(peaks, BURSTS):
            assert burst - 1 <= peak <= burst + 5, "candidates sit on the loud moments"
        done = next(c for c in items if c["state"] == "done")
        rejected = next(c for c in items if c["state"] == "rejected")
        # Context: the clip starts before the moment and ends after it.
        assert done["seg"]["start"] < done["peak"] - 5 and done["seg"]["end"] > done["peak"]
        assert done["seg"]["end"] - done["seg"]["start"] <= 120
        assert calls["transcribe"] >= 4, "quota errors were retried, not dropped"
        assert max(progress_during_ai) >= 0
        assert rejected["score"] == 3.0
        mp4 = [p for p in clips.glob("*.mp4")]
        assert len(mp4) == 1
        probe = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width,height:format=duration",
             "-of", "json", str(mp4[0])], capture_output=True, text=True).stdout)
        assert (probe["streams"][0]["width"], probe["streams"][0]["height"]) == (1080, 1920)
        assert float(probe["format"]["duration"]) <= 120.5
        assert done["latency_seconds"] < 300, "ready within five minutes"
        assert session.ring.stats()["pins"] == 0, "pins released after render/reject"
        # Stopping the session keeps the finished clip.
        writer.kill()
        manager.stop(pid)
        assert mp4[0].exists()
    finally:
        writer.kill()
        clipper.shutdown()
        sched.stop()
