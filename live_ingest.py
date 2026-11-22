"""Live ingestion from authorized sources into bounded ring buffers (Phase 5).

Sources, in order of preference:

1. **OBS output folder** - OBS records to a folder (MKV, FLV, MPEG-TS or
   fragmented MP4: a plain MP4 cannot be read while it is being written).
   Chopify follows the newest recording as it grows (ffmpeg ``-follow``) and
   switches to a new file when OBS starts one.
2. **SRT** - ``srt://host:port?mode=listener`` (OBS/encoder pushes to
   Chopify) or ``mode=caller`` (Chopify pulls).
3. **Direct media URL** - an HLS/RTMP/HTTP(S) feed you are authorized to
   use. YouTube addresses are refused (``compliance``); yt-dlp is never used.

Each session cuts the input into ~6-second MPEG-TS segments (stream copy,
cut at keyframes) under ``<project>/live/segments`` and indexes them in
``<project>/live/index.db`` on a session timeline (seconds since the
session started). The ring buffer keeps ``buffer_seconds`` of recent
segments; older ones are deleted unless a *pin* (a potential clip plus
context) overlaps them. A disk watchdog shrinks the buffer when free space
drops below ``min_free_gb`` and stops the session at half of it; pinned
segments and final clips are never deleted by either.

Supervisor: when ffmpeg exits (network drop, OBS stopped, Studio crash) it
reconnects with backoff, continuing the segment numbering and timeline. A
session's state lives in project.json (``live``), so after a Studio restart
active sessions resume on their own.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import applog
import compliance
import projects

SEGMENT_SECONDS = 6
MAX_SESSIONS = 5
KINDS = ("obs_folder", "srt", "url")
OBS_EXTS = {".mkv", ".flv", ".ts", ".mp4", ".mov"}
DEFAULTS = {"buffer_seconds": 1800, "min_free_gb": 10.0, "min_keep_seconds": 60,
            "rw_timeout_seconds": 15, "max_backoff_seconds": 30}
SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    seq INTEGER PRIMARY KEY, file TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
    bytes INTEGER, added REAL, deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pins (
    id INTEGER PRIMARY KEY AUTOINCREMENT, start REAL NOT NULL, end REAL NOT NULL,
    reason TEXT, created REAL, released REAL
);
"""


class LiveError(ValueError):
    pass


# --------------------------------------------------------------- the ring

class Ring:
    """Segment index + pins for one session (SQLite next to the segments)."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.segments = self.folder / "segments"
        self.segments.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "index.db"
        self.lock = threading.Lock()
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def add(self, seq, file, start, end):
        try:
            size = (self.segments / file).stat().st_size
        except OSError:
            size = 0
        with self.lock, self._db() as db:
            db.execute("INSERT OR IGNORE INTO segments (seq, file, start, end, bytes, added) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (seq, file, start, end, size, time.time()))

    def last(self):
        with self._db() as db:
            row = db.execute("SELECT * FROM segments ORDER BY seq DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def live_segments(self):
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM segments WHERE deleted = 0 ORDER BY seq")]

    def range(self, start, end):
        """Kept segments overlapping [start, end)."""
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM segments WHERE deleted = 0 AND end > ? AND start < ? "
                "ORDER BY seq", (start, end))]

    def pin(self, start, end, reason=""):
        with self.lock, self._db() as db:
            return db.execute("INSERT INTO pins (start, end, reason, created) VALUES "
                              "(?, ?, ?, ?)", (max(0.0, start), end, reason,
                                               time.time())).lastrowid

    def unpin(self, pin_id):
        with self.lock, self._db() as db:
            db.execute("UPDATE pins SET released = ? WHERE id = ?", (time.time(), pin_id))

    def pins(self):
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM pins WHERE released IS NULL")]

    def _pinned(self, seg, pins):
        return any(seg["end"] > p["start"] and seg["start"] < p["end"] for p in pins)

    def trim(self, keep_seconds, min_keep_seconds=0):
        """Delete unpinned segments older than ``keep_seconds`` before the newest."""
        last = self.last()
        if not last:
            return 0, 0
        horizon = last["end"] - max(keep_seconds, min_keep_seconds)
        pins = self.pins()
        removed = freed = 0
        for seg in self.live_segments():
            if seg["end"] > horizon:
                break
            if self._pinned(seg, pins):
                continue
            target = self.segments / seg["file"]
            try:
                freed += target.stat().st_size
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                continue
            with self.lock, self._db() as db:
                db.execute("UPDATE segments SET deleted = 1 WHERE seq = ?", (seg["seq"],))
            removed += 1
        return removed, freed

    def stats(self):
        segs = self.live_segments()
        return {"segments": len(segs), "bytes": sum(s["bytes"] or 0 for s in segs),
                "buffered_seconds": round(sum(s["end"] - s["start"] for s in segs), 1),
                "newest": segs[-1]["end"] if segs else 0.0, "pins": len(self.pins())}

    def extract(self, start, end, out):
        """Join the segments covering [start, end) into ``out`` (stream copy).
        Returns (file, offset): the clip starts ``offset`` seconds into file."""
        segs = self.range(start, end)
        if not segs:
            raise LiveError("Those seconds are no longer in the buffer.")
        if segs[0]["start"] > start + SEGMENT_SECONDS or segs[-1]["end"] < end - 0.5:
            raise LiveError("Part of that moment is missing from the buffer.")
        out = Path(out)
        listing = out.with_suffix(".txt")
        listing.write_text("".join(f"file '{(self.segments / s['file']).as_posix()}'\n"
                                   for s in segs), encoding="utf-8")
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i",
                            str(listing), "-c", "copy", str(out)], check=True,
                           capture_output=True)
        finally:
            listing.unlink(missing_ok=True)
        return out, max(0.0, start - segs[0]["start"])


# --------------------------------------------------------------- helpers

def validate_source(kind, source):
    if kind not in KINDS:
        raise LiveError("Choose OBS folder, SRT or a direct media URL.")
    if not isinstance(source, str) or not source.strip():
        raise LiveError("Enter the source.")
    source = source.strip()
    if kind == "obs_folder":
        folder = Path(source).expanduser()
        if not folder.is_dir():
            raise LiveError(f"OBS output folder not found: {source}")
        return str(folder.resolve())
    if compliance.is_youtube(source):
        raise LiveError(compliance.MESSAGE)
    parsed = urlparse(source)
    allowed = {"srt"} if kind == "srt" else {"http", "https", "rtmp", "rtmps", "srt"}
    if parsed.scheme.lower() not in allowed or not parsed.hostname:
        raise LiveError("Use srt://host:port for SRT, or an http(s)/rtmp media address."
                        if kind == "srt" else "Use an http(s), rtmp(s) or srt media address.")
    return source


def newest_recording(folder, after=None):
    files = [p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in OBS_EXTS
             and not p.name.startswith(".")]
    files.sort(key=lambda p: p.stat().st_mtime)
    if after:
        files = [p for p in files if p.stat().st_mtime >= after - 1]
    return files[-1] if files else None


def quit_ffmpeg(child, timeout=10):
    """Ask ffmpeg to finish cleanly ('q' closes the current segment), then force."""
    if child is None or child.poll() is not None:
        return
    try:
        child.stdin.write(b"q")
        child.stdin.flush()
        child.wait(timeout=timeout)
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        try:
            child.kill()
            child.wait(5)
        except OSError:
            pass


def free_gb(path):
    try:
        return shutil.disk_usage(str(path)).free / 1e9
    except OSError:
        return None


# --------------------------------------------------------------- session

class Session:
    def __init__(self, project_path, info, settings=None, on_segment=None, free=free_gb,
                 clock=time.time):
        self.path = Path(project_path)
        self.info = info                      # persisted in project.json["live"]
        self.settings = {**DEFAULTS, **(settings or {}), **(info.get("settings") or {})}
        self.ring = Ring(self.path / "live")
        self.on_segment = on_segment
        self.free = free
        self.clock = clock
        self.stop_event = threading.Event()
        self.child = None
        self.thread = None
        self.lock = threading.Lock()

    @property
    def project_id(self):
        return self.path.name

    def _save(self, **changes):
        with self.lock:
            self.info.update(changes)
            info = dict(self.info)
        projects.update_project(self.path, lambda s: s.update(live=info))

    def elapsed(self):
        return max(0.0, self.clock() - float(self.info["started"]))

    def _command(self, list_file, start_number):
        kind, source = self.info["kind"], self.info["source"]
        cmd = ["ffmpeg", "-v", "error", "-y"]
        timeout = str(int(self.settings["rw_timeout_seconds"] * 1_000_000))
        if kind == "obs_folder":
            files = self.info.setdefault("files", {})
            current = self.info.get("file")
            position = float(files.get(current, 0.0)) if current else 0.0
            cmd += ["-follow", "1", "-rw_timeout", timeout]
            if position > 1:
                cmd += ["-ss", f"{position:.2f}"]
            cmd += ["-i", f"file:{current}"]
        elif kind == "srt":
            joiner = "&" if "?" in source else "?"
            cmd += ["-i", f"{source}{joiner}timeout={timeout}"]
        else:
            if source.lower().startswith("http"):
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
            cmd += ["-rw_timeout", timeout, "-i", source]
        cmd += ["-map", "0:v?", "-map", "0:a?", "-c", "copy", "-f", "segment",
                "-segment_time", str(SEGMENT_SECONDS), "-reset_timestamps", "1",
                "-segment_format", "mpegts", "-segment_list", str(list_file),
                "-segment_list_type", "csv", "-segment_list_flags", "+live",
                "-segment_start_number", str(start_number),
                str(self.ring.segments / "seg%08d.ts")]
        return cmd

    def _pick_obs_file(self):
        newest = newest_recording(self.info["source"])
        if newest is None:
            return False
        current = self.info.get("file")
        if current != str(newest):
            files = self.info.setdefault("files", {})
            files.setdefault(str(newest), 0.0)
            self._save(file=str(newest), files=files)
            applog.info("live.obs_file", f"Following OBS recording {newest.name}",
                        project_id=self.project_id)
        return True

    def start(self):
        self.thread = threading.Thread(target=self._supervise, daemon=True,
                                       name=f"live-{self.project_id}")
        self.thread.start()

    def stop(self, timeout=15):
        self.stop_event.set()
        quit_ffmpeg(self.child)
        if self.thread is not None:
            self.thread.join(timeout)
        self._save(state="stopped", stopped=self.clock(), message="Stopped.")

    def _supervise(self):
        backoff = 1.0
        self._save(state="active", message="Connecting…")
        while not self.stop_event.is_set():
            if self.info["kind"] == "obs_folder" and not self._pick_obs_file():
                self._save(message="Waiting for OBS to start recording in this folder.")
                if self.stop_event.wait(2):
                    break
                continue
            last = self.ring.last()
            start_number = (last["seq"] + 1) if last else 0
            list_file = self.path / "live" / f"list-{int(self.clock() * 1000)}.csv"
            offset = self.elapsed()
            connected_at = self.clock()
            log = (self.path / "live" / "ffmpeg.log").open("ab")
            try:
                self.child = subprocess.Popen(self._command(list_file, start_number),
                                              stdin=subprocess.PIPE,
                                              stdout=subprocess.DEVNULL, stderr=log)
            except OSError as exc:
                log.close()
                self._save(state="failed", message=f"Could not start ffmpeg: {exc}")
                return
            self._save(message="Receiving.", pid=self.child.pid)
            read = 0
            while True:
                code = self.child.poll()
                read = self._index(list_file, read, offset)
                problem = self._watchdog()
                if problem:
                    quit_ffmpeg(self.child)
                    self._index(list_file, read, offset)
                    log.close()
                    self._save(state="failed", message=problem)
                    applog.error("live.disk", problem, project_id=self.project_id)
                    return
                if code is not None or self.stop_event.wait(1.0):
                    break
            if self.child.poll() is None:
                self.child.wait(15)
            read = self._index(list_file, read, offset)
            log.close()
            if self.stop_event.is_set():
                break
            got = self.clock() - connected_at
            backoff = 1.0 if got > 20 else min(float(self.settings["max_backoff_seconds"]),
                                               backoff * 2)
            reconnects = int(self.info.get("reconnects", 0)) + 1
            self._save(message=f"Input ended; reconnecting in {backoff:.0f}s.",
                       reconnects=reconnects)
            applog.warning("live.reconnect", "Live input ended; reconnecting",
                           project_id=self.project_id, backoff=backoff, reconnects=reconnects)
            if self.stop_event.wait(backoff):
                break

    def _index(self, list_file, read, offset):
        """Add newly completed segments from ffmpeg's CSV list."""
        try:
            lines = list_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return read
        for line in lines[read:]:
            parts = line.split(",")
            if len(parts) < 3:
                continue
            name = parts[0].strip()
            match = re.search(r"(\d+)\.ts$", name)
            if not match:
                continue
            try:
                start, end = float(parts[1]), float(parts[2])
            except ValueError:
                continue
            seq = int(match.group(1))
            self.ring.add(seq, name, offset + start, offset + end)
            if self.info["kind"] == "obs_folder" and self.info.get("file"):
                files = self.info.setdefault("files", {})
                files[self.info["file"]] = float(files.get(self.info["file"], 0.0)) + (end - start)
            if self.on_segment:
                try:
                    self.on_segment(self, {"seq": seq, "file": name, "start": offset + start,
                                           "end": offset + end})
                except Exception as exc:     # noqa: BLE001 - clipping must not stop ingest
                    applog.warning("live.segment_hook", f"Segment hook failed: {exc}",
                                   project_id=self.project_id)
            read += 1
        if len(lines) > read:
            read = len(lines)
        stats = self.ring.stats()
        self._save(segments=stats["segments"], buffered_seconds=stats["buffered_seconds"],
                   newest=stats["newest"], last_segment_at=self.clock() if lines else
                   self.info.get("last_segment_at"))
        return read

    def _watchdog(self):
        self.ring.trim(float(self.settings["buffer_seconds"]),
                       float(self.settings["min_keep_seconds"]))
        floor = float(self.settings["min_free_gb"])
        free = self.free(self.path)
        if free is None or free >= floor:
            return None
        removed, _ = self.ring.trim(0, float(self.settings["min_keep_seconds"]))
        applog.warning("live.disk", f"Low disk: removed {removed} old buffer segments",
                       project_id=self.project_id, free_gb=round(free, 2))
        free = self.free(self.path)
        if free is not None and free < floor / 2:
            return (f"Stopped: only {free:.1f} GB free on disk. Pinned moments and clips "
                    "were kept.")
        return None

    def status(self):
        return {**{k: v for k, v in self.info.items() if k not in ("files",)},
                "project_id": self.project_id, **self.ring.stats()}


# --------------------------------------------------------------- manager

class LiveManager:
    """Start/stop sessions (max 5, shared with the scheduler's source slots)."""

    def __init__(self, root, scheduler=None, settings=None, on_segment=None, free=free_gb):
        self.root = Path(root)
        self.scheduler = scheduler
        self.settings = settings or {}
        self.on_segment = on_segment
        self.free = free
        self.sessions = {}
        self.lock = threading.RLock()

    def _claim_slot(self, project_id, title):
        if self.scheduler is None:
            if len(self.active()) >= MAX_SESSIONS:
                raise LiveError(f"{MAX_SESSIONS} live sessions are already running.")
            return
        import scheduler as scheduler_mod
        try:
            info = self.scheduler.submit(project_id, "live", [], label=title,
                                         meta={"open": True})
        except scheduler_mod.SchedulerError as exc:
            raise LiveError(str(exc)) from exc
        if info["state"] != "active":
            self.scheduler.cancel(project_id)
            raise LiveError(f"All {self.scheduler.settings['max_sources']} source slots are "
                            "busy. A live session cannot wait (the stream would be lost): "
                            "stop or cancel a source first.")

    def start(self, kind, source, title=None, settings=None):
        source = validate_source(kind, source)
        with self.lock:
            for session in self.sessions.values():
                if session.info.get("source") == source and session.info.get("state") == "active":
                    raise LiveError("A session for this source is already running.")
            label = title or (Path(source).name if kind == "obs_folder" else source)
            state = projects.create_project(self.root, source if kind != "obs_folder"
                                            else str(Path(source) / "live"), {})
            path = projects.project_path(self.root, state["id"])
            projects.update_project(path, lambda s: s.update(
                title=f"Live: {label}"[:120], status="recording", stage="live ingest"))
            try:
                self._claim_slot(state["id"], label)
            except LiveError:
                projects.update_project(path, lambda s: s.update(
                    status="failed", error="Could not start: no free source slot."))
                raise
            info = {"kind": kind, "source": source, "state": "active", "title": label,
                    "started": time.time(), "settings": settings or {}, "reconnects": 0}
            session = Session(path, info, self.settings, self.on_segment, self.free)
            self.sessions[state["id"]] = session
            session._save()
            session.start()
            applog.info("live.start", f"Live session started ({kind})",
                        project_id=state["id"], kind=kind)
            return session.status()

    def stop(self, project_id):
        with self.lock:
            session = self.sessions.get(project_id)
        if session is None:
            raise LiveError("That live session is not running.")
        session.stop()
        projects.update_project(session.path, lambda s: s.update(status="review",
                                                                 stage="live stopped"))
        if self.scheduler is not None:
            try:
                self.scheduler.close_source(project_id)
            except Exception:              # noqa: BLE001 - slot bookkeeping only
                pass
        applog.info("live.stop", "Live session stopped", project_id=project_id)
        return session.status()

    def active(self):
        return [s for s in self.sessions.values() if s.info.get("state") == "active"
                and not s.stop_event.is_set()]

    def status(self):
        return [s.status() for s in self.sessions.values()]

    def recover(self):
        """Resume sessions that were active when Studio stopped."""
        resumed = []
        for file in self.root.glob("*/project.json"):
            try:
                state = projects.read_project(file.parent)
            except (ValueError, OSError):
                continue
            info = state.get("live")
            if not isinstance(info, dict) or info.get("state") != "active":
                continue
            with self.lock:
                if file.parent.name in self.sessions:
                    continue
                if self.scheduler is not None and not self.scheduler.busy(file.parent.name):
                    try:
                        self._claim_slot(file.parent.name, state.get("title"))
                    except LiveError as exc:
                        info.update(state="failed", message=f"Not resumed: {exc}")
                        projects.update_project(file.parent, lambda s: s.update(live=info))
                        continue
                session = Session(file.parent, info, self.settings, self.on_segment, self.free)
                self.sessions[file.parent.name] = session
                session.start()
                resumed.append(file.parent.name)
                applog.info("live.recover", "Live session resumed after restart",
                            project_id=file.parent.name)
        return resumed

    def shutdown(self):
        """Studio is closing: stop ffmpeg but keep sessions 'active' so they resume."""
        for session in list(self.sessions.values()):
            session.stop_event.set()
            quit_ffmpeg(session.child)
        for session in list(self.sessions.values()):
            if session.thread is not None:
                session.thread.join(10)
