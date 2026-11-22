"""Phase 2: five-source persistent scheduler."""
import threading
import time

import pytest

import scheduler
from scheduler import Scheduler, SchedulerError


def make(tmp_path, handlers=None, health=lambda: None, **changes):
    settings = scheduler.validate_settings({"poll_seconds": 0.05, "retry_backoff_seconds": 0,
                                            **changes} if "poll_seconds" not in changes else changes)
    return Scheduler(tmp_path / "s.db", settings=settings, handlers=handlers or {},
                     health=health, leftover=lambda pid: False)


def chain(stages=("ingest", "analysis", "render"), gpu=False):
    return [{"stage": s, "payload": {"step": s},
             "resources": ["gpu"] if gpu and s == "analysis" else []} for s in stages]


def test_five_sources_are_active_and_the_sixth_waits(tmp_path):
    s = make(tmp_path)
    for i in range(5):
        assert s.submit(f"p{i}", "vod", chain())["state"] == "active"
    sixth = s.submit("p5", "vod", chain())
    assert sixth["state"] == "waiting" and "position 1" in sixth["message"]
    assert s.status()["active"] == 5 and s.status()["waiting"] == 1
    with pytest.raises(SchedulerError, match="already queued"):
        s.submit("p0", "vod", chain())
    # Sixth starts as soon as one finishes.
    for _ in range(3):
        task = s.lease("ingest") if _ == 0 else s.lease("analysis" if _ == 1 else "render")
        assert task["source_id"] == "p0"
        s.complete(task["id"])
    assert s.source("p0")["state"] == "done"
    assert s.source("p5")["state"] == "active"


def test_full_waiting_queue_rejects_with_a_reason(tmp_path):
    s = make(tmp_path, max_sources=5, max_waiting=1)
    for i in range(6):
        s.submit(f"p{i}", "vod", chain())
    with pytest.raises(SchedulerError, match="All 5 source slots are busy"):
        s.submit("p6", "vod", chain())


def test_live_sessions_go_first_for_slots_and_work(tmp_path):
    s = make(tmp_path, max_sources=1)
    s.submit("vod-a", "vod", chain(("analysis",)))
    s.submit("vod-b", "vod", chain(("analysis",)))
    s.submit("live-a", "live", chain(("analysis",)))
    s.complete(s.lease("analysis")["id"])           # vod-a finishes
    assert s.source("live-a")["state"] == "active", "live jumps the waiting queue"
    assert s.source("vod-b")["state"] == "waiting"


def test_stage_pools_and_gpu_resource_are_independent(tmp_path):
    s = make(tmp_path, pools={"ingest": 5, "analysis": 2, "ai": 2, "render": 1})
    for i in range(5):
        s.submit(f"p{i}", "vod", [{"stage": "analysis", "resources": ["gpu"]}, {"stage": "render"}])
        s.add_task(f"p{i}", "ingest")
    ingests = [s.lease("ingest") for _ in range(6)]
    assert sum(1 for t in ingests if t) == 5, "ingest never waits for analysis"
    first = s.lease("analysis")
    assert first and s.lease("analysis") is None, "only one GPU task at a time"
    s.complete(first["id"])
    render = s.lease("render")
    assert render and s.lease("render") is None, "one renderer by default"
    assert s.lease("analysis") is not None, "analysis continues while rendering"


def test_slow_ai_never_blocks_ingestion(tmp_path):
    release = threading.Event()
    seen = []

    def slow_ai(task, cancel, set_pid):
        release.wait(5)

    def ingest(task, cancel, set_pid):
        seen.append(task["source_id"])
    s = make(tmp_path, handlers={"ai": slow_ai, "ingest": ingest})
    for i in range(5):
        s.submit(f"p{i}", "live", [{"stage": "ai"}], meta={"open": True})
    s.start()
    try:
        time.sleep(0.3)
        for i in range(5):
            s.add_task(f"p{i}", "ingest", dedupe=f"seg-{i}")
        deadline = time.time() + 5
        while len(seen) < 5 and time.time() < deadline:
            time.sleep(0.05)
        assert sorted(seen) == [f"p{i}" for i in range(5)]
        assert s.status()["stages"]["ai"]["running"] == 2, "AI pool stays at 2"
    finally:
        release.set()
        s.stop()


def test_restart_recovers_leases_without_duplicates(tmp_path):
    first = make(tmp_path)
    first.submit("p0", "vod", chain(("analysis", "render")))
    leased = first.lease("analysis")
    first.renew(leased["id"], pid=4242)
    stopped = []
    # Studio is killed: a new process opens the same database.
    second = Scheduler(tmp_path / "s.db", settings=first.settings, health=lambda: None,
                       leftover=lambda pid: stopped.append(pid) or True)
    assert second.recover() == [leased["id"]]
    assert stopped == [4242], "the leftover worker is stopped before re-running"
    again = second.lease("analysis")
    assert again["id"] == leased["id"] and again["attempts"] == 2
    assert second.lease("analysis") is None, "no duplicate task"
    second.complete(again["id"])
    assert second.lease("render")["source_id"] == "p0"


def test_a_task_that_keeps_crashing_studio_stops(tmp_path):
    s = make(tmp_path, max_attempts=2)
    s.submit("p0", "vod", chain(("analysis", "render")))
    for _ in range(2):
        s.lease("analysis")
        s = Scheduler(tmp_path / "s.db", settings=s.settings, health=lambda: None,
                      leftover=lambda pid: False)
        s.recover()
    info = s.source("p0")
    assert info["state"] == "failed" and "restarts" in info["message"]
    assert [t["state"] for t in info["tasks"]] == ["failed", "cancelled"]


def test_failed_tasks_retry_with_backoff_then_fail(tmp_path):
    now = [1000.0]
    s = Scheduler(tmp_path / "s.db", settings=scheduler.validate_settings(
        {"retry_backoff_seconds": 10}), health=lambda: None, clock=lambda: now[0],
        leftover=lambda pid: False)
    s.submit("p0", "vod", [{"stage": "ai", "max_failures": 2}, {"stage": "render"}])
    s.fail(s.lease("ai")["id"], "rate limited")
    assert s.lease("ai") is None, "waits for the backoff"
    now[0] += 11
    s.fail(s.lease("ai")["id"], "rate limited again")
    info = s.source("p0")
    assert info["state"] == "failed" and info["tasks"][1]["state"] == "cancelled"


def test_cancel_stops_running_work_and_keeps_finished_clips(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    started = threading.Event()
    stopped = []

    def render(task, cancel, set_pid):
        (clips / f"{task['payload']['n']}.mp4").write_bytes(b"final clip")
        if task["payload"]["n"] == 2:
            started.set()
            cancel.wait(5)
            stopped.append(True)
    s = make(tmp_path, handlers={"render": render})
    s.submit("p0", "vod", [{"stage": "render", "payload": {"n": n}} for n in (1, 2, 3)])
    s.on_cancel("p0", lambda: stopped.append("callback"))
    s.start()
    try:
        assert started.wait(5)
        s.cancel("p0")
        deadline = time.time() + 5
        while len(stopped) < 2 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        s.stop()
    info = s.source("p0")
    assert info["state"] == "cancelled"
    assert [t["state"] for t in info["tasks"]] == ["done", "cancelled", "cancelled"]
    assert sorted(p.name for p in clips.iterdir()) == ["1.mp4", "2.mp4"], "clips kept"
    assert "callback" in stopped and True in stopped


def test_low_disk_or_memory_pauses_heavy_stages_only(tmp_path):
    low = ["Only 1.0 GB free on disk (minimum 5 GB)."]
    s = make(tmp_path, health=lambda: low[0])
    s.submit("p0", "vod", [{"stage": "ingest"}, {"stage": "render"}])
    assert s.lease("ingest") is not None, "ingest is not heavy"
    assert s.status()["paused_reason"] is None


def test_low_disk_blocks_render(tmp_path):
    low = ["Only 1.0 GB free on disk (minimum 5 GB)."]
    s = make(tmp_path, health=lambda: low[0])
    s.submit("p0", "vod", [{"stage": "render"}])
    assert s.lease("render") is None and "GB free" in s.status()["paused_reason"]
    low[0] = None
    assert s.lease("render") is not None


def test_live_session_adds_deduplicated_tasks_and_stays_open(tmp_path):
    s = make(tmp_path)
    s.submit("live", "live", [], meta={"open": True})
    a = s.add_task("live", "render", {"clip": 1}, dedupe="live:clip:1")
    assert s.add_task("live", "render", {"clip": 1}, dedupe="live:clip:1") is None
    s.complete(s.lease("render")["id"])
    assert a and s.source("live")["state"] == "active", "session open until stopped"
    s.close_source("live")
    assert s.source("live")["state"] == "done"


def test_settings_are_validated_and_render_is_capped_at_two(tmp_path):
    assert scheduler.DEFAULTS["pools"]["render"] == 1
    with pytest.raises(SchedulerError):
        scheduler.validate_settings({"pools": {"render": 3}})
    with pytest.raises(SchedulerError):
        scheduler.validate_settings({"max_sources": 6})
    saved = scheduler.save_settings({"pools": {"render": 2}}, tmp_path / "sch.json")
    assert scheduler.load_settings(tmp_path / "sch.json")["pools"]["render"] == 2 == \
        saved["pools"]["render"]


def test_mixed_five_source_run_completes_with_threads(tmp_path):
    done = []
    lock = threading.Lock()

    def work(task, cancel, set_pid):
        time.sleep(0.02)
        with lock:
            done.append((task["source_id"], task["stage"]))
    handlers = {stage: work for stage in scheduler.STAGES}
    s = make(tmp_path, handlers=handlers)
    for i in range(6):
        s.submit(f"p{i}", "live" if i % 2 else "vod",
                 chain(("ingest", "analysis", "ai", "render"), gpu=True))
    s.start()
    try:
        deadline = time.time() + 20
        while len(done) < 24 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        s.stop()
    assert len(done) == 24 and len(set(done)) == 24, "every task once, none twice"
    assert all(s.source(f"p{i}")["state"] == "done" for i in range(6))


def test_health_probes_work_on_this_machine(tmp_path):
    assert scheduler.free_disk_gb(tmp_path) > 0
    memory = scheduler.free_memory_mb()
    assert memory is None or memory > 0
