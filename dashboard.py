"""Chopify Studio: loopback browser UI with a serial, resumable project queue."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

import branding
import ingest
import jobs
import live
import projects
import publish_meta
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


def detect_source(source):
    """Classify a local path or URL (normal video, live, upcoming, ...)."""
    if not isinstance(source, str) or not source.strip() or len(source) > 8000:
        raise ValueError("Provide a video path or URL.")
    source = source.strip()
    if not ingest.is_url(source) and not Path(source).expanduser().is_file():
        raise ValueError(f"Video file does not exist or URL is invalid: {source}")
    meta = ingest.probe_source(source)
    info = ingest.describe_source(meta)
    return {**info, "source": source, "title": meta.get("title"),
            "duration": meta.get("duration"),
            "live_status": meta.get("live_status"),
            "release_timestamp": meta.get("release_timestamp")}


class Studio:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "brands").mkdir(exist_ok=True)
        self.token = secrets.token_urlsafe(32)
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
        for path in self.root.glob("*/project.json"):
            try:
                state = projects.read_project(path.parent)
                changed = live.mark_interrupted(state)
                if state["status"] in {"queued", "running", "recording"}:
                    state.update(status="interrupted", error="Studio stopped. Resume this project to continue.")
                    changed = True
                if changed:
                    projects.save_project(path.parent, state)
            except (ValueError, OSError, KeyError):
                continue

    def busy(self, project_id):
        return self.jobs.busy(project_id)

    # ---------------------------------------------------------- YouTube
    UPLOAD_JOB = "youtube-uploads"

    def kick_uploads(self):
        """Process approved uploads on the upload lane (one at a time)."""
        with self.lock:
            if self.closing or self.jobs.busy(self.UPLOAD_JOB, "upload"):
                return
            if not self.uploads.due():
                self._schedule_uploads()
                return
            self.jobs.submit("upload", self.UPLOAD_JOB, self._run_uploads, kind="upload")

    def _run_uploads(self):
        try:
            self.uploads.process()
        finally:
            self._schedule_uploads()

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

    def enqueue(self, project_id, action, preview=False, pick=None, after_recording=False):
        if action not in {"prepare", "render", "all"}:
            raise ValueError("Unknown action.")
        if not isinstance(preview, bool):
            raise ValueError("Preview must be true or false.")
        if pick is not None and (not isinstance(pick, str) or not re.fullmatch(r"\d+(,\d+)*", pick)):
            raise ValueError("Select numbered clips, for example 1,3,5.")
        with self.lock:
            path = projects.project_path(self.root, project_id)
            if self.jobs.busy(project_id, "process"):
                raise ValueError("This project is already queued or running.")
            if not after_recording and self.jobs.busy(project_id, "record"):
                raise ValueError("Stop the recording before finding clips.")
            state = projects.read_project(path)
            rec = state.get("recording")
            if (not after_recording and isinstance(rec, dict)
                    and rec.get("state") != "recorded"
                    and live.part_groups(path)):
                # The source is still the live URL; processing it would fail
                # with a confusing "stream is live" error.
                raise ValueError("This recording is not finalized yet. Use "
                                 "Finalize recording (or Resume recording) "
                                 "first; clips are found automatically after.")
            state.update(status="queued", stage=action, error=None)
            projects.save_project(path, state)
            self.cancelled.discard(project_id)
            job = self.jobs.submit("process", project_id, self._run, path, action,
                                   preview, pick, kind=action)
            job.on_cancel(lambda: self._cancel_process(project_id))
            self.futures[project_id] = job.future
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

    def _run(self, path, action, preview, pick):
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
        try:
            with (path / "job.log").open("w", encoding="utf-8") as log:
                with self.lock:
                    if self.closing:
                        return
                    child = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=subprocess.STDOUT)
                    self.processes.add(child)
                    self.children[path.name] = child
                code = child.wait()
                with self.lock:
                    self.processes.discard(child)
                    self.children.pop(path.name, None)
                    cancelled = path.name in self.cancelled
            if cancelled:
                state = projects.read_project(path)
                state.update(status="cancelled", stage="cancelled", error=None)
                projects.save_project(path, state)
            elif code:
                state = projects.read_project(path)
                state.update(status="failed", error=state.get("error") or "Job failed. Open the job log for setup or input details.")
                projects.save_project(path, state)
            return code
        except Exception as exc:
            state = projects.read_project(path)
            state.update(status="failed", error=str(exc))
            projects.save_project(path, state)

    def close(self):
        with self.lock:
            self.closing = True
            if self.upload_timer is not None:
                self.upload_timer.cancel()
            # Recordings stop now and are marked interrupted (parts are kept).
            for _, abort in list(self.recorders.values()):
                abort.set()
            for child in list(self.processes):
                child.terminate()
        self.jobs.shutdown()


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

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if not self.valid_host():
            return self.send_json({"error": "Use the 127.0.0.1 Studio address."}, 403)
        try:
            parts = unquote(urlsplit(self.path).path).strip("/").split("/")
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
                return self.send_json({"projects": self.app.list_projects()})
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
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, KeyError):
            self.send_json({"error": "Project or file not found."}, 404)
        except (ValueError, TypeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_POST(self):
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
                return self.send_json(detect_source(body.get("source")))
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
                    result = []
                    for source in sources:
                        state = projects.create_project(self.app.root, source, options)
                        self.app.enqueue(state["id"], "prepare")
                        result.append(state)
                    return self.send_json({"projects": result}, 201)
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
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as file:
                file.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = file.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def make_server(root="projects", port=8765):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.app = Studio(root)
    # Resume uploads the user already approved (e.g. interrupted by a restart).
    server.app.kick_uploads()
    return server


def main():
    parser = argparse.ArgumentParser(description="Open Chopify Studio on your own computer.")
    parser.add_argument("--projects-dir", default="projects")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = make_server(args.projects_dir, args.port)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Chopify Studio: {url}\nProjects: {server.app.root}\nPress Ctrl+C to stop.", flush=True)
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
