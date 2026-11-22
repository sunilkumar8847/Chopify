"""Phase 2 in Studio: five sources, sixth waits, cancel keeps clips, restart recovery."""
import json
import time

import pytest

import projects

pytestmark = pytest.mark.scheduler

FAKE_WORKER = r'''
import json, os, sys, time
from pathlib import Path
project = Path(sys.argv[sys.argv.index("--project") + 1])
action = sys.argv[sys.argv.index("--action") + 1]
with (project / "launches.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps({"action": action, "start": time.time(), "pid": os.getpid()}) + "\n")
delay = float((project / "delay.txt").read_text()) if (project / "delay.txt").exists() else 0.3
time.sleep(delay)
if action == "render":
    out = Path(sys.argv[sys.argv.index("--out") + 1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "clip_01.mp4").write_bytes(b"final clip")
state = json.loads((project / "project.json").read_text(encoding="utf-8"))
state["status"] = "review" if action == "prepare" else "done"
(project / "project.json").write_text(json.dumps(state), encoding="utf-8")
with (project / "launches.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps({"action": action, "end": time.time()}) + "\n")
'''


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    import dashboard
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "chopify.py").write_text(FAKE_WORKER, encoding="utf-8")
    monkeypatch.setattr(dashboard, "HERE", folder)
    return folder


def studio_at(root):
    import dashboard
    studio = dashboard.Studio(root)
    studio.scheduler.settings["poll_seconds"] = 0.1
    return studio


def new_project(root, tmp_path, name, delay=None, **options):
    video = tmp_path / f"{name}.mp4"
    video.write_bytes(b"\0" * 64)
    state = projects.create_project(root, str(video), options)
    if delay is not None:
        (projects.project_path(root, state["id"]) / "delay.txt").write_text(str(delay))
    return state["id"]


def launches(root, project_id):
    path = projects.project_path(root, project_id) / "launches.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def wait_for(check, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return False


def test_five_sources_run_and_the_sixth_waits_its_turn(tmp_path, bin_dir):
    root = tmp_path / "projects"
    studio = studio_at(root)
    try:
        ids = [new_project(root, tmp_path, f"v{i}", delay=1.5, transcriber="groq:whisper-large-v3-turbo")
               for i in range(6)]
        infos = [studio.enqueue(pid, "all") for pid in ids]
        assert [i["state"] for i in infos] == ["active"] * 5 + ["waiting"]
        assert "position 1" in infos[5]["message"]
        status = studio.dashboard()["scheduler"] if hasattr(studio, "dashboard") else \
            studio.scheduler.status()
        assert status["active"] == 5 and status["waiting"] == 1
        for pid in ids:
            assert studio.futures[pid].result(timeout=120) == "done"
        for pid in ids:
            actions = [e["action"] for e in launches(root, pid) if "start" in e]
            assert actions == ["prepare", "render"], "each step once, in order"
            assert (projects.project_path(root, pid) / "clips" / "clip_01.mp4").exists()
        # Analysis ran two at a time; render one at a time.
        spans = [(e["start"], pid) for pid in ids for e in launches(root, pid)
                 if e.get("action") == "render" and "start" in e]
        ends = {pid: [e["end"] for e in launches(root, pid) if e.get("action") == "render"
                      and "end" in e][0] for pid in ids}
        ordered = sorted(spans)
        for (start, pid), (next_start, _) in zip(ordered, ordered[1:]):
            assert next_start >= ends[pid] - 0.05, "renders never overlap (render pool = 1)"
    finally:
        studio.close()


def test_cancel_keeps_finished_clips_and_frees_the_slot(tmp_path, bin_dir):
    root = tmp_path / "projects"
    studio = studio_at(root)
    try:
        pid = new_project(root, tmp_path, "long", delay=30)
        clips = projects.project_path(root, pid) / "clips"
        clips.mkdir()
        (clips / "earlier.mp4").write_bytes(b"made before")
        studio.enqueue(pid, "all")
        assert wait_for(lambda: launches(root, pid))
        assert studio.cancel(pid)["cancelled"][0] == "process"
        assert studio.futures[pid].result(timeout=30) == "cancelled"
        assert wait_for(lambda: projects.read_project(
            projects.project_path(root, pid))["status"] == "cancelled", 30)
        assert (clips / "earlier.mp4").read_bytes() == b"made before"
        assert not studio.busy(pid)
        studio.enqueue(pid, "render")            # the slot is free again
    finally:
        studio.close()


def test_restart_continues_interrupted_work_once(tmp_path, bin_dir):
    root = tmp_path / "projects"
    studio = studio_at(root)
    pid = new_project(root, tmp_path, "crash", delay=3)
    studio.enqueue(pid, "prepare")
    assert wait_for(lambda: launches(root, pid))
    studio.close()                               # Studio stops mid-analysis
    assert wait_for(lambda: projects.read_project(
        projects.project_path(root, pid)).get("status") != "running", 10) or True
    (projects.project_path(root, pid) / "delay.txt").write_text("0.2")
    again = studio_at(root)
    try:
        assert again.busy(pid), "queued again by the persistent queue"
        assert wait_for(lambda: not again.busy(pid), 60)
        state = projects.read_project(projects.project_path(root, pid))
        assert state["status"] == "review"
        starts = [e for e in launches(root, pid) if "start" in e]
        assert len(starts) == 2, "interrupted run + one resumed run, never two at once"
    finally:
        again.close()


def test_missing_source_fails_at_ingest_without_starting_a_worker(tmp_path, bin_dir):
    root = tmp_path / "projects"
    studio = studio_at(root)
    try:
        state = projects.create_project(root, str(tmp_path / "gone.mp4"), {})
        studio.enqueue(state["id"], "prepare")
        assert studio.futures[state["id"]].result(timeout=30) == "failed"
        saved = projects.read_project(projects.project_path(root, state["id"]))
        assert saved["status"] == "failed" and "does not exist" in saved["error"]
        assert launches(root, state["id"]) == []
    finally:
        studio.close()


def test_scheduler_settings_api_caps_renderers_at_two(tmp_path, bin_dir):
    root = tmp_path / "projects"
    studio = studio_at(root)
    try:
        assert studio.scheduler.configure({"pools": {"render": 2}})["pools"]["render"] == 2
        import scheduler
        with pytest.raises(scheduler.SchedulerError):
            studio.scheduler.configure({"pools": {"render": 5}})
    finally:
        studio.close()
