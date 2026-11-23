"""Phase 9: kill Studio during ingestion, analysis and rendering; check recovery.

    python tools/recovery_test.py [--out D:/Clipping/Chopify/_phase9]

Isolated run folder and config. AI stays on this computer (local Whisper on
the GPU; the AI judge is switched off for this test) so the slow network
cannot blur the timing. Studio is killed with ``taskkill /F`` on the Studio
process only - exactly like a crash - and the test checks:

* its worker and ffmpeg children end with it (Windows Job Object);
* the restarted Studio re-queues only the interrupted work, once;
* the live session resumes, with no duplicate or half-written segments;
* finished clips survive, no clip is made twice, no partial files remain;
* the user's real projects folder is unchanged.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (HERE, api, isolated_env, keep_awake, kill_run, kill_studio_only,  # noqa: E402
                     obs_writer, probe, real_projects_snapshot, run_processes, speech,
                     start_studio, worker_count, workers)

sys.path.insert(0, str(HERE))
FINISHED = {"complete", "done", "review", "failed", "cancelled"}


def project(base, token, pid):
    return api(base, token, f"/api/projects/{pid}")[1].get("project") or {}


def wait(check, timeout, step=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(step)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE.parent / "_phase9"))
    ap.add_argument("--port", type=int, default=8797)
    args = ap.parse_args()
    keep_awake(True)
    before_real = real_projects_snapshot()
    run = Path(args.out) / time.strftime("recovery-%Y%m%d-%H%M%S")
    (run / "config").mkdir(parents=True)
    env = isolated_env(run, CHOPIFY_LLM_DISABLED=1, CHOPIFY_FEATURE_LIVE_CLIPPING=0)
    wav = speech(run / "speech.wav", seconds=420)
    video = run / "talk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=1280x720:rate=30", "-i", str(wav), "-t", "420", "-c:v",
                    "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest", str(video)],
                   check=True)
    (run / "obs").mkdir()
    writer = obs_writer(run / "obs", wav, 3600, 90, 0)
    sim = {writer.pid}                         # the simulated OBS is not Studio's child
    report = {"run": str(run), "steps": [], "checks": {}}
    checks = report["checks"]

    def note(**item):
        item["t"] = round(time.time(), 1)
        report["steps"].append(item)
        (run / "recovery.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        print(json.dumps(item), flush=True)

    studio, base, token = start_studio(run, args.port, env)
    try:
        time.sleep(3)
        _, live = api(base, token, "/api/live/start", {"kind": "obs_folder",
                                                       "source": str(run / "obs")})
        live_id = live["project_id"]
        _, vod = api(base, token, "/api/projects", {"source": str(video), "options": {
            "min_score": 0, "transcriber": "local", "max_clips": 3}})
        vod_id = vod["id"]
        api(base, token, f"/api/projects/{vod_id}/run", {"action": "all"})

        def live_segments():
            sessions = api(base, token, "/api/live")[1].get("sessions") or [{}]
            return sessions[0].get("segments", 0)

        # 1) ingestion + analysis -----------------------------------------
        got = wait(lambda: project(base, token, vod_id).get("stage") == "transcribe"
                   and workers(run) and live_segments() >= 3, 300)
        children = run_processes(run, sim)
        note(step="kill during ingestion + analysis", in_window=bool(got),
             vod_stage=project(base, token, vod_id).get("stage"),
             live_segments=live_segments(), children=[(p, n) for p, n, _ in children])
        kill_studio_only(studio)
        time.sleep(3)
        left = run_processes(run, sim)
        checks["children_end_with_studio_1"] = not left and bool(children)
        note(step="after kill 1", processes_left=[(p, n) for p, n, _ in left])
        studio, base, token = start_studio(run, args.port, env)
        requeued = wait(lambda: project(base, token, vod_id).get("status") in (
            "queued", "running") and worker_count(run) == 1, 120)
        _, listing = api(base, token, "/api/projects")
        sched = listing.get("scheduler") or {}
        checks["analysis_resumed_once"] = bool(requeued) and worker_count(run) <= 1
        checks["only_interrupted_sources"] = sorted(
            s["id"] for s in sched.get("sources", []) if s["state"] == "active") == sorted(
            [vod_id, live_id])
        resumed = wait(lambda: live_segments() > 0 and api(base, token, "/api/live")[1][
            "sessions"][0]["state"] == "active", 60)
        checks["live_resumed_1"] = bool(resumed)
        note(step="restarted 1", vod_status=project(base, token, vod_id).get("status"),
             workers=worker_count(run), sources=sched.get("sources"))

        # 2) rendering ------------------------------------------------------
        max_workers = 0

        def rendering():
            nonlocal max_workers
            max_workers = max(max_workers, worker_count(run))
            state = project(base, token, vod_id)
            return state.get("stage", "").startswith("render") and workers(run)
        got = wait(rendering, 900, 0.3)
        clips_dir = run / "projects" / vod_id / "clips"
        time.sleep(4)                           # let one clip finish if it can
        before = sorted(p.name for p in clips_dir.glob("*.mp4")) if clips_dir.is_dir() else []
        children = run_processes(run, sim)
        note(step="kill during rendering", in_window=bool(got),
             vod_stage=project(base, token, vod_id).get("stage"), clips_finished=before,
             children=[(p, n) for p, n, _ in children])
        kill_studio_only(studio)
        time.sleep(3)
        left = run_processes(run, sim)
        checks["children_end_with_studio_2"] = not left and bool(children)
        checks["max_workers_for_one_project"] = max_workers
        note(step="after kill 2", processes_left=[(p, n) for p, n, _ in left])
        studio, base, token = start_studio(run, args.port, env)
        finished = wait(lambda: project(base, token, vod_id).get("status") in FINISHED
                        and not workers(run), 1200, 2)
        state = project(base, token, vod_id)
        clips = sorted(clips_dir.glob("*.mp4"))
        numbers = [c.name.split("-")[0] for c in clips]
        probes = {c.name: probe(c) for c in clips}
        leftovers = [p.name for p in (run / "projects" / vod_id).rglob("*") if p.is_file() and (
            p.suffix in (".part", ".tmp") or p.name.startswith(("_caption-", "_crop-",
                                                                 "_tight-")))]
        checks["render_finished_after_restart"] = bool(finished) and state.get("status") in (
            "complete", "done", "review")
        checks["finished_clips_survived"] = all((clips_dir / n).exists() for n in before)
        checks["no_duplicate_clips"] = len(numbers) == len(set(numbers)) and bool(clips)
        checks["clips_playable"] = all(p and p["duration"] > 1 and p["height"] == 1920
                                       for p in probes.values())
        checks["no_partial_render_files"] = not leftovers

        # live ring after two crashes ----------------------------------------
        from live_ingest import Ring
        sessions = api(base, token, "/api/live")[1]["sessions"]
        ring = Ring(run / "projects" / live_id / "live")
        segs = ring.live_segments()
        seqs = [s["seq"] for s in segs]
        indexed = {s["file"] for s in segs}
        checks["live_resumed_2"] = sessions[0]["state"] == "active"
        checks["no_duplicate_segments"] = len(seqs) == len(set(seqs)) and bool(seqs)
        api(base, token, f"/api/live/{live_id}/stop", {}, timeout=120)
        unindexed = [p.name for p in ring.segments.glob("*.ts") if p.name not in indexed
                     and p.name not in {s["file"] for s in ring.live_segments()}]
        checks["no_partial_segments_after_stop"] = not unindexed
        note(step="final", vod_status=state.get("status"), vod_error=state.get("error"),
             clips=[c.name for c in clips], probes=probes, leftovers=leftovers,
             live_segments=len(seqs), reconnects=sessions[0].get("reconnects"),
             unindexed=unindexed)
    finally:
        writer.kill()
        kill_run(run, studio)
        keep_awake(False)
    checks["no_processes_left"] = not run_processes(run, sim)
    checks["real_projects_untouched"] = real_projects_snapshot() == before_real
    report["passed"] = all(v is True or (k == "max_workers_for_one_project" and v <= 1)
                           for k, v in checks.items())
    (run / "recovery.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({"checks": checks, "passed": report["passed"]}, indent=1))


if __name__ == "__main__":
    main()
