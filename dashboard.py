"""Chopify Studio: loopback browser UI with a serial, resumable project queue."""
import argparse
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

import applog
import automation
import envconfig
import features
import scheduler as scheduler_mod
import compliance
import youtube_data
import download_and_transcribe as dt
import transcription
import branding
import ingest
import jobs
import live
import projects
import publish_meta
import storage
import render_clips
import youtube_auth
import youtube_upload

HERE = Path(__file__).resolve().parent
STATIC = HERE / "assets" / "studio"


def project_detail(root, project_id):
    """Build the JSON payload for one project, including selection.json."""
    path = projects.project_path(root, project_id)
    state = projects.read_project(path)
    data = {"project": state, "busy": False, "candidates": [],
            "transcript": None, "selection": {}, "files": [], "log": ""}
    for filename, key in [("segments.json", "candidates"),
                          ("transcript.json", "transcript"),
                          ("selection.json", "selection")]:
        if (path / filename).exists():
            data[key] = projects.read_json(path / filename)
    for directory in ("previews", "clips"):
        for file in sorted((path / directory).glob("*")):
            if file.suffix in {".mp4", ".srt", ".png", ".json"}:
                data["files"].append({"name": file.name, "kind": directory,
                                      "url": f"/media/{project_id}/{directory}/{file.name}"})
    if (path / "job.log").exists():
        with (path / "job.log").open("rb") as log:
            log.seek(max(0, (path / "job.log").stat().st_size - 12000))
            data["log"] = log.read().decode("utf-8", errors="replace")
    transcript = data.get("transcript") or {}
    source_meta = state.get("source_meta") or {}
    render_from_sections = bool(
        source_meta.get("render_from_sections")
        or transcript.get("render_from_sections"))
    video = transcript.get("video") or state.get("video")
    recording = state.get("recording") if isinstance(state.get("recording"), dict) else None
    source_url = (recording or {}).get("url") or state.get("source_url")
    data.update({
        "recording": recording,
        "recording_parts": len(live.part_groups(path)) if recording else 0,
        "source_kind": (ingest.describe_source(source_meta)
                        if source_meta and not recording else None),
        "can_record": bool(ingest.is_url(source_url or state.get("source", ""))
                           and (recording or {}).get("state") != "recorded"),
        "source_meta": source_meta,
        "progress": state.get("progress") or {},
        "diagnosis": automation.diagnose(state, data["log"][-4000:]),
        "pipeline": automation.pipeline(path, state),
        "activity": automation.activity(state, automation.pipeline(path, state)),
        "legacy_captions": projects.legacy_caption_clips(path),
        "render_errors": state.get("render_errors") or [],
        "playable_source": bool(
            not render_from_sections and video
            and Path(video).is_file()),
    })
    return data


def public_upload(entry):
    """Upload entry for the UI: no session URI (it can resume the upload)."""
    return {k: v for k, v in entry.items() if k not in {"session", "info"}}


def clip_info(path, clip):
    """What the metadata writer needs to know about one exported clip."""
    meta_file = clip.with_suffix(".meta.json")
    meta = projects.read_json(meta_file) if meta_file.is_file() else {}
    transcript = projects.read_json(path / "transcript.json") if (path / "transcript.json").is_file() else {}
    start, end = meta.get("start"), meta.get("end")
    words = transcript.get("words") or []
    text = " ".join(str(w.get("word", "")).strip() for w in words
                    if start is not None and end is not None
                    and float(w["start"]) >= float(start) - 0.05
                    and float(w["end"]) <= float(end) + 0.05)
    summary = ""
    if (path / "segments.json").is_file() and start is not None:
        for segment in projects.read_json(path / "segments.json"):
            if abs(float(segment.get("start", -1)) - float(start)) < 0.05:
                summary = (segment.get("judge") or {}).get("summary", "")
                break
    state = projects.read_project(path)
    hook = str(meta.get("title") or "").replace("#shorts", "").strip()
    return {"hook": hook, "text": text[:4000], "summary": summary,
            "language": transcript.get("language", ""),
            "source_title": (state.get("source_meta") or {}).get("title", ""),
            "start": start, "end": end}


def youtube_metadata(source, client):
    """Check source for a YouTube URL: official videos.list only.

    Never downloads anything and never falls back to yt-dlp or scraping.
    """
    video_id = automation.youtube_id(source)
    base = {"source": source, "kind": "youtube", "blocked": not compliance.youtube_downloads_allowed(),
            "can_record": False, "probe_error": None, "metadata_source": "youtube_data_api",
            "title": None, "duration": None, "live_status": None, "release_timestamp": None}
    if not video_id:
        return {**base, "label": "Not a YouTube video link",
                "message": "Check source looks up single videos (watch, youtu.be, shorts or "
                           "live links). Channel and playlist links are not checked."}
    if client is None:
        raise ValueError("The official YouTube API is not available in this mode.")
    with applog.step("source.validate", f"Check source {video_id} (YouTube Data API)"):
        try:
            info = client.video(video_id)
        except youtube_data.DataApiError as exc:
            raise ValueError(f"Check source could not ask YouTube: {exc}") from exc
    if info is None:
        return {**base, "label": "Video not found",
                "message": f"YouTube has no public video with the ID {video_id}. It may be "
                           "private, removed or mistyped."}
    bits = [info["channel_title"]]
    if info["duration"]:
        total = int(info["duration"])
        bits.append(f"{total // 3600}:{total % 3600 // 60:02}:{total % 60:02}" if total >= 3600
                    else f"{total // 60}:{total % 60:02}")
    if info["published_at"]:
        bits.append(f"published {info['published_at'][:10]}")
    if info["view_count"] is not None:
        bits.append(f"{info['view_count']:,} views")
    label = {"is_live": "Live now on YouTube", "is_upcoming": "Upcoming YouTube stream",
             "was_live": "Finished YouTube live stream"}.get(info["live_status"], "YouTube video")
    note = (" This is public metadata from the official Data API only. Downloading or "
            "recording YouTube audiovisual content is disabled pending written approval, "
            "so Find clips cannot use this link. To clip your own video, download it from "
            "YouTube Studio and add the file, or use your OBS recording.") \
        if base["blocked"] else ""
    return {**base, "label": label, "title": info["title"],
            "channel": info["channel_title"], "channel_id": info["channel_id"],
            "duration": info["duration"], "live_status": info["live_status"],
            "privacy_status": info["privacy_status"], "url": info["url"],
            "message": " · ".join(b for b in bits if b) + "." + note}


def detect_source(source, youtube=None):
    """Classify a local path or URL (normal video, live, upcoming, ...)."""
    if not isinstance(source, str) or not source.strip() or len(source) > 8000:
        raise ValueError("Provide a video path or URL.")
    source = source.strip()
    if not ingest.is_url(source) and not Path(source).expanduser().is_file():
        raise ValueError(f"Video file does not exist or URL is invalid: {source}")
    if compliance.blocked(source):
        # Downloads are off (the default): official API metadata only.
        return youtube_metadata(source, youtube)
    with applog.step("source.validate", f"Check source {source}"):
        meta = ingest.probe_source(source)
        info = ingest.describe_source(meta)
    if meta.get("probe_error"):
        applog.warning("source.validate", "yt-dlp could not read the URL",
                       error=meta["probe_error"])
    return {**info, "source": source, "title": meta.get("title"),
            "probe_error": meta.get("probe_error"),
            "duration": meta.get("duration"),
            "live_status": meta.get("live_status"),
            "release_timestamp": meta.get("release_timestamp")}


def source_problem(source):
    """Why a source cannot be processed, or None."""
    if not isinstance(source, str) or not source.strip():
        return "Empty source line."
    source = source.strip()
    if ingest.is_url(source):
        return compliance.MESSAGE if compliance.blocked(source) else None
    if re.match(r"^[a-z][a-z0-9+.-]*://", source, re.I) or source.lower().startswith("www."):
        return f"Not a usable URL (use a full http(s) link): {source}"
    if not Path(source).expanduser().is_file():
        return f"Video file does not exist: {source}"
    return None


def error_from_log(log_path, code):
    """A readable reason from a failed worker's own output."""
    try:
        with open(log_path, "rb") as handle:
            handle.seek(max(0, Path(log_path).stat().st_size - 6000))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    run = []
    for line in reversed(lines):
        if line.startswith("===== "):
            break
        run.insert(0, line)
    meaningful = [l.strip() for l in run if l.strip()]
    for line in reversed(meaningful):
        if re.search(r"(?i)error|failed|exception|not found|traceback|refused|denied", line) \
                and not line.startswith("PREFLIGHT OK"):
            return f"{line[:400]} (exit code {code})"
    if meaningful:
        return f"{meaningful[-1][:400]} (exit code {code})"
    return f"The worker exited with code {code} before writing any output."


class Studio:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "brands").mkdir(exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self._gpu = None
        # Separate lanes: recording, processing (one at a time) and uploads.
        self.jobs = jobs.JobManager()
        self.lock = threading.RLock()
        self.futures = {}          # project_id -> latest processing job future
        self.processes = set()
        self.children = {}         # project_id -> running chopify.py child
        self.cancelled = set()     # project_ids whose processing was cancelled
        self.recorders = {}        # project_id -> (stop_event, abort_event)
        self.closing = False
        # Phase 5: YouTube. Uploads run only for clips the user approved.
        self.youtube = youtube_auth.YouTubeAccount()
        self.uploads = youtube_upload.UploadQueue(self.youtube)
        self.upload_timer = None
        self.storage_timer = None
        # Phase 6: automation settings, event history, channel monitoring.
        self.settings = automation.Settings(self.root)
        self.events = automation.EventLog(self.root)
        self.monitor = automation.Monitor(self.settings)
        self.monitor.official = self.youtube_data()
        self.monitor_timer = None
        self.monitor_busy = threading.Lock()
        self.recover = []          # processing jobs interrupted by a restart
        if self.settings.get()["processing_paused"]:
            self.jobs.pause("process")
        # Phase 2: persistent five-source scheduler (feature flag "scheduler").
        self.scheduler = None
        if features.enabled("scheduler"):
            self.scheduler = scheduler_mod.Scheduler(
                self.root / ".scheduler.db", settings=scheduler_mod.load_settings(),
                handlers={"ingest": self._task_ingest, "analysis": self._task_process,
                          "render": self._task_process, "ai": self._task_ai},
                disk_path=self.root, paused=self._stage_paused, on_finish=self._source_finished)
        self.ai_jobs = {}          # Phase 6: ai task kinds -> callable(payload, cancel)
        resumable = set()
        if self.scheduler is not None:
            resumable = {s["id"] for s in self.scheduler.status()["sources"]
                         if s["state"] in ("active", "waiting")}
            if resumable and not self.settings.get()["resume_after_restart"]:
                for project_id in resumable:
                    self.scheduler.cancel(project_id)
                    self.events.add("recovery", "Studio restarted; press Retry to "
                                    "continue this project.", project_id)
                resumable = set()
        for path in self.root.glob("*/project.json"):
            try:
                state = projects.read_project(path.parent)
                changed = live.mark_interrupted(state)
                if path.parent.name in resumable:
                    # The persistent queue continues this project by itself.
                    if state["status"] in {"queued", "running"}:
                        state.update(status="queued", error=None,
                                     stage="continuing after restart")
                        projects.save_project(path.parent, state)
                    continue
                if state["status"] in {"queued", "running"} and state.get("last_action"):
                    self.recover.append(path.parent.name)
                if state["status"] in {"queued", "running", "recording"}:
                    state.update(status="interrupted", error="Studio stopped. Resume this project to continue.")
                    changed = True
                if changed:
                    projects.save_project(path.parent, state)
            except (ValueError, OSError, KeyError):
                continue
        if self.scheduler is not None:
            self.scheduler.start()

    def busy(self, project_id):
        return self.jobs.busy(project_id) or bool(
            self.scheduler is not None and self.scheduler.busy(project_id))

    # ---------------------------------------------- Phase 2 scheduler tasks
    def _stage_paused(self, stage):
        if stage in ("analysis", "render") and self.jobs.paused("process"):
            return "Processing is paused."
        return None

    def _source_finished(self, project_id, state):
        future = self.futures.get(project_id)
        if future is not None and hasattr(future, "set_result") and not future.done():
            future.set_result(state)

    def _task_ingest(self, task, cancel, set_pid):
        """Recorded videos: make sure the source is there before analysis."""
        payload = task["payload"] or {}
        path = projects.project_path(self.root, task["source_id"])
        state = projects.read_project(path)
        source = state["source"]
        if not ingest.is_url(source) and not Path(source).expanduser().is_file():
            error = f"Video file does not exist: {source}"
            projects.update_project(path, lambda s: s.update(status="failed", error=error))
            raise scheduler_mod.SchedulerError(error)
        if payload.get("session"):
            handler = self.ai_jobs.get("ingest:" + payload["session"])
            if handler:
                return handler(payload, cancel)
        return {"source": "ok"}

    def _task_process(self, task, cancel, set_pid):
        """Analysis (prepare) and render run the existing worker, one child each."""
        payload = task["payload"] or {}
        project_id = task["source_id"]
        path = projects.project_path(self.root, project_id)
        self.scheduler.on_cancel(project_id, lambda: self._cancel_process(project_id))
        code = self._run(path, payload.get("action", "prepare"), bool(payload.get("preview")),
                         payload.get("pick"), on_child=lambda child: set_pid(child.pid))
        if self.closing:
            raise RuntimeError("Studio is closing.")
        if project_id in self.cancelled or cancel.is_set():
            raise scheduler_mod.Cancelled()
        if code:
            error = projects.read_project(path).get("error") or f"Worker exited with {code}."
            exc = RuntimeError(error)
            exc.retryable = False          # the same input fails the same way
            raise exc
        return {"code": 0}

    def _task_ai(self, task, cancel, set_pid):
        payload = task["payload"] or {}
        handler = self.ai_jobs.get(payload.get("kind"))
        if handler is None:
            raise scheduler_mod.SchedulerError(f"Unknown AI task {payload.get('kind')}.")
        return handler(payload, cancel)

    def _enqueue_scheduled(self, project_id, path, state, action, preview, pick):
        options = state.get("options") or {}
        gpu = ["gpu"] if (options.get("transcriber") or "local") == "local" else []
        tasks = []
        if action in ("prepare", "all"):
            tasks += [{"stage": "ingest", "payload": {"check": True}},
                      {"stage": "analysis", "payload": {"action": "prepare"}, "resources": gpu}]
        if action in ("all", "render"):
            tasks.append({"stage": "render", "payload": {"action": "render", "preview": preview,
                                                         "pick": pick}})
        kind = "vod"                   # live sessions are submitted by live ingestion
        future = Future()
        self.futures[project_id] = future
        info = self.scheduler.submit(project_id, kind, tasks, label=state.get("title"))
        if info["state"] == "waiting":
            projects.update_project(path, lambda s: s.update(stage=info["message"]))
        return info

    # ------------------------------------------------------- automation
    def start_automation(self):
        """After startup: resume interrupted work and arm the monitor."""
        settings = self.settings.get()
        if self.recover:
            for project_id in list(self.recover):
                if not settings["resume_after_restart"]:
                    self.events.add("recovery", "Studio restarted; press Retry to "
                                    "continue this project.", project_id)
                    continue
                try:
                    state = projects.read_project(projects.project_path(self.root, project_id))
                    self.enqueue(project_id, state.get("last_action") or "prepare",
                                 bool(state.get("last_preview")), state.get("last_pick"),
                                 reason="resumed after restart")
                except (ValueError, OSError, KeyError) as exc:
                    self.events.add("recovery", f"Could not resume: {exc}",
                                    project_id, level="error")
            self.recover = []
        self._schedule_monitor()
        self._schedule_storage(30)       # also resumes interrupted cleanups

    def find_duplicate(self, source):
        """The existing project for this video, or None."""
        key = automation.source_key(source)
        for state in self.list_projects():
            if state.get("status") == "cancelled" and not state.get("cache"):
                continue
            if key in automation.project_source_keys(state):
                return state
        return None

    def create_sources(self, sources, options, allow_duplicates=False, origin="manual",
                       errors=None):
        """Create and queue projects; the same video is never added twice.

        A source that cannot be used is reported in ``errors`` (when given a
        list) instead of aborting the batch. A project whose job cannot be
        queued is marked failed with the reason, never left waiting.
        """
        created, duplicates = [], []
        seen = set()
        for source in sources:
            with applog.step("source.validate", f"Validate {source}"):
                problem = source_problem(source)
            if problem:
                applog.warning("source.validate", problem, source=source)
                if errors is None:
                    raise ValueError(problem)
                errors.append({"source": source, "error": problem})
                continue
            key = automation.source_key(source)
            existing = None if allow_duplicates else self.find_duplicate(source)
            if existing is not None or key in seen:
                duplicates.append({"source": source,
                                   "project_id": existing["id"] if existing else None})
                applog.info("source.duplicate", "Source already has a project",
                            source=source, existing=existing["id"] if existing else None)
                continue
            seen.add(key)
            with applog.step("project.create", f"Create project for {source}"):
                state = projects.create_project(self.root, source, options)
            with applog.bound(project_id=state["id"]):
                self.events.add("created", f"New project from {origin}: {source}", state["id"])
                try:
                    self.enqueue(state["id"], "prepare")
                except Exception as exc:  # noqa: BLE001 - reported, project not stuck
                    message = f"Could not start processing: {exc}"
                    applog.error("job.submit", message, exc=exc)
                    projects.update_project(
                        projects.project_path(self.root, state["id"]),
                        lambda s: s.update(status="failed", stage="queue", error=message))
                    self.events.add("failed", message, state["id"], level="error",
                                    advice="Press Retry; if it fails again, restart Studio.")
                    if errors is not None:
                        errors.append({"source": source, "error": message,
                                       "project_id": state["id"]})
                    state = projects.read_project(
                        projects.project_path(self.root, state["id"]))
            created.append(state)
        if duplicates:
            self.events.add("duplicate", f"Skipped {len(duplicates)} source(s) "
                            "that already have a project.", level="warning")
        return created, duplicates

    def set_paused(self, target, paused):
        if target not in ("processing", "uploads"):
            raise ValueError("Pause target must be processing or uploads.")
        key = f"{target}_paused"
        self.settings.update({key: bool(paused)})
        if target == "processing":
            (self.jobs.pause if paused else self.jobs.resume)("process")
        else:
            if not paused:
                self.kick_uploads()
        self.events.add("pause", f"{target.capitalize()} {'paused' if paused else 'resumed'}.")
        return self.settings.get()

    def retry(self, project_id):
        state = projects.read_project(projects.project_path(self.root, project_id))
        if state.get("status") not in ("failed", "interrupted", "cancelled"):
            raise ValueError("Only failed, interrupted or cancelled projects can be retried.")
        rec = state.get("recording")
        if isinstance(rec, dict) and rec.get("state") not in (None, "recorded"):
            raise ValueError("This recording is not finalized. Use Resume recording "
                             "or Finalize recording in the project.")
        self.enqueue(project_id, state.get("last_action") or "prepare",
                     bool(state.get("last_preview")), state.get("last_pick"),
                     reason="retry")
        return {"ok": True}

    def youtube_data(self):
        """Read-only official Data API client (quota counted in the ledger)."""
        return youtube_data.DataClient(self.youtube, record_usage=self.uploads.record_api_call)

    def gpu_status(self):
        """Whether Whisper can use the GPU here (checked once per Studio run)."""
        if self._gpu is None:
            usable, reason = dt.cuda_status()
            self._gpu = {"usable": usable, "reason": reason}
            applog.info("studio.gpu", f"GPU transcription {'available' if usable else 'unavailable'}: {reason}")
        return self._gpu

    def overview(self):
        """Everything the automation dashboard shows, in one payload."""
        uploads = self.uploads.entries()
        by_project = {}
        for entry in uploads:
            by_project.setdefault(entry["project_id"], []).append(entry)
        rows = []
        for state in self.list_projects():
            path = projects.project_path(self.root, state["id"])
            log_tail = ""
            if state.get("status") in ("failed", "interrupted", "cancelled"):
                try:
                    with (path / "job.log").open("rb") as handle:
                        handle.seek(max(0, (path / "job.log").stat().st_size - 4000))
                        log_tail = handle.read().decode("utf-8", errors="replace")
                except OSError:
                    pass
            project_uploads = by_project.get(state["id"], [])
            meta = state.get("source_meta") or {}
            rows.append({
                "id": state["id"], "source": state.get("source"),
                "title": meta.get("title") or Path(str(state.get("source") or "")).name,
                "status": state.get("status"), "stage": state.get("stage"),
                "error": state.get("error"), "created": state.get("created"),
                "updated": state.get("updated"), "busy": self.busy(state["id"]),
                "pipeline": automation.pipeline(path, state, project_uploads),
                "progress": automation.progress(state),
                "diagnosis": automation.diagnose(state, log_tail),
                "clips": len(list((path / "clips").glob("*.mp4"))) if (path / "clips").is_dir() else 0,
                "legacy_captions": len(projects.legacy_caption_clips(path)),
                "uploads": {s: sum(1 for u in project_uploads if u["state"] == s)
                            for s in {u["state"] for u in project_uploads}},
                "recording": (state.get("recording") or {}).get("state"),
            })
        jobs = self.jobs.list()
        active = [j for j in jobs if j["state"] in ("queued", "running")]
        recent = [j for j in jobs if j["state"] not in ("queued", "running")][-15:]
        upload_counts = {}
        for entry in uploads:
            upload_counts[entry["state"]] = upload_counts.get(entry["state"], 0) + 1
        settings = self.settings.get()
        return {
            "projects": rows,
            "scheduler": self.scheduler.status() if self.scheduler is not None else None,
            "jobs": {"active": active, "recent": list(reversed(recent)),
                     "paused": {"process": self.jobs.paused("process"),
                                "uploads": settings["uploads_paused"]}},
            "uploads": {"counts": upload_counts,
                        "active": [public_upload(e) for e in uploads
                                   if e["state"] in ("uploading", "queued", "quota_wait", "failed")],
                        "usage": self.uploads.usage()},
            "settings": settings,
            "usage": self.settings.usage(),
            "channels": [{k: c.get(k) for k in ("id", "url", "label", "added", "baseline",
                                                 "last_check", "last_error", "found")}
                         for c in self.settings.channels()],
            "monitor_next": getattr(self, "monitor_next", None),
            "events": self.events.tail(60),
        }

    # ------------------------------------------------------ monitoring
    def _schedule_monitor(self):
        with self.lock:
            if self.monitor_timer is not None:
                self.monitor_timer.cancel()
                self.monitor_timer = None
            settings = self.settings.get()
            self.monitor_next = None
            if self.closing or not settings["monitor_enabled"] or not self.settings.channels():
                return
            if not compliance.youtube_downloads_allowed() and not any(
                    c.get("own") and not c.get("inactive") for c in self.settings.channels()):
                return              # only your own channel is monitored (official API)
            delay = settings["monitor_interval_minutes"] * 60
            self.monitor_next = time.time() + delay
            self.monitor_timer = threading.Timer(delay, self._monitor_tick)
            self.monitor_timer.daemon = True
            self.monitor_timer.start()

    def _monitor_tick(self):
        try:
            self.check_channels()
        finally:
            self._schedule_monitor()

    def check_channels(self):
        """One monitoring pass. New videos become processing jobs and live
        streams become recordings, within the daily limit. Nothing is ever
        published: uploads still need the user's approval clip by clip."""
        if not self.monitor_busy.acquire(blocking=False):
            return {"busy": True}
        try:
            settings = self.settings.get()

            def handle(kind, entry, channel):
                if self.find_duplicate(entry["url"]) is not None:
                    return True
                if not compliance.youtube_downloads_allowed():
                    # Metadata only: report it with a link; never download.
                    if kind == "upcoming":
                        return False             # report it when it goes live
                    what = "is live" if kind == "live" else "has a new upload"
                    self.events.add("monitor", f"Your channel {what}: {entry['url']} "
                                    "(metadata only; YouTube downloads are off, so add a "
                                    "local copy or your OBS recording to clip it).")
                    return True
                if kind == "upcoming":
                    return False                 # look again when it is live
                if kind == "live" and not settings["record_live"]:
                    return True
                if not self.settings.take_source_slot():
                    self.events.add("limit", "Daily limit for new sources reached; "
                                    f"{entry['url']} waits for tomorrow.", level="warning")
                    return False
                options = projects.validate_options(dict(settings["new_project_options"]))
                label = channel.get("label") or channel["url"]
                if kind == "live":
                    after = settings["new_source_action"]
                    state = live.start_recording_project(
                        self.root, entry["url"], {"after": after}, options)
                    self.start_recording(state["id"])
                    self.events.add("monitor", f"{label}: recording live stream "
                                    f"\"{entry.get('title') or entry['url']}\"", state["id"])
                    return True
                state = projects.create_project(self.root, entry["url"], options)
                self.events.add("monitor", f"{label}: new video "
                                f"\"{entry.get('title') or entry['url']}\"", state["id"])
                if settings["new_source_action"] != "none":
                    self.enqueue(state["id"], settings["new_source_action"],
                                 reason="channel monitor")
                return True

            results = self.monitor.check(handle)
            for result in results:
                if result.get("error"):
                    self.events.add("monitor", f"Channel check failed: {result['error']}",
                                    level="error")
            return {"results": results}
        finally:
            self.monitor_busy.release()

    # ---------------------------------------------------------- storage
    STORAGE_RECHECK = 15 * 60        # YouTube processing usually takes minutes

    def storage_plan(self, project_id, verify=False):
        """Cleanup plan for one project; ``verify`` asks YouTube first."""
        path = projects.project_path(self.root, project_id)
        state = projects.read_project(path)
        entries = self.uploads.entries()
        verify_error = None
        if verify:
            todo = [e["id"] for e in storage.clip_uploads(path, project_id, entries).values()
                    if e.get("state") == "uploaded" and e.get("youtube_status") != "processed"]
            if todo:
                try:
                    self.uploads.verify_uploaded(todo)
                except (youtube_upload.UploadError, youtube_auth.AuthError) as exc:
                    verify_error = str(exc)
                entries = self.uploads.entries()
        result = storage.plan(path, state, entries, busy=self.busy(project_id))
        if verify_error:
            result["eligible"] = False
            result["reasons"].append(f"upload status is uncertain: {verify_error}")
        result.update(title=(state.get("source_meta") or {}).get("title") or state.get("source"),
                      status=state.get("status"), last_cleanup=storage.last_cleanup(path),
                      auto_cleanup=bool((state.get("storage") or {}).get("auto_cleanup")))
        return result

    def storage_overview(self):
        """Disk use by category and per project (no YouTube calls)."""
        rows, totals = [], {c: 0 for c in storage.CATEGORIES}
        for state in self.list_projects():
            try:
                plan = self.storage_plan(state["id"])
            except (OSError, ValueError, KeyError):
                continue
            for key, value in plan["totals"].items():
                totals[key] += value
            row = {k: plan[k] for k in (
                "project_id", "title", "status", "eligible", "reasons", "bytes",
                "totals", "total_bytes", "clips", "last_cleanup", "auto_cleanup", "outside")}
            row.update(delete_count=len(plan["delete"]), keep_count=len(plan["keep"]))
            rows.append(row)
        logs = applog.log_dir()
        log_bytes = (sum(p.stat().st_size for p in logs.glob("*") if p.is_file())
                     if logs.is_dir() else 0)
        try:
            free = shutil.disk_usage(self.root).free
        except OSError:
            free = None
        return {"projects": rows, "totals": totals, "labels": storage.LABELS,
                "logs_bytes": log_bytes, "disk_free": free,
                "chopify_bytes": sum(totals.values()) + log_bytes,
                "reclaimable_bytes": sum(r["bytes"] for r in rows if r["eligible"]),
                "eligible": [r["project_id"] for r in rows if r["eligible"]]}

    def cleanup_project(self, project_id, reason="manual"):
        """Verify uploads with YouTube, then delete supporting files."""
        with self.lock:
            path = projects.project_path(self.root, project_id)
            if storage.pending(path):              # resume an interrupted run
                summary = storage.run_cleanup(path, {}, reason="resumed")
            else:
                plan = self.storage_plan(project_id, verify=True)
                if not plan["eligible"]:
                    applog.info("storage.cleanup", "Cleanup skipped: not eligible",
                                project_id=project_id, reason=reason,
                                reasons=plan["reasons"][:10])
                    return {"project_id": project_id, "cleaned": False,
                            "reasons": plan["reasons"]}
                summary = storage.run_cleanup(path, plan, reason=reason)

            def record(s):
                s.setdefault("storage", {}).update(
                    cleaned_at=summary["finished"],
                    files_deleted=summary["total_files_deleted"],
                    bytes_freed=summary["total_bytes_freed"])
            projects.update_project(path, record)
            self.events.add("storage", f"Cleaned up {summary['files_deleted']} supporting "
                            f"files ({summary['bytes_freed'] / 1024**2:.1f} MB); "
                            "final clips kept.", project_id)
            return {"project_id": project_id, "cleaned": True, **summary}

    def cleanup_all(self, reason="manual"):
        results = []
        for row in self.storage_overview()["projects"]:
            path = projects.project_path(self.root, row["project_id"])
            if row["eligible"] or storage.pending(path):
                results.append(self.cleanup_project(row["project_id"], reason))
        cleaned = [r for r in results if r.get("cleaned")]
        return {"results": results, "projects_cleaned": len(cleaned),
                "files_deleted": sum(r["files_deleted"] for r in cleaned),
                "bytes_freed": sum(r["bytes_freed"] for r in cleaned)}

    def auto_cleanup(self):
        """Clean new projects whose clips are all uploaded and processed.

        Only projects created with automatic cleanup on (new projects; never
        existing ones, which need explicit approval). Waits while an upload
        is unverified or YouTube is still processing it.
        """
        waiting = False
        for state in self.list_projects():
            path = projects.project_path(self.root, state["id"])
            if storage.pending(path):
                self.cleanup_project(state["id"], "resumed")
                continue
            info = state.get("storage") or {}
            if not info.get("auto_cleanup") or info.get("cleaned_at"):
                continue
            entries = storage.clip_uploads(path, state["id"], self.uploads.entries())
            if not entries or not all(e.get("state") == "uploaded" for e in entries.values()):
                continue
            result = self.cleanup_project(state["id"], "automatic after verified upload")
            if not result["cleaned"]:
                waiting = True
        if waiting:
            self._schedule_storage()
        return waiting

    def _schedule_storage(self, delay=None):
        with self.lock:
            if self.closing:
                return
            if self.storage_timer is not None:
                self.storage_timer.cancel()
            self.storage_timer = threading.Timer(delay or self.STORAGE_RECHECK,
                                                 self._storage_tick)
            self.storage_timer.daemon = True
            self.storage_timer.start()

    def _storage_tick(self):
        try:
            self.auto_cleanup()
        except Exception as exc:                 # never kill the timer thread
            applog.error("storage.cleanup", "Automatic cleanup check failed", exc=exc)
            self._schedule_storage()

    def delete_clips(self, project_id, names, confirm=False, confirmed=None):
        with self.lock:
            if self.busy(project_id):
                raise ValueError("Wait for this project's current job to finish.")
            path = projects.project_path(self.root, project_id)
            result = storage.delete_clips(path, project_id, names, self.uploads.entries(),
                                          confirm=confirm, confirmed_files=confirmed)
            if result.get("deleted"):
                self.events.add("storage", f"Deleted {len(names)} final clip(s) "
                                f"({result['bytes_freed'] / 1024**2:.1f} MB) on request.",
                                project_id)
            return result

    # ---------------------------------------------------------- YouTube
    UPLOAD_JOB = "youtube-uploads"

    def kick_uploads(self):
        """Process approved uploads on the upload lane (one at a time)."""
        with self.lock:
            if self.closing or self.jobs.busy(self.UPLOAD_JOB, "upload"):
                return
            if self.settings.get()["uploads_paused"]:
                return              # resumed from the dashboard
            if not self.uploads.due():
                self._schedule_uploads()
                return
            self.jobs.submit("upload", self.UPLOAD_JOB, self._run_uploads, kind="upload")

    def _run_uploads(self):
        try:
            self.uploads.process()
        finally:
            self._schedule_uploads()
            # A finished upload can make its project eligible once YouTube
            # has processed it: check soon, then every 15 minutes.
            self._schedule_storage(60)

    def _schedule_uploads(self):
        wake = self.uploads.next_wakeup()
        with self.lock:
            if self.upload_timer is not None:
                self.upload_timer.cancel()
                self.upload_timer = None
            if wake is None or self.closing:
                return
            delay = max(5.0, min(wake - time.time(), 6 * 3600))
            self.upload_timer = threading.Timer(delay, self.kick_uploads)
            self.upload_timer.daemon = True
            self.upload_timer.start()

    def youtube_state(self, project_id=None):
        entries = [public_upload(e) for e in self.uploads.entries()
                   if project_id is None or e["project_id"] == project_id]
        return {"account": self.youtube.status(), "settings": self.uploads.settings(),
                "usage": self.uploads.usage(), "uploads": entries,
                "categories": publish_meta.CATEGORIES,
                "worker_busy": self.jobs.busy(self.UPLOAD_JOB, "upload")}

    def publish_draft(self, project_id, clip_name, regenerate=False):
        """Upload entry (with AI metadata) for one exported clip. Uploads nothing."""
        path = projects.project_path(self.root, project_id)
        clip = (path / "clips" / str(clip_name)).resolve()
        if clip.parent != (path / "clips").resolve() or clip.suffix != ".mp4" or not clip.is_file():
            raise ValueError("Choose an exported clip (not a draft preview).")
        info = clip_info(path, clip)
        state = projects.read_project(path)
        settings = self.uploads.settings()
        existing = [e for e in self.uploads.entries()
                    if e["file"] == str(clip) and e["state"] != "cancelled"]
        if existing and not regenerate and existing[0]["state"] not in ("uploading", "uploaded"):
            meta = existing[0]["metadata"]
            outdated = meta.get("policy") not in (None, publish_meta.POLICY)
            # Drafted before the current language policy (an unedited AI draft
            # still carries its old policy tag; a hand-edited one does not) or
            # still containing Devanagari: draft it again. Uploaded videos and
            # uploads in progress are never touched.
            if outdated or publish_meta.devanagari_fields(meta):
                regenerate = True
        if existing and not regenerate:
            return existing[0]
        meta = publish_meta.generate(info, publish_meta.provider_for(state["options"]),
                                     settings)
        if existing:
            return self.uploads.update_metadata(existing[0]["id"], meta)
        return self.uploads.draft(project_id, clip, meta, {"info": info})

    def list_projects(self):
        result = []
        for path in self.root.glob("*/project.json"):
            try:
                result.append(projects.read_project(path.parent))
            except (ValueError, OSError):
                continue
        return sorted(result, key=lambda p: p["created"], reverse=True)

    def enqueue(self, project_id, action, preview=False, pick=None, after_recording=False,
                reason=None):
        if action not in {"prepare", "render", "all"}:
            raise ValueError("Unknown action.")
        if not isinstance(preview, bool):
            raise ValueError("Preview must be true or false.")
        if pick is not None and (not isinstance(pick, str) or not re.fullmatch(r"\d+(,\d+)*", pick)):
            raise ValueError("Select numbered clips, for example 1,3,5.")
        with self.lock:
            path = projects.project_path(self.root, project_id)
            if self.jobs.busy(project_id, "process") or (
                    self.scheduler is not None and self.scheduler.busy(project_id)):
                raise ValueError("This project is already queued or running.")
            if not after_recording and self.jobs.busy(project_id, "record"):
                raise ValueError("Stop the recording before finding clips.")
            state = projects.read_project(path)
            if action == "render" and (state.get("storage") or {}).get("cleaned_at"):
                raise ValueError("This project's supporting files were cleaned up after its "
                                 "clips were uploaded, so it cannot export or preview again. "
                                 "The final clips are kept. Find clips again to start over.")
            rec = state.get("recording")
            if (not after_recording and isinstance(rec, dict)
                    and rec.get("state") != "recorded"
                    and live.part_groups(path)):
                # The source is still the live URL; processing it would fail
                # with a confusing "stream is live" error.
                raise ValueError("This recording is not finalized yet. Use "
                                 "Finalize recording (or Resume recording) "
                                 "first; clips are found automatically after.")
            state.update(status="queued", stage=action, error=None,
                         last_action=action, last_preview=bool(preview), last_pick=pick,
                         last_request_id=applog.context().get("request_id"),
                         queued_at=time.time())
            projects.save_project(path, state)
            self.events.add("queued", f"{'Export' if action == 'all' else 'Render' if action == 'render' else 'Find clips'}"
                            f"{' (preview)' if preview else ''} queued"
                            f"{f' - {reason}' if reason else ''}"
                            f"{' - processing is paused' if self.jobs.paused('process') else ''}.",
                            project_id)
            self.cancelled.discard(project_id)
            if self.scheduler is not None:
                try:
                    return self._enqueue_scheduled(project_id, path, state, action, preview,
                                                   pick)
                except scheduler_mod.SchedulerError as exc:
                    projects.update_project(path, lambda s: s.update(
                        status="failed" if state.get("status") != "review" else "review",
                        error=str(exc)))
                    raise ValueError(str(exc)) from exc
            job = self.jobs.submit("process", project_id, self._run, path, action,
                                   preview, pick, kind=action)
            job.on_cancel(lambda: self._cancel_process(project_id))
            self.futures[project_id] = job.future
            applog.info("job.submit", f"{action} job queued", project_id=project_id,
                        job_id=job.id, paused=self.jobs.paused("process") or None)
            return job

    def _cancel_process(self, project_id):
        with self.lock:
            self.cancelled.add(project_id)
            child = self.children.get(project_id)
        if child is not None and child.poll() is None:
            ingest._kill_tree(child)

    def cancel(self, project_id):
        """Cancel this project's queued or running processing/recording job."""
        path = projects.project_path(self.root, project_id)
        active = self.jobs.active(project_id)
        if self.scheduler is not None and self.scheduler.busy(project_id):
            running = any(t["state"] == "leased"
                          for t in self.scheduler.source(project_id)["tasks"])
            self.scheduler.cancel(project_id)
            self.jobs.cancel_project(project_id)
            if not running:
                projects.update_project(path, lambda s: s.update(
                    status="cancelled", stage="cancelled", error=None))
            return {"cancelled": ["process"] + [job.lane for job in active]}
        if not active:
            raise ValueError("Nothing is queued or running for this project.")
        was_queued = {job.lane for job in active if job.state == "queued"}
        self.jobs.cancel_project(project_id)
        if "process" in was_queued:
            projects.update_project(path, lambda s: s.update(
                status="cancelled", stage="cancelled", error=None))
        return {"cancelled": [job.lane for job in active]}

    # ---------------------------------------------------------- recording
    def start_recording(self, project_id, settings=None):
        """Start, resume or retry recording a live/upcoming stream URL."""
        with self.lock:
            path = projects.project_path(self.root, project_id)
            if self.busy(project_id):
                raise ValueError("Wait for this project's current job to finish.")
            state = projects.read_project(path)
            rec = state.get("recording")
            if isinstance(rec, dict) and rec.get("state") == "recorded":
                raise ValueError("This stream is already recorded. Create a new "
                                 "project to record it again.")
            url = (rec or {}).get("url") or state.get("source_url") or state["source"]
            compliance.require(url, "live recording")
            if not ingest.is_url(url):
                raise ValueError("Recording needs a YouTube live or upcoming stream URL.")
            if not isinstance(rec, dict):
                rec = live.new_recording(url, settings)
            elif settings is not None:
                rec["settings"] = live.validate_settings({**rec["settings"], **settings})
            rec.update(state="queued", error=None, message="Starting...")

            def mutate(s):
                s.update(recording=rec, status="recording", stage="recording queued",
                         error=None)
            projects.update_project(path, mutate)
            stop, abort = threading.Event(), threading.Event()
            job = self.jobs.submit("record", project_id, self._record, path, stop,
                                   abort, kind="record")
            job.on_cancel(stop.set)  # cancelling a recording keeps what exists
            self.recorders[project_id] = (stop, abort)
            return job

    def stop_recording(self, project_id):
        stop_abort = self.recorders.get(project_id)
        if stop_abort is None or not self.jobs.busy(project_id, "record"):
            raise ValueError("No recording is running for this project.")
        stop_abort[0].set()

    def finalize_recording(self, project_id):
        """Join an interrupted/failed recording's parts and hand them off."""
        with self.lock:
            path = projects.project_path(self.root, project_id)
            if self.busy(project_id):
                raise ValueError("Wait for this project's current job to finish.")
            if not live.part_groups(path):
                raise ValueError("There is no recorded media to finalize.")
            return self.jobs.submit("record", project_id, self._finalize_recording,
                                    path, kind="finalize")

    def _record(self, path, stop, abort):
        try:
            result = live.Recorder(path, stop, abort).run()
        finally:
            self.recorders.pop(path.name, None)
        self._after_recording(path, result)
        return result

    def _finalize_recording(self, path):
        result = live.finalize_existing(path)
        self._after_recording(path, result)
        return result

    def _after_recording(self, path, result):
        """Hand a finished recording to the existing processing pipeline."""
        if result != "recorded" or self.closing:
            return
        after = live.validate_settings(
            projects.read_project(path)["recording"].get("settings"))["after"]
        if after == "none":
            return
        try:
            self.enqueue(path.name, after, after_recording=True)
        except ValueError as exc:
            projects.update_project(path, lambda s: s.update(
                error=f"Recording saved, but processing did not start: {exc}"))

    def _run(self, path, action, preview, pick, on_child=None):
        # Background jobs have no one to answer prompts: --no-input keeps the
        # project's saved settings, and stdin=DEVNULL stops the child from
        # inheriting (and blocking on) the Studio terminal.
        cmd = [sys.executable, str(HERE / "chopify.py"), "--project", str(path),
               "--action", action, "--out", str(path / ("previews" if preview else "clips")),
               "--no-input"]
        if preview:
            cmd.append("--preview")
        if pick:
            cmd += ["--clips", pick]
        log_path = path / "job.log"
        try:
            # Append (Phase 6): earlier runs stay readable; rotate at 2 MB.
            if log_path.exists() and log_path.stat().st_size > 2_000_000:
                os.replace(log_path, path / "job.log.1")
        except OSError:
            pass
        self.events.add("started", f"{action}{' preview' if preview else ''} started.", path.name)
        started = time.time()
        job = jobs.current_job()
        try:
            request_id = projects.read_project(path).get("last_request_id")
        except (OSError, ValueError, KeyError):
            request_id = None
        ids = {"project_id": path.name, "job_id": job.id if job else None,
               "request_id": request_id}
        env = dict(os.environ)
        env.update({"CHOPIFY_PROJECT_ID": path.name, "CHOPIFY_JOB_ID": ids["job_id"] or "",
                    "CHOPIFY_REQUEST_ID": request_id or ""})
        applog.info("worker.start", f"Worker started: {action}{' preview' if preview else ''}",
                    **ids)
        try:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {action}"
                          f"{' preview' if preview else ''} =====\n")
                log.flush()
                with self.lock:
                    if self.closing:
                        return
                    child = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=subprocess.STDOUT, env=env)
                    self.processes.add(child)
                    self.children[path.name] = child
                if on_child:
                    on_child(child)
                code = child.wait()
                with self.lock:
                    self.processes.discard(child)
                    self.children.pop(path.name, None)
                    cancelled = path.name in self.cancelled
            if cancelled:
                state = projects.read_project(path)
                state.update(status="cancelled", stage="cancelled", error=None)
                projects.save_project(path, state)
                self.events.add("cancelled", f"{action} cancelled.", path.name, level="warning")
                applog.warning("job.cancelled", f"{action} cancelled", **ids)
            elif code:
                state = projects.read_project(path)
                if state.get("status") not in ("failed",) or not state.get("error"):
                    # The worker died without recording why (e.g. preflight or
                    # an import error): take the reason from its own output.
                    state.update(status="failed",
                                 error=state.get("error") or error_from_log(log_path, code))
                    projects.save_project(path, state)
                self._log_failure(path, state, ids, time.time() - started)
            else:
                state = projects.read_project(path)
                self.events.add("finished", f"{action} finished in {time.time() - started:.0f}s "
                                f"({state.get('status')}).", path.name)
                applog.info("job.complete", f"{action} finished ({state.get('status')})",
                            seconds=round(time.time() - started, 1), **ids)
            return code
        except Exception as exc:
            applog.error("worker.start", "Worker could not run", exc=exc, **ids)
            state = projects.read_project(path)
            state.update(status="failed", error=f"Could not start the worker: {exc}")
            projects.save_project(path, state)
            self._log_failure(path, state, ids, time.time() - started)

    def _log_failure(self, path, state, ids=None, seconds=None):
        try:
            with (path / "job.log").open("rb") as handle:
                handle.seek(max(0, (path / "job.log").stat().st_size - 4000))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            tail = ""
        diagnosis = automation.diagnose(state, tail) or {}
        self.events.add("failed", f"Failed at {diagnosis.get('stage', 'unknown')}: "
                        f"{diagnosis.get('problem', state.get('error'))}", path.name,
                        level="error", advice=diagnosis.get("advice"))
        applog.error("job.failed", f"Failed at {diagnosis.get('stage', 'unknown')}: "
                     f"{diagnosis.get('problem', state.get('error'))}",
                     stage=diagnosis.get("stage"), error=state.get("error"),
                     advice=diagnosis.get("advice"),
                     seconds=round(seconds, 1) if seconds is not None else None,
                     log_tail=tail[-1500:] or None, **(ids or {"project_id": path.name}))

    def close(self):
        if self.scheduler is not None:
            self.scheduler.stopping.set()     # interrupted tasks resume after restart
        with self.lock:
            self.closing = True
            if self.upload_timer is not None:
                self.upload_timer.cancel()
            if self.storage_timer is not None:
                self.storage_timer.cancel()
            if self.monitor_timer is not None:
                self.monitor_timer.cancel()
            # Recordings stop now and are marked interrupted (parts are kept).
            for _, abort in list(self.recorders.values()):
                abort.set()
            for child in list(self.processes):
                child.terminate()
        self.jobs.shutdown()
        if self.scheduler is not None:
            self.scheduler.stop()


class Handler(BaseHTTPRequestHandler):
    server_version = "ChopifyStudio/1.4.0"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; media-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # Set when the browser closed the connection while we were answering
    # (a seek in the video player, a reload, a closed tab). Windows reports
    # this as ConnectionAbortedError [WinError 10053] or ConnectionResetError
    # [WinError 10054]. It is not an application error: the work behind the
    # request has already happened, there is just nobody left to tell.
    client_gone = False

    def _client_left(self):
        self.client_gone = True
        self.close_connection = True

    request_id = None
    status_sent = None

    def send_json(self, data, status=200):
        if self.client_gone:
            return
        if status >= 400 and isinstance(data, dict) and self.request_id:
            data = {**data, "request_id": self.request_id}
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.status_sent = status
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if self.request_id:
                self.send_header("X-Request-ID", self.request_id)
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            self._client_left()
            applog.info("http.client_gone", "Browser closed the connection before "
                        "the answer was sent (reload, seek or closed tab)",
                        path=urlsplit(getattr(self, "path", "") or "").path)

    def _dispatch(self, method, handler):
        """Request id, logging and a last-resort 500 for every request."""
        self.request_id = secrets.token_hex(6)
        path = urlsplit(self.path).path
        # Polling GETs and media are frequent: log them only when they fail.
        noisy = method == "GET"
        started = time.time()
        with applog.bound(request_id=self.request_id):
            if not noisy:
                applog.info("http.request", f"{method} {path}")
            try:
                handler()
            except Exception as exc:  # noqa: BLE001 - logged with traceback, answered
                applog.error("http.unexpected", f"{method} {path} failed unexpectedly", exc=exc)
                self.send_json({"error": f"Studio hit an unexpected error ({type(exc).__name__}: "
                                         f"{exc}). The full details are in the log "
                                         f"(request {self.request_id})."}, 500)
            status = self.status_sent or 0
            if not noisy or status >= 400:
                applog.write("ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO",
                             "http.response", f"{method} {path} -> {status}",
                             status=status, seconds=round(time.time() - started, 3))

    def do_GET(self):
        self._dispatch("GET", self._get)

    def do_POST(self):
        self._dispatch("POST", self._post)

    def valid_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def _get(self):
        if not self.valid_host():
            return self.send_json({"error": "Use the 127.0.0.1 Studio address."}, 403)
        try:
            parts = unquote(urlsplit(self.path).path).strip("/").split("/")
            if parts == ["api", "ai"]:
                from ai import studio as ai_studio
                return self.send_json(ai_studio.overview())
            if parts == ["api", "storage"]:
                return self.send_json(self.app.storage_overview())
            if len(parts) == 4 and parts[:2] == ["api", "storage"] and parts[3] == "plan":
                plan = self.app.storage_plan(parts[2])
                return self.send_json({k: v for k, v in plan.items() if k not in {"totals"}})
            if parts == ["api", "config"]:
                brands = sorted(p.stem for p in (self.app.root / "brands").glob("*.json"))
                caption_brands = {}
                for brand_path in (self.app.root / "brands").glob("*.json"):
                    try:
                        brand = branding.validate_brand(
                            projects.read_json(brand_path), brand_path.parent)
                    except (ValueError, OSError, KeyError):
                        continue
                    caption_brands[brand_path.stem] = {
                        key: brand[key] for key in branding.CAPTION_FIELDS
                        if brand[key] is not None
                    }
                return self.send_json({
                    "token": self.app.token,
                    "defaults": projects.DEFAULT_OPTIONS,
                    "new_defaults": projects.NEW_PROJECT_DEFAULTS,
                    "profiles": transcription.PROFILES,
                    "gpu": self.app.gpu_status(),
                    "compliance": compliance.status(),
                    "brands": brands,
                    "caption_brands": caption_brands,
                    "caption_presets": render_clips.CAPTION_STYLES,
                    "caption_defaults": branding.CAPTION_DEFAULTS,
                    "caption_aspects": {
                        name: {"font_size": size, "margin": margin}
                        for name, (_, _, size, margin) in render_clips.ASPECTS.items()
                    },
                    "caption_power_words": sorted(render_clips.POWER_WORDS),
                    "caption_emoji_rules": [
                        {"words": list(stems), "emoji": emoji}
                        for stems, emoji in render_clips.EMOJI_RULES
                    ],
                })
            if parts == ["api", "projects"]:
                items = self.app.list_projects()
                for item in items:
                    item["status_label"] = automation.status_label(
                        item, self.app.busy(item["id"]))
                return self.send_json({"projects": items})
            if parts == ["api", "overview"]:
                return self.send_json(self.app.overview())
            if parts == ["api", "logs"]:
                query = parse_qs(urlsplit(self.path).query)
                project = query.get("project", [None])[0]
                if project is not None:
                    projects.project_path(self.app.root, project)   # validates id
                level = query.get("level", ["INFO"])[0].upper()
                limit = max(1, min(500, int(query.get("limit", ["100"])[0])))
                entries = applog.read(limit, project, query.get("request", [None])[0],
                                      level if level in applog.LEVELS else "INFO")
                return self.send_json({"entries": [
                    {k: v for k, v in e.items() if k != "traceback"} for e in entries],
                    "folder": str(applog.log_dir())})
            if parts == ["api", "youtube"]:
                query = parse_qs(urlsplit(self.path).query)
                project = query.get("project", [None])[0]
                if project is not None:
                    projects.project_path(self.app.root, project)   # validates id
                return self.send_json(self.app.youtube_state(project))
            if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                data = project_detail(self.app.root, parts[2])
                data["busy"] = self.app.busy(parts[2])
                data["recording_active"] = self.app.jobs.busy(parts[2], "record")
                data["jobs"] = self.app.jobs.list(parts[2])[-10:]
                data["status_label"] = automation.status_label(data["project"], data["busy"])
                data["clip_counts"] = automation.clip_counts(
                    projects.project_path(self.app.root, parts[2]), data["project"],
                    data["busy"], selected=len(data["candidates"] or []))
                return self.send_json(data)
            if parts and parts[0] == "media" and len(parts) >= 3:
                path = projects.project_path(self.app.root, parts[1]).resolve()
                if parts[2:] == ["source"]:
                    source = projects.read_json(path / "transcript.json")["video"]
                    return self.send_file(Path(source))
                if len(parts) != 4 or parts[2] not in {"clips", "previews"}:
                    raise ValueError("Unknown media path.")
                file = (path / parts[2] / parts[3]).resolve()
                if file.parent != path / parts[2] or file.suffix not in {".mp4", ".srt", ".png", ".json"}:
                    raise ValueError("Invalid media path.")
                return self.send_file(file)
            static = {"": "index.html", "app.js": "app.js", "style.css": "style.css"}
            filename = "/".join(parts)
            if filename in static:
                return self.send_file(STATIC / static[filename])
            self.send_json({"error": "Not found."}, 404)
        except (OSError, KeyError):
            self.send_json({"error": "Project or file not found."}, 404)
        except (ValueError, TypeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def _post(self):
        origin = self.headers.get("Origin")
        expected = f"http://127.0.0.1:{self.server.server_port}"
        if (not self.valid_host() or (origin is not None and origin != expected)
                or not secrets.compare_digest(self.headers.get("X-Chopify-Token", ""), self.app.token)):
            return self.send_json({"error": "Request rejected. Reload Studio and retry."}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 10_000_000:
                raise ValueError("Request must be JSON smaller than 10 MB.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            parts = unquote(urlsplit(self.path).path).strip("/").split("/")
            if parts == ["api", "detect"]:
                # Network probe: deliberately outside the global lock.
                return self.send_json(detect_source(body.get("source"),
                                                    self.app.youtube_data()))
            if parts[:2] == ["api", "automation"]:
                return self.automation_post(parts, body)
            if parts == ["api", "scheduler"]:
                if self.app.scheduler is None:
                    raise ValueError("The scheduler is turned off (feature flag).")
                applog.info("scheduler.settings", "Scheduler settings changed")
                self.app.scheduler.configure(body)
                return self.send_json(self.app.scheduler.status())
            if parts[:2] == ["api", "ai"] and len(parts) == 3:
                from ai import studio as ai_studio
                action = parts[2]
                provider = body.get("provider")
                applog.info("ai.settings", f"AI settings: {action}",
                            provider=provider if isinstance(provider, str) else None)
                if action == "settings":
                    ai_studio.apply(body)
                elif action in ("key", "key-remove"):
                    # Keys live only in .env; the request body (a key?) is ignored.
                    return self.send_json({"error": ai_studio.KEYS_IN_ENV}, 410)
                elif action == "health":
                    from ai import gateway as ai_gateway
                    if not isinstance(provider, str):
                        raise ValueError("Choose a provider.")
                    return self.send_json(ai_gateway.get().health(provider))
                elif action == "confirm":
                    if body.get("confirm") is not True and not body.get("revoke"):
                        raise ValueError("Confirmation is required.")
                    ai_studio.confirm(provider, body.get("what"), bool(body.get("revoke")))
                else:
                    return self.send_json({"error": "Not found."}, 404)
                return self.send_json(ai_studio.overview())
            if parts == ["api", "storage", "cleanup"]:
                project_id = body.get("project_id")
                applog.info("storage.request", "Cleanup requested in Studio",
                            project_id=project_id, scope="project" if project_id else "all")
                if project_id:
                    if not isinstance(project_id, str):
                        raise ValueError("project_id must be text.")
                    return self.send_json(self.app.cleanup_project(project_id))
                return self.send_json(self.app.cleanup_all())
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "delete-clips":
                files = body.get("files")
                if not isinstance(files, list) or not files or len(files) > 500:
                    raise ValueError("Choose the clips to delete.")
                confirm = body.get("confirm", False)
                if not isinstance(confirm, bool):
                    raise ValueError("confirm must be true or false.")
                applog.info("storage.request", "Clip deletion " + (
                    "confirmed" if confirm else "previewed") + " in Studio",
                    project_id=parts[2], clips=len(files))
                return self.send_json(self.app.delete_clips(
                    parts[2], files, confirm=confirm, confirmed=body.get("confirmed")))
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "retry":
                return self.send_json(self.app.retry(parts[2]))
            if parts[:2] == ["api", "youtube"] or parts[:2] == ["api", "uploads"] or (
                    len(parts) == 4 and parts[:2] == ["api", "projects"]
                    and parts[3] == "publish"):
                return self.youtube_post(parts, body)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] in {
                    "record-stop", "cancel"}:
                # These act on a busy project, so they skip the busy check below.
                if parts[3] == "record-stop":
                    self.app.stop_recording(parts[2])
                    return self.send_json({"ok": True})
                return self.send_json({"ok": True, **self.app.cancel(parts[2])})
            with self.app.lock:
                if parts == ["api", "record"]:
                    source = body.get("source")
                    if not isinstance(source, str) or not ingest.is_url(source.strip()):
                        raise ValueError("Recording needs a YouTube live or upcoming stream URL.")
                    compliance.require(source.strip(), "live recording")
                    state = live.start_recording_project(
                        self.app.root, source.strip(), body.get("settings"),
                        body.get("options", {}))
                    self.app.start_recording(state["id"])
                    return self.send_json(projects.read_project(
                        projects.project_path(self.app.root, state["id"])), 201)
                if parts == ["api", "projects"]:
                    return self.send_json(projects.create_project(self.app.root, body["source"], body.get("options", {})), 201)
                if parts == ["api", "batch"]:
                    sources = body["sources"]
                    if not isinstance(sources, list) or not 1 <= len(sources) <= 100 or any(not isinstance(s, str) or not s.strip() for s in sources):
                        raise ValueError("Provide 1 to 100 sources.")
                    options = projects.validate_options(body.get("options", {}))
                    allow = body.get("allow_duplicates", False)
                    if not isinstance(allow, bool):
                        raise ValueError("allow_duplicates must be true or false.")
                    applog.info("findclips.request", "Find clips request received",
                                sources=[s.strip() for s in sources][:20], count=len(sources))
                    errors = []
                    result, duplicates = self.app.create_sources(
                        [s.strip() for s in sources], options, allow_duplicates=allow,
                        errors=errors)
                    if errors and not result and not duplicates:
                        return self.send_json({"error": errors[0]["error"],
                                               "errors": errors}, 400)
                    return self.send_json({"projects": result, "duplicates": duplicates,
                                           "errors": errors}, 201)
                if parts == ["api", "brands"]:
                    name = body["name"]
                    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", name):
                        raise ValueError("Preset name must use letters, numbers, - or _.")
                    brand = branding.validate_brand(body["preset"])
                    path = self.app.root / "brands" / f"{name}.json"
                    projects.atomic_json(path, brand)
                    return self.send_json({"name": name, "path": str(path)})
                if len(parts) == 4 and parts[:2] == ["api", "projects"]:
                    project_id, action = parts[2:]
                    path = projects.project_path(self.app.root, project_id)
                    state = projects.read_project(path)
                    if self.app.busy(project_id):
                        raise ValueError("Wait for this project's current job to finish before editing.")
                    if action == "settings":
                        settings = dict(body)
                        if "brand_name" in settings:
                            name = settings.pop("brand_name")
                            if name and (not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", name)):
                                raise ValueError("Invalid brand preset.")
                            settings["brand"] = str(self.app.root / "brands" / f"{name}.json") if name else None
                        state["options"] = projects.validate_options({**state["options"], **settings})
                        projects.save_project(path, state)
                    elif action == "transcript":
                        projects.edit_transcript(path, body["words"])
                    elif action == "candidates":
                        duration = projects.read_json(path / "transcript.json")["duration"]
                        segments = projects.validate_segments(body["candidates"], duration)
                        projects.check_lengths(segments, projects.validate_options(state["options"]))
                        projects.atomic_json(path / "segments.json", segments)
                    elif action == "run":
                        self.app.enqueue(project_id, body.get("action", "prepare"), body.get("preview", False), body.get("pick"))
                    elif action == "record":
                        settings = body.get("settings")
                        if settings is not None and not isinstance(settings, dict):
                            raise ValueError("Recording settings must be an object.")
                        self.app.start_recording(project_id, settings)
                    elif action == "record-finalize":
                        self.app.finalize_recording(project_id)
                    else:
                        return self.send_json({"error": "Not found."}, 404)
                    return self.send_json({"ok": True})
            self.send_json({"error": "Not found."}, 404)
        except FileNotFoundError:
            self.send_json({"error": "Project or transcript not found. Prepare the project first."}, 404)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def automation_post(self, parts, body):
        app = self.app
        try:
            if parts == ["api", "automation", "settings"]:
                result = app.settings.update(body.get("settings") or {})
                app.events.add("settings", "Automation settings saved.")
                app._schedule_monitor()
            elif parts == ["api", "automation", "pause"]:
                result = app.set_paused(body.get("target"), body.get("paused") is True)
            elif parts == ["api", "automation", "channels"]:
                result = app.monitor.add(body.get("url"), body.get("label", ""))
                app.events.add("monitor", f"Monitoring {result['url']} (existing videos "
                               "are skipped; only new ones are processed).")
                app._schedule_monitor()
            elif (len(parts) == 5 and parts[:3] == ["api", "automation", "channels"]
                  and parts[4] == "remove"):
                app.monitor.remove(parts[3])
                app._schedule_monitor()
                result = {"ok": True}
            elif parts == ["api", "automation", "check"]:
                threading.Thread(target=app.check_channels, daemon=True).start()
                result = {"ok": True, "started": True}
            else:
                return self.send_json({"error": "Not found."}, 404)
            return self.send_json(result)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return self.send_json({"error": str(exc)}, 400)

    def youtube_post(self, parts, body):
        app = self.app
        try:
            if parts == ["api", "youtube", "client"]:
                result = app.youtube.import_client(body.get("path", ""))
            elif parts == ["api", "youtube", "connect"]:
                url = app.youtube.begin_signin(open_browser=webbrowser.open)
                result = {"url": url}
            elif parts == ["api", "youtube", "disconnect"]:
                result = app.youtube.disconnect()
            elif parts == ["api", "youtube", "settings"]:
                result = app.uploads.update_settings(body.get("settings"))
            elif len(parts) == 4 and parts[3] == "publish":
                result = public_upload(app.publish_draft(
                    parts[2], body.get("clip"), bool(body.get("regenerate"))))
            elif len(parts) == 4 and parts[:2] == ["api", "uploads"]:
                entry_id, action = parts[2], parts[3]
                if action == "metadata":
                    result = app.uploads.update_metadata(entry_id, body.get("metadata"))
                elif action == "approve":
                    # The only path to an upload: an explicit, confirmed approval.
                    result = app.uploads.approve(entry_id, body.get("metadata"),
                                                 confirm=body.get("confirm") is True)
                    app.kick_uploads()
                elif action == "retry":
                    result = app.uploads.retry(entry_id)
                    app.kick_uploads()
                elif action == "cancel":
                    result = app.uploads.cancel(entry_id)
                elif action == "regenerate":
                    entry = app.uploads.get(entry_id)
                    result = app.publish_draft(entry["project_id"], entry["name"], True)
                else:
                    return self.send_json({"error": "Not found."}, 404)
                result = public_upload(result)
            else:
                return self.send_json({"error": "Not found."}, 404)
            return self.send_json(result)
        except (youtube_auth.AuthError, youtube_upload.UploadError, ValueError,
                KeyError, TypeError, OSError) as exc:
            return self.send_json({"error": str(exc)}, 400)

    def send_file(self, path):
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
            if not match:
                return self.send_json({"error": "Unsupported byte range."}, 416)
            start = int(match[1])
            end = min(int(match[2]) if match[2] else end, end)
            if start > end or start >= size:
                return self.send_json({"error": "Range outside file."}, 416)
            status = 206
        # Open first: a read error must become a clean 404, not a JSON body
        # appended to headers that were already sent.
        with path.open("rb") as file:
            file.seek(start)
            try:
                self.send_response(status)
                self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(max(0, end - start + 1)))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                remaining = end - start + 1
                while remaining > 0:
                    chunk = file.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except ConnectionError:
                # Browsers cancel media range requests on every seek.
                self._client_left()


class StudioServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # A browser that disconnects while a request is being read is normal;
        # every other unexpected error keeps its full traceback.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


def make_server(root="projects", port=8765):
    server = StudioServer(("127.0.0.1", port), Handler)
    server.app = Studio(root)
    # Resume uploads the user already approved (e.g. interrupted by a restart).
    server.app.kick_uploads()
    # Phase 6: resume interrupted processing (setting) and arm the monitor.
    server.app.start_automation()
    return server


def main():
    parser = argparse.ArgumentParser(description="Open Chopify Studio on your own computer.")
    parser.add_argument("--projects-dir", default="projects")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    env = envconfig.status()        # read .env first (names only are logged, never values)
    applog.info("env.loaded" if env["exists"] else "env.missing",
                f".env {'loaded' if env['exists'] else 'not found'}: {env['path']}",
                variables=len(env["names"]), bad_lines=env["bad_lines"] or None)
    if env["error"] or env["bad_lines"]:
        applog.warning("env.problem", env["error"] or
                       f".env lines {env['bad_lines']} could not be read and were ignored.")
    server = make_server(args.projects_dir, args.port)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Chopify Studio: {url}\nProjects: {server.app.root}\n"
          f"Logs: {applog.log_dir()}  (python applog.py --errors)\nPress Ctrl+C to stop.",
          flush=True)
    applog.info("studio.start", f"Studio started on {url}", projects=str(server.app.root))
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.close()
        server.server_close()


if __name__ == "__main__":
    main()
