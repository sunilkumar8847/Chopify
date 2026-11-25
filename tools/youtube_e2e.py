"""End-to-end YouTube test against a real Studio process (isolated folders).

    python tools/youtube_e2e.py

Uses openly licensed / public-domain material only:
* NASA ScienceCasts (public domain, narrated) - standard watch URL: Find clips
  (download, Groq transcription + judging, selection) then Export (render).
* Another NASA ScienceCasts - youtu.be and /shorts/ links: Find clips.
* NASA ISS live stream (public domain) - live session for ~3 minutes.

AI routes come from your real AI settings (Groq Free); keys from .env.
Writes ``youtube_e2e.json`` in the run folder.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (HERE, api, isolated_env, keep_awake, kill_run, probe,  # noqa: E402
                     real_projects_snapshot, run_processes, start_studio)

STANDARD = "https://www.youtube.com/watch?v=RU-i523fflU"
SHORT_LINK = "https://youtu.be/LCJ78OY2jwc"
SHORTS = "https://www.youtube.com/shorts/LCJ78OY2jwc"
LIVE = "https://www.youtube.com/watch?v=ROboUHfPUrU"
DONE = {"review", "complete", "done", "failed", "cancelled"}


def wait_status(base, token, pid, timeout):
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        state = api(base, token, f"/api/projects/{pid}")[1].get("project", {})
        if state.get("status") in DONE:
            return state
        time.sleep(5)
    return state


def main():
    keep_awake(True)
    before = real_projects_snapshot()
    run = HERE.parent / "_phase9" / time.strftime("youtube-%Y%m%d-%H%M%S")
    (run / "config").mkdir(parents=True)
    shutil.copy(Path(os.environ["APPDATA"]) / "Chopify" / "ai_settings.json",
                run / "config" / "ai_settings.json")
    studio, base, token = start_studio(run, 8792, isolated_env(run))
    report = {"run": str(run), "results": {}}
    results = report["results"]

    def save():
        (run / "youtube_e2e.json").write_text(json.dumps(report, indent=1, default=str),
                                              encoding="utf-8")
    try:
        # 1) standard URL: full pipeline ----------------------------------
        started = time.time()
        status, body = api(base, token, "/api/batch", {
            "sources": [STANDARD], "options": {"min_score": 4, "max_clips": 2}})
        pid = body["projects"][0]["id"]
        state = wait_status(base, token, pid, 2400)
        prepared = time.time() - started
        folder = run / "projects" / pid
        transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8")) \
            if (folder / "transcript.json").exists() else {}
        segments = json.loads((folder / "segments.json").read_text(encoding="utf-8")) \
            if (folder / "segments.json").exists() else []
        results["standard"] = {
            "url": STANDARD, "batch_status": status, "status": state.get("status"),
            "error": state.get("error"), "prepare_seconds": round(prepared),
            "downloaded": sorted(p.name for p in folder.iterdir() if p.is_file()
                                 and p.suffix in (".webm", ".m4a", ".mp4", ".opus")),
            "transcript_words": len(transcript.get("words") or []),
            "transcript_language": transcript.get("language"),
            "transcript_source": transcript.get("transcript_source"),
            "candidates": len(segments),
            "judged": sum(1 for s in segments if (s.get("judge") or {}).get("verdict"))}
        save()
        if state.get("status") == "review" and segments:
            started = time.time()
            api(base, token, f"/api/projects/{pid}/run", {"action": "render"})
            state = wait_status(base, token, pid, 2400)
            clips = sorted((folder / "clips").glob("*.mp4"))
            results["standard"].update(
                render_status=state.get("status"), render_error=state.get("error"),
                render_seconds=round(time.time() - started),
                clips={c.name: probe(c) for c in clips})
        save()

        # 2) youtu.be and /shorts/ links ------------------------------------
        for name, url in (("youtu.be", SHORT_LINK), ("shorts", SHORTS)):
            started = time.time()
            status, body = api(base, token, "/api/batch", {
                "sources": [url], "options": {"min_score": 4}, "allow_duplicates": True})
            pid = body["projects"][0]["id"]
            state = wait_status(base, token, pid, 1800)
            folder = run / "projects" / pid
            transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8")) \
                if (folder / "transcript.json").exists() else {}
            results[name] = {"url": url, "batch_status": status, "status": state.get("status"),
                             "stage": state.get("stage"), "error": state.get("error"),
                             "seconds": round(time.time() - started),
                             "duration": transcript.get("duration"),
                             "transcript_words": len(transcript.get("words") or [])}
            save()

        # 3) YouTube live stream as a live session ---------------------------
        status, body = api(base, token, "/api/live/start", {"kind": "url", "source": LIVE,
                                                            "title": "NASA ISS"})
        results["live"] = {"url": LIVE, "start_status": status, "error": body.get("error")}
        if status == 201:
            live_id = body["project_id"]
            time.sleep(180)
            _, live = api(base, token, "/api/live")
            session = next(s for s in live["sessions"] if s["project_id"] == live_id)
            api(base, token, f"/api/live/{live_id}/stop", {}, timeout=120)
            segs = sorted((run / "projects" / live_id / "live" / "segments").glob("*.ts"))
            results["live"].update(
                state=session.get("state"), message=session.get("message"),
                segments=session.get("segments"), buffered_seconds=session.get(
                    "buffered_seconds"), reconnects=session.get("reconnects"),
                first_segment=probe(segs[0]) if segs else None)
        save()
    finally:
        kill_run(run, studio)
        keep_awake(False)
    results["no_processes_left"] = not run_processes(run)
    results["real_projects_untouched"] = real_projects_snapshot() == before
    save()
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
