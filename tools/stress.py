"""Phase 9 five-source stress run against a real Studio process.

    python tools/stress.py --minutes 30 [--out D:/Clipping/Chopify/_phase9]

Isolated run folder, config and logs. AI is the deterministic *simulated*
provider (``CHOPIFY_AI_SIMULATE=1``, every call slow on purpose) so five
sources can run for a long time without uploading anything over the slow
connection; real Groq is checked separately (tools/groq_check.py).

Timeline
* 0 min: five simulated OBS recordings (854x480 30 fps, speech, a loud moment
  every ``--burst`` s) start as five live sessions; a sixth live session must
  be refused; a recorded video (VOD) must wait for a slot.
* 40 %: one live session stops -> the waiting VOD starts (mixed live + VOD).
* 60 %: a second VOD is submitted and cancelled during analysis.
* end: sessions stop; clips are checked; Studio is stopped; nothing of this
  run may still be running.

Every 15 s it records CPU, RAM, GPU memory/use, disk, network counters and
Studio's scheduler/live status into ``samples.jsonl``; ``summary.json`` has
the results and pass/fail checks.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (HERE, Pacer, api, isolated_env, keep_awake, kill_run, obs_writer,  # noqa: E402
                     probe, real_projects_snapshot, run_processes, speech, start_studio,
                     stats, system_sample)

SOURCES = 5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--out", default=str(HERE.parent / "_phase9"))
    ap.add_argument("--burst", type=int, default=150)
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--latency", type=float, default=8.0, help="simulated AI seconds per call")
    args = ap.parse_args()
    keep_awake(True)
    before_real = real_projects_snapshot()
    run = Path(args.out) / time.strftime("stress-%Y%m%d-%H%M%S")
    (run / "config").mkdir(parents=True)
    (run / "config" / "ai_settings.json").write_text(json.dumps({
        "providers": {"simulated": {"enabled": True, "models": []}},
        "routes": {"transcribe": {"provider": "simulated", "model": "whisper"},
                   "generate": {"provider": "simulated", "model": "judge"}}}), encoding="utf-8")
    env = isolated_env(run, CHOPIFY_AI_SIMULATE=1, CHOPIFY_AI_SIMULATE_LATENCY=args.latency)
    seconds = int(args.minutes * 60) + 300
    wav = speech(run / "speech.wav", seconds=90)
    vods = []
    for i in range(2):
        video = run / f"vod{i}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=size=1280x720:rate=30", "-stream_loop", "-1", "-i", str(wav),
                        "-t", "300", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                        str(video)], check=True)
        vods.append(video)
    studio, base, token = start_studio(run, args.port, env)
    writers, events = [], []
    samples_file = run / "samples.jsonl"
    rows, pacer = [], Pacer(15)
    checks = {}

    def event(name, **data):
        item = {"t": round(time.time(), 1), "event": name, **data}
        events.append(item)
        print(json.dumps(item), flush=True)
    started = time.time()
    try:
        for i in range(SOURCES + 1):
            folder = run / f"obs{i}"
            folder.mkdir()
            if i < SOURCES:
                writers.append(obs_writer(folder, wav, seconds, args.burst, i * 30))
        time.sleep(4)
        live_ids = []
        for i in range(SOURCES):
            status, body = api(base, token, "/api/live/start", {
                "kind": "obs_folder", "source": str(run / f"obs{i}"), "title": f"Cam {i}"})
            live_ids.append(body.get("project_id"))
            event("live_start", i=i, status=status, error=body.get("error"))
        status, body = api(base, token, "/api/live/start", {"kind": "obs_folder",
                                                           "source": str(run / f"obs{SOURCES}")})
        checks["sixth_live_refused"] = status == 400 and "slots are busy" in body.get("error", "")
        event("sixth_live", status=status, error=body.get("error"))
        _, vod_a = api(base, token, "/api/projects", {"source": str(vods[0]), "options": {
            "min_score": 0, "max_clips": 2}})
        status, body = api(base, token, f"/api/projects/{vod_a['id']}/run", {"action": "all"})
        _, listing = api(base, token, "/api/projects")
        sched = listing.get("scheduler") or {}
        checks["five_active_sixth_waits"] = sched.get("active") == 5 and sched.get("waiting") == 1
        event("vod_a_submitted", status=status, active=sched.get("active"),
              waiting=sched.get("waiting"))
        stop_at = started + args.minutes * 60 * 0.4
        cancel_at = started + args.minutes * 60 * 0.6
        stopped_one = cancel_done = False
        vod_b = None
        segment_history = {}
        while time.time() - started < args.minutes * 60:
            sample = system_sample(run)
            _, live = api(base, token, "/api/live")
            _, listing = api(base, token, "/api/projects")
            sample["scheduler"] = listing.get("scheduler")
            sample["live"] = [{"id": s["project_id"], "state": s["state"],
                               "segments": s.get("segments"), "newest": s.get("newest"),
                               "pins": s.get("pins"),
                               "candidates": [c["state"] for c in s.get("candidates", [])]}
                              for s in live.get("sessions", [])]
            for s in sample["live"]:
                segment_history.setdefault(s["id"], []).append((sample["t"], s["newest"] or 0,
                                                                s["state"]))
            rows.append(sample)
            with samples_file.open("a", encoding="utf-8") as out:
                out.write(json.dumps(sample) + "\n")
            if not stopped_one and time.time() >= stop_at:
                api(base, token, f"/api/live/{live_ids[-1]}/stop", {}, timeout=120)
                stopped_one = True
                event("live_stopped_to_free_a_slot", id=live_ids[-1])
            if stopped_one and vod_b is None and time.time() >= cancel_at:
                _, vod_b = api(base, token, "/api/projects", {"source": str(vods[1]),
                                                              "options": {"min_score": 0}})
                api(base, token, f"/api/projects/{vod_b['id']}/run", {"action": "prepare"})
                event("vod_b_submitted", id=vod_b["id"])
                # Cancel it while it is running (poll closely: analysis can be quick).
                deadline = time.time() + 600
                while time.time() < deadline:
                    state = api(base, token, f"/api/projects/{vod_b['id']}")[1].get(
                        "project", {})
                    if state.get("status") == "running":
                        status, body = api(base, token,
                                           f"/api/projects/{vod_b['id']}/cancel", {})
                        cancel_done = True
                        event("vod_b_cancel", status=status, stage=state.get("stage"),
                              body=body)
                        break
                    time.sleep(0.5)
                pacer.last = time.time()          # this wait is not a stall
            pacer.wait()
        _, live = api(base, token, "/api/live")
        for session in live.get("sessions", []):
            if session["state"] == "active":
                api(base, token, f"/api/live/{session['project_id']}/stop", {}, timeout=120)
        time.sleep(5)
        _, live = api(base, token, "/api/live")
        _, listing = api(base, token, "/api/projects")
        vod_a_state = api(base, token, f"/api/projects/{vod_a['id']}")[1].get("project", {})
        vod_b_state = api(base, token, f"/api/projects/{vod_b['id']}")[1].get(
            "project", {}) if vod_b else {}
    finally:
        for writer in writers:
            writer.kill()
        kill_run(run, studio)
        keep_awake(False)
    time.sleep(2)
    projects_dir = run / "projects"
    clips = sorted(projects_dir.rglob("clips/*.mp4"))
    probes = {str(c.relative_to(projects_dir)): probe(c) for c in clips}
    candidates = []
    for file in projects_dir.glob("*/live/candidates.json"):
        candidates += json.loads(file.read_text(encoding="utf-8"))
    latencies = sorted(c["latency_seconds"] for c in candidates if c.get("latency_seconds"))
    max_render = max(((r.get("scheduler") or {}).get("stages", {}).get("render", {})
                      .get("running", 0) for r in rows), default=0)
    stalls = []
    for pid, history in segment_history.items():
        active = [(t, n) for t, n, s in history if s == "active"]
        for (t0, n0), (t1, n1) in zip(active, active[1:]):
            if t1 - t0 < 60 and n1 <= n0 and t1 - started > 60:
                stalls.append({"session": pid, "at": round(t1 - started), "newest": n1})
    ai_delayed = sum(1 for r in rows for s in r["live"]
                     for c in s["candidates"] if c in ("ai_queued", "ai_waiting"))
    live_clips = [c["clip"] for c in candidates
                  if c.get("state") == "done" and c.get("clip") and Path(c["clip"]).exists()]
    checks.update({
        "candidates_generated": len(candidates) > 0,
        "rendered_live_clip": bool(live_clips),
        "clips_valid_shorts": all(p and p["height"] == 1920 and p["duration"] <= 120.5
                                  for p in probes.values()) and bool(probes),
        "render_within_limit": max_render <= 1,
        "ingestion_never_stalled": not stalls,
        "ingestion_continued_while_ai_delayed": ai_delayed > 0 and not stalls,
        "vod_a_ran_after_slot_freed": vod_a_state.get("status") in ("complete", "done", "review"),
        "vod_b_cancelled": vod_b_state.get("status") == "cancelled",
        "no_processes_left": not run_processes(run, {w.pid for w in writers}),
        "real_projects_untouched": real_projects_snapshot() == before_real,
        "no_sleep_or_stall_detected": not pacer.gaps,
    })
    net = [r for r in rows if r.get("net_sent_bytes") is not None]
    minutes = (rows[-1]["t"] - rows[0]["t"]) / 60 if len(rows) > 1 else 0
    summary = {
        "minutes_planned": args.minutes, "minutes_sampled": round(minutes, 1), "run": str(run),
        "ai": f"simulated, {args.latency}s per call", "events": events, "samples": len(rows),
        "cpu_pct": stats(rows, "cpu_pct"), "ram_free_mb": stats(rows, "ram_free_mb"),
        "ram_total_mb": rows[0].get("ram_total_mb") if rows else None,
        "python_ffmpeg_rss_mb": stats(rows, "python_ffmpeg_rss_mb"),
        "python_ffmpeg_count": stats(rows, "python_ffmpeg_count"),
        "vram_used_mb": stats(rows, "vram_used_mb"), "gpu_util_pct": stats(rows, "gpu_util_pct"),
        "disk_free_gb": stats(rows, "disk_free_gb"),
        "run_gb_end": round(rows[-1]["run_bytes"] / 1e9, 2) if rows else None,
        "net_sent_mb": round((net[-1]["net_sent_bytes"] - net[0]["net_sent_bytes"]) / 1e6, 1)
        if len(net) > 1 else None,
        "net_recv_mb": round((net[-1]["net_recv_bytes"] - net[0]["net_recv_bytes"]) / 1e6, 1)
        if len(net) > 1 else None,
        "max_render_running": max_render, "stalls": stalls, "sleep_gaps": pacer.gaps,
        "candidates": len(candidates),
        "candidate_states": {s: sum(1 for c in candidates if c["state"] == s)
                             for s in {c["state"] for c in candidates}},
        "clip_latency_seconds": {"min": latencies[0], "median": latencies[len(latencies) // 2],
                                 "max": latencies[-1]} if latencies else None,
        "clips": len(clips), "live_clips": len(live_clips), "probes": probes,
        "vod_a": {k: vod_a_state.get(k) for k in ("status", "stage", "error")},
        "vod_b": {k: vod_b_state.get(k) for k in ("status", "stage", "error")},
        "checks": checks, "passed": all(checks.values())}
    (run / "summary.json").write_text(json.dumps(summary, indent=1, default=str),
                                      encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("probes", "events")},
                     indent=1, default=str))


if __name__ == "__main__":
    main()
