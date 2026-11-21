"""Probe sources and download only the media a long job needs.

Local files are never copied. Long URL sources download audio for discovery
(or a low-resolution video when there is no audio) and later download only the
selected video sections. Everything uses yt-dlp/ffprobe locally.
"""
import json
import re
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import subtitles

LONG_SECONDS = 2 * 60 * 60
LONG_BYTES = 8 * 1024**3
AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".ogg", ".wav", ".aac", ".flac", ".webm"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
MIN_FREE_BYTES = 500 * 1024**2
SECTION_TIMEOUT = 180
FULL_TIMEOUT = 60 * 60


class SectionDownloadTimeout(RuntimeError):
    """A ranged section download produced no usable file before timing out."""


# yt-dlp ``live_status`` values. ``post_live`` means the stream just ended and
# YouTube is still processing the replay.
LIVE_STATUSES = ("not_live", "is_live", "is_upcoming", "was_live", "post_live")
# Statuses that change over time, so a cached probe must not be trusted.
TRANSIENT_LIVE = {"is_live", "is_upcoming", "post_live"}
# Statuses the normal download pipeline cannot process yet.
BLOCKED_LIVE = TRANSIENT_LIVE
_UPCOMING_HINTS = ("will begin", "premieres in", "premiere will begin",
                   "scheduled for", "is_upcoming", "this live event")


def is_url(source):
    return urlparse(str(source)).scheme in {"http", "https"}


def live_status_from(data):
    """Normalize yt-dlp metadata to one of LIVE_STATUSES (or None if unknown)."""
    if not isinstance(data, dict) or not data:
        return None
    status = data.get("live_status")
    if status in LIVE_STATUSES:
        return status
    if data.get("is_live"):
        return "is_live"
    if data.get("was_live"):
        return "was_live"
    return "not_live"


def describe_source(meta):
    """Plain-language classification used by Studio and the CLI.

    Returns {"kind", "label", "message", "can_process", "can_record"}.
    """
    meta = meta or {}
    if not meta.get("url"):
        return {"kind": "local", "label": "Local video", "can_process": True,
                "can_record": False,
                "message": "Local file. It stays in place and is processed directly."}
    status = meta.get("live_status")
    if status == "is_live":
        return {"kind": "live", "label": "Live now", "can_process": False,
                "can_record": True,
                "message": "This stream is live. Record it, then Chopify "
                           "finds clips in the recording."}
    if status == "is_upcoming":
        when = meta.get("release_timestamp")
        text = "This stream has not started yet."
        if isinstance(when, (int, float)) and when > 0:
            import datetime
            text = ("This stream is scheduled for "
                    + datetime.datetime.fromtimestamp(when).strftime(
                        "%Y-%m-%d %H:%M") + " (local time).")
        return {"kind": "upcoming", "label": "Upcoming stream",
                "can_process": False, "can_record": True,
                "message": text + " Start recording and Chopify waits for it."}
    if status == "post_live":
        return {"kind": "processing", "label": "Stream just ended",
                "can_process": False, "can_record": False,
                "message": "The stream ended and YouTube is still processing "
                           "the replay. Try again later (usually minutes, "
                           "sometimes hours)."}
    if status == "was_live":
        return {"kind": "live_vod", "label": "Completed livestream",
                "can_process": True, "can_record": False,
                "message": "Finished livestream replay. It is processed like a "
                           "normal video."}
    if status == "not_live":
        return {"kind": "video", "label": "Video", "can_process": True,
                "can_record": False, "message": "Regular video."}
    reason = meta.get("probe_error")
    return {"kind": "unknown", "label": "Cannot read this video" if reason else "Unknown",
            "can_process": True, "can_record": False,
            "message": (f"YouTube/yt-dlp could not read this URL: {str(reason).rstrip('.')}. "
                        "Find clips will still try, but it will probably fail at "
                        "the download step." if reason else
                        "Could not read this URL's details. Processing will "
                        "still try to download it.")}


def live_block_message(meta):
    """Error text for a URL the processing pipeline cannot download yet."""
    info = describe_source(meta)
    if info["kind"] in {"live", "upcoming"}:
        return (f"{info['message']} Use Start recording in Studio, or run "
                "'python live.py URL' from the command line.")
    return info["message"]


def _kill_tree(proc):
    """Stop a downloader and any ffmpeg child it started."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True, check=False)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _run(cmd, timeout=None, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", True)
    if timeout is None:
        return subprocess.run(cmd, **kwargs)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(
            cmd, timeout, output=stdout, stderr=stderr)
    if kwargs.get("check", True) and proc.returncode:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _format_size(fmt, duration):
    if not isinstance(fmt, dict):
        return 0
    for key in ("filesize", "filesize_approx"):
        try:
            value = int(fmt.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    try:
        rate = float(fmt.get("tbr") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    if rate > 0 and duration:
        return int(rate * 1000 / 8 * float(duration))
    return 0


def _default_bitrate(height):
    if not height:
        return 4_000_000
    if height >= 1080:
        return 8_000_000
    if height >= 720:
        return 5_000_000
    if height >= 480:
        return 2_500_000
    return 1_500_000


def _probe_local(path, run=None):
    run = run or _run
    path = Path(path).expanduser().resolve()
    result = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size:stream=codec_type,width,height",
        "-of", "json", str(path)])
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"ffprobe returned invalid JSON for {path}") from exc
    streams = data.get("streams") or []
    video = next(
        (item for item in streams if item.get("codec_type") == "video"), {})
    size = int((data.get("format") or {}).get("size") or path.stat().st_size)
    duration = (data.get("format") or {}).get("duration")
    return {
        "source": str(path),
        "url": False,
        "title": path.name,
        "duration": float(duration) if duration is not None else None,
        "size": size,
        "estimated_size": size,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(item.get("codec_type") == "audio" for item in streams),
        "has_video": bool(video),
    }


def _probe_url(url, run=None):
    run = run or _run
    fallback = {
        "source": url, "url": True, "title": url, "duration": None,
        "size": None, "estimated_size": None, "width": 0, "height": 0,
        "has_audio": None, "has_video": None, "formats": [],
        "live_status": None, "is_live": False, "release_timestamp": None,
    }
    command = [sys.executable, "-m", "yt_dlp", "--dump-single-json",
               "--skip-download", "--no-playlist", url]
    try:
        result = run(command)
        data = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        # Upcoming streams have no formats yet, so yt-dlp exits with an error.
        # Ask again for metadata only; normal failures stay "unknown".
        # Keep yt-dlp's own reason ("This video is unavailable") so the user
        # is told why instead of a generic "unknown".
        failed = {**fallback, "probe_error": ytdlp_reason(exc)}
        data = _probe_without_formats(command, run)
        if data is None:
            stderr = str(getattr(exc, "stderr", "") or "").lower()
            if any(hint in stderr for hint in _UPCOMING_HINTS):
                return {**fallback, "live_status": "is_upcoming"}
            return failed
        status = live_status_from(data)
        if status not in TRANSIENT_LIVE:
            return failed
        return {**fallback, "title": str(data.get("title") or url),
                "live_status": status, "is_live": status == "is_live",
                "release_timestamp": data.get("release_timestamp")}

    duration = data.get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    formats = [item for item in (data.get("formats") or [])
               if isinstance(item, dict)]
    videos = [item for item in formats
              if item.get("vcodec") not in (None, "none")]
    audios = [item for item in formats
              if item.get("acodec") not in (None, "none")]
    video = max(
        videos,
        key=lambda item: (int(item.get("height") or 0),
                          float(item.get("tbr") or 0)),
        default=None)
    audio = max(
        audios,
        key=lambda item: float(item.get("tbr") or 0),
        default=None)
    estimated = 0
    if video is not None:
        estimated += _format_size(video, duration)
        if not estimated and duration:
            estimated = int(_default_bitrate(
                int(video.get("height") or 0)) * duration / 8)
        if audio is not None:
            estimated += _format_size(audio, duration)
    elif audio is not None:
        estimated = _format_size(audio, duration)
    elif duration:
        estimated = int(_default_bitrate(0) * duration / 8)

    return {
        "source": url,
        "url": True,
        "title": str(data.get("title") or url),
        "duration": duration,
        "size": None,
        "estimated_size": estimated or None,
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "has_audio": any(
            item.get("acodec") not in (None, "none") for item in formats),
        "has_video": bool(videos),
        "formats": formats,
        "live_status": live_status_from(data),
        "is_live": live_status_from(data) == "is_live",
        "release_timestamp": data.get("release_timestamp"),
        # Caption inventory for subtitle-first transcripts (no download).
        "captions": subtitles.track_list(data),
    }


def ytdlp_reason(exc):
    """yt-dlp's own error line from a failed run ("This video is unavailable")."""
    text = str(getattr(exc, "stderr", "") or getattr(exc, "output", "") or "")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    reason = next((l for l in reversed(lines) if l.startswith("ERROR")), "")
    reason = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[\w-]+:\s*)?", "", reason)
    return reason[:300] or None


def _probe_without_formats(command, run):
    """Metadata-only retry used to recognize upcoming streams."""
    try:
        result = run(command[:-1] + ["--ignore-no-formats-error", command[-1]])
        data = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) and data else None


def probe_source(source, runner=None):
    """Return local or yt-dlp metadata; URL failures become an unknown source."""
    source = str(source)
    run = runner or _run
    if is_url(source):
        return _probe_url(source, run)
    path = Path(source).expanduser()
    if not path.is_file():
        raise ValueError(f"Video file does not exist or URL is invalid: {source}")
    return _probe_local(path, run)


def is_long_source(meta, seconds=LONG_SECONDS, size_bytes=LONG_BYTES):
    """Duration, estimated full download, or unknown duration selects long."""
    duration = (meta or {}).get("duration")
    if duration is None:
        return True
    try:
        if float(duration) >= float(seconds):
            return True
    except (TypeError, ValueError):
        return True
    size = (meta or {}).get("estimated_size")
    try:
        return size is not None and float(size) >= float(size_bytes)
    except (TypeError, ValueError):
        return True


def _find_media(workdir, prefix):
    workdir = Path(workdir)
    for path in sorted(workdir.glob(prefix + ".*")):
        if path.suffix.lower() in AUDIO_EXTS | VIDEO_EXTS and path.is_file():
            return path.resolve()
    return None


def _remove_old(workdir, prefix):
    for path in Path(workdir).glob(prefix + ".*"):
        if path.suffix.lower() in AUDIO_EXTS | VIDEO_EXTS:
            try:
                path.unlink()
            except OSError:
                pass


def download_visual_discovery(url, workdir, runner=None):
    """Download the lowest-resolution video for speechless visual discovery."""
    run = runner or _run
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    _remove_old(workdir, "discovery")
    try:
        run([
            sys.executable, "-m", "yt_dlp",
            "-f", "worst[height<=360]/worst", "--no-playlist",
            "-o", str(workdir / "discovery.%(ext)s"), url])
    except (OSError, subprocess.SubprocessError) as exc:
        reason = ytdlp_reason(exc)
        raise RuntimeError(
            f"Download failed: {reason}" if reason
            else "Download failed: no video file produced.") from exc
    video = _find_media(workdir, "discovery")
    if video is None:
        raise RuntimeError("Download failed: no video file produced.")
    return video


def download_discovery(url, workdir, meta=None, runner=None):
    """Download best audio, or a low-resolution video for silent URLs."""
    run = runner or _run
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    meta = meta or probe_source(url, runner=run)
    _remove_old(workdir, "audio")
    _remove_old(workdir, "discovery")

    if meta.get("has_audio") is not False:
        try:
            run([
                sys.executable, "-m", "yt_dlp", "-f", "bestaudio/best",
                "--no-playlist", "-o", str(workdir / "audio.%(ext)s"), url])
        except (OSError, subprocess.SubprocessError):
            pass
        audio = _find_media(workdir, "audio")
        if audio is not None:
            return audio, "audio"

    return download_visual_discovery(url, workdir, runner=run), "video"


def _download(url, out_path, fmt, runner=None, timeout=None, extra=()):
    """Run one yt-dlp download, converting a timeout into a clear error."""
    run = runner or _run
    cmd = [sys.executable, "-m", "yt_dlp", "-f", fmt,
           "--merge-output-format", "mp4", "--no-playlist",
           "--force-overwrites", *extra, "-o", str(out_path), url]
    try:
        if runner is None:
            run(cmd, timeout=timeout)
        else:
            run(cmd)
    except subprocess.TimeoutExpired as exc:
        raise SectionDownloadTimeout(
            f"Downloader stalled for {timeout} seconds.") from exc
    if not out_path.is_file() or out_path.stat().st_size <= 0:
        raise RuntimeError(f"Download produced no file: {out_path.name}")
    return out_path


def download_section(url, start, end, out_path, quality=1080, runner=None,
                     timeout=SECTION_TIMEOUT):
    """Download one accurate video section with a bounded wait."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (out_path, out_path.with_suffix(out_path.suffix + ".part")):
        try:
            stale.unlink()
        except OSError:
            pass
    if quality:
        fmt = (f"bestvideo[height<={int(quality)}]+bestaudio/"
               f"best[height<={int(quality)}]")
    else:
        fmt = "worst"
    try:
        return _download(
            url, out_path, fmt, runner=runner, timeout=timeout,
            extra=("--download-sections",
                   f"*{float(start):.3f}-{float(end):.3f}",
                   "--force-keyframes-at-cuts"))
    except SectionDownloadTimeout as exc:
        for stale in (out_path, out_path.with_suffix(out_path.suffix + ".part")):
            try:
                stale.unlink()
            except OSError:
                pass
        raise SectionDownloadTimeout(
            f"Section download stalled for {float(start):.0f}-"
            f"{float(end):.0f}s. The source may not support ranged "
            "downloads.") from exc


def download_full(url, out_path, quality=1080, runner=None,
                  timeout=FULL_TIMEOUT):
    """Download a complete source for sources where ranged cuts stall."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (out_path, out_path.with_suffix(out_path.suffix + ".part")):
        try:
            stale.unlink()
        except OSError:
            pass
    if quality:
        q = int(quality)
        fmt = (f"bestvideo[height<={q}][protocol=https][vcodec^=avc1]+"
               f"bestaudio[protocol=https][ext=m4a]/"
               f"bestvideo[height<={q}][protocol=https]+"
               f"bestaudio[protocol=https]/best[height<={q}]")
    else:
        fmt = "worst"
    return _download(url, out_path, fmt, runner=runner, timeout=timeout)


def check_disk(path, required_bytes):
    """Warn below 1.5x the estimate; stop only under 500 MB free."""
    free = shutil.disk_usage(str(path)).free
    if free < MIN_FREE_BYTES:
        raise ValueError(
            f"Not enough free disk space: {free / 1024**2:.0f} MB free.")
    if required_bytes and free < 1.5 * float(required_bytes):
        print(f"Disk warning: {free / 1024**3:.1f} GB free, "
              f"estimated job size {float(required_bytes) / 1024**3:.1f} GB.",
              flush=True)
