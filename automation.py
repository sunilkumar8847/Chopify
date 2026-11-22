"""Phase 6: automation around the existing pipeline (no new processing logic).

* ``Settings``      - automation settings and daily limits (projects root
                      ``.studio/automation.json``, atomic writes).
* ``EventLog``      - append-only JSON-lines history of what Studio did, rotated
                      at 1 MB (``.studio/events.jsonl``, ``.1``, ``.2``).
* ``source_key``    - one fingerprint per source (YouTube video ID or resolved
                      file path) so the same video is never processed twice.
* ``Monitor``       - optional channel watching: new uploads become processing
                      jobs, live streams become recordings. It NEVER publishes:
                      uploads still need the user's explicit approval.
* ``diagnose``      - which stage failed and what the user can do about it.
* ``pipeline``      - per-project stage overview for the dashboard.

Everything here is local; the only network access is ``yt-dlp`` listing a
channel's latest videos when monitoring is switched on.
"""
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jsoncache

STATE_DIR = ".studio"
SETTINGS_FILE = "automation.json"
EVENTS_FILE = "events.jsonl"
EVENTS_MAX_BYTES = 1_000_000
EVENTS_KEEP = 2
ACTIONS = ("prepare", "all", "none")
DEFAULTS = {
    "processing_paused": False,
    "uploads_paused": False,
    "resume_after_restart": True,
    "new_source_action": "prepare",      # what monitoring does with a new video
    "max_new_sources_per_day": 5,        # monitoring limit (manual adds are free)
    "monitor_enabled": False,
    "monitor_interval_minutes": 30,
    "record_live": True,
    "new_project_options": {"aspect": "9:16"},
}
MONITOR_LATEST = 5                       # newest entries checked per tab
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def state_dir(root):
    path = Path(root) / STATE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def today():
    return datetime.date.today().isoformat()


# ------------------------------------------------------------- settings

def validate_settings(values, current=None):
    if not isinstance(values, dict):
        raise ValueError("Automation settings must be an object.")
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown automation settings: {', '.join(sorted(unknown))}")
    result = {**DEFAULTS, **(current or {}), **values}
    for key in ("processing_paused", "uploads_paused", "resume_after_restart",
                "monitor_enabled", "record_live"):
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} must be true or false.")
    if result["new_source_action"] not in ACTIONS:
        raise ValueError("new_source_action must be prepare, all or none.")
    limit = result["max_new_sources_per_day"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 100:
        raise ValueError("max_new_sources_per_day must be a whole number from 0 to 100.")
    minutes = result["monitor_interval_minutes"]
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 5 <= minutes <= 1440:
        raise ValueError("monitor_interval_minutes must be 5 to 1440.")
    if not isinstance(result["new_project_options"], dict):
        raise ValueError("new_project_options must be an object.")
    return {key: result[key] for key in DEFAULTS}


class Settings:
    """Automation settings plus small durable state (usage, channels)."""

    def __init__(self, root):
        self.path = state_dir(root) / SETTINGS_FILE
        self.lock = threading.RLock()

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data):
        with jsoncache.locked(self.path):
            jsoncache.write_atomic(self.path, data)

    def get(self):
        with self.lock:
            stored = self._read().get("settings") or {}
            try:
                return validate_settings({}, {k: v for k, v in stored.items() if k in DEFAULTS})
            except ValueError:
                return dict(DEFAULTS)

    def update(self, values):
        with self.lock:
            data = self._read()
            data["settings"] = validate_settings(values, self.get())
            self._write(data)
            return data["settings"]

    # daily limit for automatically created sources
    def usage(self):
        with self.lock:
            used = (self._read().get("usage") or {}).get(today(), 0)
        limit = self.get()["max_new_sources_per_day"]
        return {"day": today(), "new_sources": used, "limit": limit,
                "left": max(0, limit - used)}

    def take_source_slot(self):
        """Count one automatic source; False when today's limit is used up."""
        with self.lock:
            data = self._read()
            usage = data.get("usage") or {}
            used = usage.get(today(), 0)
            if used >= self.get()["max_new_sources_per_day"]:
                return False
            data["usage"] = {today(): used + 1}      # only today is kept
            self._write(data)
            return True

    # monitored channels
    def channels(self):
        with self.lock:
            return list(self._read().get("channels") or [])

    def save_channels(self, channels):
        with self.lock:
            data = self._read()
            data["channels"] = channels
            self._write(data)


# ------------------------------------------------------------- event log

class EventLog:
    """Append-only history (JSON lines), rotated at EVENTS_MAX_BYTES."""

    def __init__(self, root):
        self.path = state_dir(root) / EVENTS_FILE
        self.lock = threading.Lock()

    def add(self, kind, message, project_id=None, level="info", **extra):
        event = {"time": time.time(), "kind": kind, "level": level,
                 "message": str(message)[:500], "project_id": project_id}
        event.update({k: v for k, v in extra.items() if v is not None})
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self.lock:
            try:
                if self.path.exists() and self.path.stat().st_size > EVENTS_MAX_BYTES:
                    self._rotate()
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                pass
        return event

    def _rotate(self):
        for index in range(EVENTS_KEEP, 0, -1):
            older = self.path.with_name(f"{EVENTS_FILE}.{index}")
            newer = self.path if index == 1 else self.path.with_name(f"{EVENTS_FILE}.{index - 1}")
            if newer.exists():
                os.replace(newer, older)

    def tail(self, limit=100, project_id=None):
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events = []
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if project_id is None or event.get("project_id") == project_id:
                events.append(event)
            if len(events) >= limit:
                break
        return events


# ------------------------------------------------------ duplicate sources

def youtube_id(source):
    """The 11-character video ID of a YouTube URL, else None."""
    try:
        parts = urlsplit(str(source).strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host.endswith("youtu.be"):
        candidate = parts.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        query = parse_qs(parts.query)
        if query.get("v"):
            candidate = query["v"][0]
        else:
            bits = [b for b in parts.path.split("/") if b]
            candidate = bits[1] if len(bits) >= 2 and bits[0] in {
                "live", "shorts", "embed", "v"} else ""
    else:
        return None
    return candidate if YOUTUBE_ID.match(candidate or "") else None


def source_key(source):
    """One fingerprint per video: 'youtube:<id>', 'file:<path>' or 'url:<url>'."""
    text = str(source or "").strip()
    video = youtube_id(text)
    if video:
        return f"youtube:{video}"
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        return "url:" + text.rstrip("/")
    try:
        return "file:" + os.path.normcase(str(Path(text).expanduser().resolve()))
    except (OSError, ValueError):
        return "file:" + os.path.normcase(text)


def project_source_keys(state):
    """Every fingerprint a saved project answers to."""
    keys = {source_key(state.get("source"))}
    recording = state.get("recording") or {}
    for value in (recording.get("url"), recording.get("source"),
                  (state.get("source_meta") or {}).get("discovery"),
                  (state.get("source_meta") or {}).get("webpage_url")):
        if value:
            keys.add(source_key(value))
    return keys


# ----------------------------------------------------------- monitoring

def channel_id(url):
    return hashlib.sha256(str(url).strip().lower().rstrip("/").encode()).hexdigest()[:10]


def normalize_channel(url):
    """https://www.youtube.com/@name (or /channel/UC..., /c/..., /user/...)."""
    text = str(url or "").strip()
    if text.startswith("@"):
        text = "https://www.youtube.com/" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (
            host == "youtube.com" or host.endswith(".youtube.com")):
        raise ValueError("Enter a YouTube channel address, e.g. https://www.youtube.com/@name")
    bits = [b for b in parts.path.split("/") if b]
    if not bits or not (bits[0].startswith("@") or (
            bits[0] in {"channel", "c", "user"} and len(bits) >= 2)):
        raise ValueError("That is not a channel address (expected /@name or /channel/ID).")
    keep = bits[:1] if bits[0].startswith("@") else bits[:2]
    return "https://www.youtube.com/" + "/".join(keep)


def ytdlp_lister(channel_url, tab, limit=MONITOR_LATEST, timeout=120):
    """Newest entries of a channel tab via yt-dlp (no downloads)."""
    import compliance
    compliance.require(channel_url, "channel monitoring")
    cmd = [sys.executable, "-m", "yt_dlp", "--flat-playlist", "-J", "--no-warnings",
           "--playlist-end", str(limit), f"{channel_url}/{tab}"]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip().splitlines()
        text = message[-1] if message else "yt-dlp failed"
        if "does not have a" in text and "tab" in text:
            return []                          # e.g. a channel without /streams
        raise RuntimeError(text[:300])
    data = json.loads(result.stdout or "{}")
    entries = []
    for entry in data.get("entries") or []:
        video = entry.get("id")
        if not video or not YOUTUBE_ID.match(str(video)):
            continue
        entries.append({"id": video, "title": entry.get("title") or "",
                        "url": f"https://www.youtube.com/watch?v={video}",
                        "live_status": entry.get("live_status")})
    return entries


class Monitor:
    """Watch channels; report new videos and live streams. Never publishes."""

    TABS = ("videos", "streams")

    def __init__(self, settings, lister=None, clock=time.time):
        self.settings = settings
        self.lister = lister or ytdlp_lister
        self.clock = clock
        self.lock = threading.Lock()

    def add(self, url, label=""):
        url = normalize_channel(url)
        import compliance
        compliance.require(url, "channel monitoring")
        channels = self.settings.channels()
        if any(c["url"] == url for c in channels):
            raise ValueError("This channel is already monitored.")
        entry = {"id": channel_id(url), "url": url, "label": str(label or "")[:80],
                 "added": self.clock(), "baseline": False, "seen": [],
                 "last_check": None, "last_error": None, "found": 0}
        self.settings.save_channels(channels + [entry])
        return entry

    def remove(self, channel):
        channels = self.settings.channels()
        kept = [c for c in channels if c["id"] != channel]
        if len(kept) == len(channels):
            raise ValueError("Unknown channel.")
        self.settings.save_channels(kept)

    def check(self, handle):
        """Check every channel once. ``handle(kind, entry, channel)`` is called
        for each new item; kind is "video", "live" or "upcoming". It returns
        True when the item was taken (so it is never offered again).

        The first check of a channel only records what already exists, so
        adding a channel never processes its back catalogue.
        """
        import compliance
        if self.lister is ytdlp_lister and not compliance.youtube_downloads_allowed():
            # Channels and their history are kept untouched; nothing is read.
            return [{"channel": None, "error": compliance.MESSAGE, "blocked": True}]
        with self.lock:
            channels = self.settings.channels()
            results = []
            for channel in channels:
                seen = set(channel.get("seen") or [])
                found = []
                try:
                    for tab in self.TABS:
                        for entry in self.lister(channel["url"], tab):
                            if entry["id"] not in seen and entry["id"] not in {
                                    e["id"] for e in found}:
                                found.append(entry)
                    channel["last_error"] = None
                except Exception as exc:  # noqa: BLE001 - shown on the dashboard
                    channel["last_error"] = str(exc)[:300]
                    channel["last_check"] = self.clock()
                    results.append({"channel": channel["id"], "error": channel["last_error"]})
                    continue
                taken = []
                if not channel.get("baseline"):
                    taken = [e["id"] for e in found]       # remember, don't process
                    channel["baseline"] = True
                else:
                    for entry in found:
                        status = entry.get("live_status")
                        kind = ("live" if status == "is_live" else
                                "upcoming" if status == "is_upcoming" else "video")
                        if status == "post_live":
                            continue                       # replay not ready yet
                        if handle(kind, entry, channel):
                            taken.append(entry["id"])
                            channel["found"] = channel.get("found", 0) + 1
                channel["seen"] = (list(seen) + taken)[-500:]
                channel["last_check"] = self.clock()
                results.append({"channel": channel["id"], "new": len(taken)})
            # keep edits made meanwhile (add/remove) but store our progress
            current = {c["id"]: c for c in self.settings.channels()}
            for channel in channels:
                if channel["id"] in current:
                    current[channel["id"]] = channel
            self.settings.save_channels(list(current.values()))
            return results


# ------------------------------------------------------------- diagnosis

STAGES = ("source", "transcribe", "discover", "select", "render", "upload")
_RULES = [
    # (stage or None, regex, problem, what to do)
    (None, r"sign in to confirm|cookies|age.?restricted|members.only|private video",
     "YouTube refused the download (sign-in, age or membership check).",
     "Open the video in your browser to check it plays. Private, members-only "
     "and age-restricted videos cannot be downloaded; use a local copy instead."),
    (None, r"file does not exist|no such file|cannot find the file",
     "The video file was not found.",
     "Check that the file still exists at that path (moved, renamed or on a "
     "disconnected drive?), then add it again."),
    (None, r"video (is )?unavailable|is not available|has been removed|does not exist|404",
     "The video is unavailable.",
     "Check the URL. Removed or region-blocked videos cannot be processed."),
    (None, r"this stream is live|is live\b|has not started|upcoming",
     "The source is a live or upcoming stream.",
     "Use Record this stream; clips are found when the recording finishes."),
    (None, r"timed? ?out|connection|network|temporary failure|getaddrinfo|10054|10060",
     "A network problem interrupted the job.",
     "Check the internet connection and press Retry; finished stages are reused."),
    ("transcribe", r"cublas|cudnn|cuda",
     "The GPU libraries for Whisper could not be loaded.",
     "Install the CUDA libraries in the virtual environment (see "
     "docs/TROUBLESHOOTING.md) or set Device to cpu in the project settings, then Retry."),
    (None, r"out of memory|mkl_malloc|memoryerror|cannot allocate",
     "The computer ran out of memory.",
     "Close other programs or choose a smaller Whisper model, then Retry. "
     "Finished transcript chunks are kept."),
    (None, r"no space left|disk full|errno 28",
     "The disk is full.",
     "Free space on the projects drive, then Retry."),
    ("render", r"no clips reached|no candidates were found",
     "No clip reached the minimum score.",
     "Review the candidate scores; lower the minimum score for this project "
     "only if you want weaker clips, or try another video."),
    ("render", r"section download stalled|ranged downloads",
     "YouTube would not send just the clip's part of the video.",
     "Retry later; Chopify then downloads only the clips' sections. If it keeps "
     "stalling, the retry falls back to downloading the full video once (large)."),
    ("render", r"(ffmpeg|nvenc|libx264|encoder)[^\n]*(error|failed|non-zero|exited)",
     "Video encoding failed.",
     "Retry once; if it fails again set CHOPIFY_ENCODER=x264 or check the job log."),
    (None, r"ffmpeg.*not found|ffprobe.*not found|yt-dlp.*not found|preflight failed",
     "A required program is missing.",
     "Run python chopify.py --doctor and install what it lists."),
    (None, r"source video changed",
     "The source file changed after transcription.",
     "Press Find clips again so the transcript matches the file."),
    (None, r"studio stopped|interrupted",
     "Studio stopped while this job was running.",
     "Press Retry (or enable 'Resume after restart'); finished stages are reused."),
    (None, r"cancel",
     "The job was cancelled.",
     "Press Retry to run it again."),
]


def stage_of(stage_text):
    text = str(stage_text or "").lower()
    if text.startswith(("render", "preview")):
        return "render"
    for stage in STAGES:
        if text.startswith(stage):
            return stage
    if "download" in text or "probe" in text or "record" in text:
        return "source"
    return None


def diagnose(state, log_text=""):
    """{"stage", "problem", "advice"} for a failed or interrupted project."""
    if state.get("status") not in ("failed", "interrupted", "cancelled"):
        return None
    stage = stage_of(state.get("stage")) or "source"
    haystack = f"{state.get('error') or ''}\n{log_text or ''}".lower()
    for rule_stage, pattern, problem, advice in _RULES:
        if rule_stage is not None and rule_stage != stage:
            continue
        if re.search(pattern, haystack):
            return {"stage": stage, "problem": problem, "advice": advice}
    return {"stage": stage, "problem": state.get("error") or "The job failed.",
            "advice": "Open the job log for details, then press Retry; "
                      "finished stages are reused."}


# --------------------------------------------------------------- pipeline

def pipeline(path, state, uploads=()):
    """Per-stage status for one project: done | active | failed | pending | skipped."""
    path = Path(path)
    status = state.get("status")
    current = stage_of(state.get("stage"))
    has = {
        "source": bool((state.get("cache") or {}).get("source")) or (path / "transcript.json").exists(),
        "transcribe": (path / "transcript.json").exists(),
        "discover": (path / "candidates.json").exists() or (path / "segments.json").exists(),
        "select": (path / "segments.json").exists(),
        "render": any((path / "clips").glob("*.mp4")) if (path / "clips").is_dir() else False,
        "upload": any(u.get("state") == "uploaded" for u in uploads),
    }
    if (state.get("storage") or {}).get("cleaned_at"):
        # Supporting files were deleted after upload; these stages finished.
        for stage in ("source", "transcribe", "discover", "select"):
            has[stage] = True
    running = status in ("queued", "running", "recording", "finalizing")
    result = {}
    for stage in STAGES:
        if stage == current and running:
            result[stage] = "active"
        elif stage == current and status in ("failed", "interrupted"):
            result[stage] = "failed"
        elif has[stage]:
            result[stage] = "done"
        else:
            result[stage] = "pending"
    if status == "review" and not has["render"]:
        result["render"] = "pending"
    if uploads and not has["upload"]:
        states = {u.get("state") for u in uploads}
        result["upload"] = ("active" if states & {"uploading", "queued", "quota_wait"} else
                            "failed" if "failed" in states else "pending")
    return result


def progress(state):
    """0-1 fraction of the current stage when known (transcribe, render i/n)."""
    info = state.get("progress") or {}
    if info.get("total"):
        return {"stage": info.get("stage"), "fraction": round(
            min(1.0, float(info.get("done", 0)) / float(info["total"])), 3),
            "detail": info.get("detail")}
    match = re.search(r"(render|preview) (\d+)/(\d+)", str(state.get("stage") or ""))
    if match:
        done, total = int(match.group(2)) - 1, int(match.group(3))
        return {"stage": "render", "fraction": round(done / total, 3) if total else 0,
                "detail": f"clip {match.group(2)} of {total}"}
    return None


STAGE_LABELS = {"source": "Getting the video", "transcribe": "Transcribing speech",
                "discover": "Finding candidate moments", "select": "AI judging and choosing clips",
                "render": "Rendering clips", "upload": "Uploading"}
# Named steps inside a stage.
STEP_LABELS = {"transcribe captions": "Checking YouTube captions against the speech",
               "transcribe clips": "Transcribing the exported clips with Whisper"}


def activity(state, stages):
    """What the user sees first: current operation, last success, progress."""
    status = state.get("status")
    current = stage_of(state.get("stage"))
    done = [s for s in STAGES if stages.get(s) == "done"]
    result = {"status": status, "current": None,
              "last_completed": STAGE_LABELS[done[-1]] if done else None,
              "progress": progress(state)}
    if status == "queued":
        result["current"] = "Waiting for its turn in the queue"
    elif status in ("running", "recording", "finalizing"):
        label = (STEP_LABELS.get(str(state.get("stage") or ""))
                 or STAGE_LABELS.get(current) or str(state.get("stage") or "Working"))
        info = result["progress"]
        if info and info.get("fraction") is not None and info.get("stage") == current:
            label += f" ({round(info['fraction'] * 100)}%"
            label += f", {info['detail']})" if info.get("detail") else ")"
        result["current"] = label
    return result


def status_label(state, busy):
    """What is really happening, in words, for the sidebar and header.

    Export and preview jobs run Whisper on their clips first; that step is
    labelled as part of the export/preview so it never looks like the whole
    video is being transcribed again. A saved "running" status without a live
    job is shown as stale instead of as running.
    """
    status = str(state.get("status") or "")
    stage = str(state.get("stage") or "")
    job = "Preview" if state.get("last_preview") else "Export"
    pick = [p for p in str(state.get("last_pick") or "").split(",") if p]
    progress = state.get("progress") or {}
    if status in ("queued", "running") and not busy:
        return "Stopped (no job is running; the saved status was stale)"
    if status == "queued":
        return f"Queued: {job.lower() if state.get('last_action') == 'render' else 'find clips'}"
    if status == "running":
        if stage == "transcribe clips":
            done, total = progress.get("done"), progress.get("total")
            count = f" {done}/{total}" if total else ""
            return f"{job}: Whisper word timing for {len(pick) or 'the'} clip(s){count}"
        match = re.match(r"(render|preview)(?: (\d+)/(\d+))?", stage)
        if match:
            if match.group(2):
                return f"{job}: rendering clip {match.group(2)} of {match.group(3)}"
            return f"{job}: preparing"
        return {"source": "Find clips: getting the video",
                "transcribe": "Find clips: transcribing",
                "transcribe captions": "Find clips: checking YouTube captions",
                "discover": "Find clips: finding moments",
                "select": "Find clips: AI judging"}.get(stage, f"Running: {stage}")
    return {"review": "Ready for review", "complete": "Exported",
            "failed": f"Failed at {stage}", "cancelled": "Cancelled",
            "interrupted": "Interrupted"}.get(status, status or "New")


def clip_counts(path, state, busy, selected=None):
    """Selected, queued, rendering, rendered and failed clip counts."""
    path = Path(path)
    stage = str(state.get("stage") or "")
    rendering = bool(busy and state.get("last_action") in ("render", "all")
                     and not state.get("last_preview"))
    pick = [p for p in str(state.get("last_pick") or "").split(",") if p]
    match = re.match(r"render (\d+)/(\d+)", stage)
    current = int(match.group(1)) if (rendering and match) else 0
    total = int(match.group(2)) if (rendering and match) else len(pick)
    return {
        "selected": selected,
        "queued": max(0, total - current) if rendering else 0,
        "rendering": 1 if current else 0,
        "rendered": len(list((path / "clips").glob("*.mp4"))) if (path / "clips").is_dir() else 0,
        "failed": len(state.get("render_errors") or []),
        "previews": len(list((path / "previews").glob("*.mp4"))) if (path / "previews").is_dir() else 0,
    }
