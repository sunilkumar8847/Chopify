"""Record YouTube livestreams to crash-safe parts, then hand off to processing.

How a recording works (all files live inside the project folder):

1. ``waiting``    - the stream has not started; poll its status.
2. ``recording``  - yt-dlp writes ``recording/partNNN.ts``. MPEG-TS can be
                    played up to the last byte written, so a crash, power cut or
                    forced stop never corrupts what was already recorded. A
                    network drop ends the part; if the stream is still live a
                    new part starts after a back-off.
3. ``finalizing`` - the parts are joined into ``recording.mp4`` (no re-encode
                    when the parts match) and the project source switches to
                    that local file, so the normal pipeline takes over.
4. ``recorded``   - done. ``failed`` / ``interrupted`` keep every part so the
                    user can Resume (record more) or Finalize (use what exists).

On Windows the downloader runs in its own process group and inside a Job
Object: Stop sends Ctrl+Break to that group, and if anything is still alive
after the grace period the whole job (yt-dlp and its ffmpeg) is terminated.
The Job Object also kills the recorder if Studio itself crashes, so no
orphaned downloads keep writing in the background.

Command line (manual use / testing):
    python live.py "https://www.youtube.com/@channel/live"
    python live.py --project projects/abc123def456 --finalize
"""
import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import ingest
import projects
import ytdlp

RECORDING_DIR = "recording"
OUTPUT_STEM = "recording"
PART_EXTS = (".ts", ".mkv", ".mp4", ".webm")
ACTIVE_STATES = {"waiting", "recording", "stopping", "finalizing"}
AFTER_CHOICES = ("prepare", "all", "none")

DEFAULT_SETTINGS = {
    "from_start": False,          # experimental: --live-from-start (DVR)
    "max_duration": 12 * 3600,    # seconds of recording before a safe stop
    "max_height": 1080,
    "min_free_gb": 2.0,           # stop safely when the drive gets this low
    "max_wait": 24 * 3600,        # how long to wait for an upcoming stream
    "max_retries": 10,            # reconnects in a row before giving up
    "stall_timeout": 180,         # seconds without new bytes = connection lost
    "after": "prepare",           # what to run after recording
    "keep_parts": False,          # keep partNNN files after a verified join
}
POLL_SECONDS = 2.0
WAIT_POLL_SECONDS = 60.0
SAVE_SECONDS = 5.0
STOP_GRACE = 20.0
HEALTHY_PART_SECONDS = 300.0


class RecordingError(RuntimeError):
    """A recording could not start or could not be finalized."""


# ------------------------------------------------------------- settings

def validate_settings(settings=None):
    """Return complete, validated recording settings."""
    settings = dict(settings or {})
    unknown = set(settings) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ValueError(f"Unknown recording settings: {', '.join(sorted(unknown))}")
    result = {**DEFAULT_SETTINGS, **settings}
    for key in ("from_start", "keep_parts"):
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} must be true or false.")

    def number(key, low, high, integer=False):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number.")
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}.")
        result[key] = int(value) if integer else float(value)

    number("max_duration", 60, 48 * 3600, integer=True)
    number("max_height", 144, 4320, integer=True)
    number("min_free_gb", 0.5, 1000)
    number("max_wait", 0, 7 * 24 * 3600, integer=True)
    number("max_retries", 0, 100, integer=True)
    number("stall_timeout", 30, 3600, integer=True)
    if result["after"] not in AFTER_CHOICES:
        raise ValueError("after must be prepare, all or none.")
    return result


def new_recording(url, settings=None):
    """Initial ``recording`` block stored in project.json."""
    return {"state": "new", "url": url, "settings": validate_settings(settings),
            "parts": [], "bytes": 0, "seconds": 0.0, "retries": 0,
            "message": "", "error": None, "stop_reason": None,
            "live_status": None, "output": None, "started_at": None,
            "ended_at": None, "pid": None}


def recording_dir(project_dir):
    return Path(project_dir) / RECORDING_DIR


# From-start mode downloads video and audio separately (part001.f399.mkv +
# part001.f140.mkv) and yt-dlp only merges them when it exits normally. A
# stopped from-start recording therefore leaves these per-stream files;
# they are real media and Finalize merges them. In-progress fragments
# ("-Frag12") and resume state (".ytdl") are not media.
_STREAM_FILE = re.compile(r"^part\d{3}\.f[0-9A-Za-z_-]+\.(mkv|mp4|webm|m4a)$",
                          re.IGNORECASE)


def part_groups(project_dir):
    """Recorded parts, oldest first, as lists of files.

    A complete part is ``[partNNN.ext]``. A from-start part that was stopped
    before yt-dlp merged it is ``[partNNN.fVIDEO.ext, partNNN.fAUDIO.ext]``.
    """
    directory = recording_dir(project_dir)
    if not directory.is_dir():
        return []
    groups = {}
    for path in sorted(directory.glob("part[0-9][0-9][0-9].*")):
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        group = groups.setdefault(path.name[:7], {"complete": None, "streams": []})
        if path.name.count(".") == 1 and path.suffix.lower() in PART_EXTS:
            group["complete"] = path
        elif _STREAM_FILE.match(path.name):
            group["streams"].append(path)
    result = []
    for stem in sorted(groups):
        group = groups[stem]
        if group["complete"] is not None:
            result.append([group["complete"]])
        elif group["streams"]:
            result.append(sorted(group["streams"]))
    return result


def part_files(project_dir):
    """Every recorded media file (complete parts and unmerged streams)."""
    return [path for group in part_groups(project_dir) for path in group]


def _part_bytes(directory, stem):
    total = 0
    for path in Path(directory).glob(stem + "*"):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def build_command(url, out_stem, settings):
    """yt-dlp command for one recording part (``out_stem`` has no suffix)."""
    height = int(settings["max_height"])
    common = [*ytdlp.base(), "--no-playlist", "--no-part",
              "--newline", "--retries", "10", "--fragment-retries", "10"]
    if settings["from_start"]:
        # DASH fragments from the start of the DVR window, merged at the end.
        # Prefer H.264 + AAC: YouTube otherwise picks AV1, which is slower to
        # decode during clip detection and rendering.
        return common + [
            "--live-from-start",
            "-f", (f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]/"
                   f"bv*[height<={height}]+ba/b[height<={height}]/b"),
            "--merge-output-format", "mkv",
            "-o", str(out_stem) + ".%(ext)s", url]
    # Recording from now. YouTube serves live video and audio as separate HLS
    # streams, so ask for both; ffmpeg merges them live into MPEG-TS
    # (--hls-use-mpegts). yt-dlp names the file .mp4 but the content is
    # MPEG-TS, which stays readable even if the process is killed mid-write.
    return common + [
        "--hls-use-mpegts",
        "-f", f"bv*[height<={height}]+ba/b[height<={height}]/b",
        "-o", str(out_stem) + ".%(ext)s", url]


# yt-dlp errors that retrying cannot fix.
FATAL_PATTERNS = (
    ("requested format is not available",
     "YouTube offered no recordable format for this stream."),
    ("members-only", "This stream is for channel members only."),
    ("join this channel", "This stream is for channel members only."),
    ("private video", "This stream is private."),
    ("sign in to confirm", "YouTube asks to sign in (age or bot check)."),
    ("video unavailable", "This stream is unavailable."),
    ("this video is unavailable", "This stream is unavailable."),
)


def fatal_error(log_text):
    """Plain-language reason if a downloader log shows a permanent error."""
    low = (log_text or "").lower()
    for pattern, message in FATAL_PATTERNS:
        if pattern in low:
            return message
    return None


# ------------------------------------------------------- process control

class _WindowsJob:
    """A Windows Job Object that kills every process in it when closed."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k = self._kernel32
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(info),
                                         ctypes.sizeof(info)):
            self.close()
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, process):
        if not self._kernel32.AssignProcessToJobObject(
                self.handle, int(process._handle)):
            raise OSError(self._ctypes.get_last_error(),
                          "AssignProcessToJobObject failed")

    def terminate(self):
        if self.handle:
            self._kernel32.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


class ProcessGroup:
    """A child process plus everything it starts, stoppable as one unit."""

    def __init__(self, cmd, log=None, cwd=None):
        kwargs = {"stdin": subprocess.DEVNULL,
                  "stdout": log if log is not None else subprocess.DEVNULL,
                  "stderr": subprocess.STDOUT, "cwd": cwd}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(cmd, **kwargs)
        self.pid = self.process.pid
        self.job = None
        if os.name == "nt":
            try:
                self.job = _WindowsJob()
                self.job.assign(self.process)
            except OSError:
                if self.job is not None:
                    self.job.close()
                self.job = None  # fall back to taskkill /T

    @property
    def returncode(self):
        return self.process.returncode

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        return self.process.wait(timeout)

    def interrupt(self):
        """Ask politely: Ctrl+Break on Windows, SIGINT elsewhere."""
        if self.poll() is not None:
            return
        try:
            if os.name == "nt":
                os.kill(self.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(self.pid, signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass

    def kill(self):
        """Force-stop the child and all of its descendants."""
        if os.name == "nt":
            if self.job is not None:
                self.job.terminate()
            else:
                ingest._kill_tree(self.process)
        else:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            pass

    def stop(self, grace=STOP_GRACE):
        """Interrupt, wait up to ``grace`` seconds, then force-stop."""
        self.interrupt()
        try:
            self.process.wait(grace)
        except subprocess.TimeoutExpired:
            pass
        self.kill()
        self.close()
        return self.process.returncode

    def close(self):
        if self.job is not None:
            # Closing the job kills anything still inside it (KILL_ON_CLOSE).
            self.job.close()
            self.job = None


def spawn(cmd, log=None, cwd=None):
    return ProcessGroup(cmd, log=log, cwd=cwd)


def pid_alive(pid):
    """True while a process with this PID exists."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                                ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------- finalize

def _ffprobe(path, runner):
    """(duration, (codec, width, height)) for a media file, or (None, None)."""
    try:
        result = runner([
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of", "json", str(path)])
        import json
        data = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    video = next((s for s in data.get("streams") or []
                  if s.get("codec_type") == "video"), {})
    shape = (video.get("codec_name"), video.get("width"), video.get("height"))
    return duration, shape


def _concat_line(path):
    return "file '" + path.resolve().as_posix().replace("'", "'\\''") + "'\n"


def finalize_parts(project_dir, keep_parts=False, runner=None):
    """Join recorded parts into ``recording.mp4``. Returns (path, seconds).

    The result is written to a temporary name and only replaces the final
    file after ffprobe confirms it holds (almost) all recorded time, so an
    interruption here is harmless: the parts stay until the join is verified.
    """
    run = runner or ingest._run
    project_dir = Path(project_dir)
    usable, total, shapes, originals, temps = [], 0.0, set(), [], []
    for group in part_groups(project_dir):
        if len(group) == 1:
            source = group[0]
        else:
            source = _mux_streams(group, run)
            if source is None:
                continue
            temps.append(source)
        duration, shape = _ffprobe(source, run)
        if duration and duration > 0:
            usable.append(source)
            originals.extend(group)
            total += duration
            shapes.add(shape)
    if not usable:
        raise RecordingError("No playable media was recorded.")
    listing = recording_dir(project_dir) / "concat.txt"
    listing.write_text("".join(_concat_line(p) for p in usable), encoding="utf-8")
    base = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", str(listing), "-map", "0:v?", "-map", "0:a?", "-dn", "-sn"]
    if len(shapes) > 1:
        # A reconnect picked a different quality: re-encode to one shape.
        width = max(int(s[1] or 0) for s in shapes) or 1280
        height = max(int(s[2] or 0) for s in shapes) or 720
        codec = ["-vf", (f"scale={width}:{height}:force_original_aspect_ratio="
                         f"decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-c:a", "aac", "-b:a", "160k"]
    else:
        codec = ["-c", "copy"]
    attempts = [(".mp4", ["-movflags", "+faststart", "-f", "mp4"]),
                (".mkv", ["-f", "matroska"])]
    output = None
    last_error = None
    for suffix, container in attempts:
        temp = project_dir / f"{OUTPUT_STEM}.tmp{suffix}"
        try:
            run(base + codec + container + [str(temp)])
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = exc
            try:
                temp.unlink()
            except OSError:
                pass
            continue
        duration, _ = _ffprobe(temp, run)
        if duration is None or duration < total * 0.9 - 2.0:
            last_error = RecordingError(
                f"Joined recording is too short ({duration or 0:.0f}s of "
                f"{total:.0f}s).")
            try:
                temp.unlink()
            except OSError:
                pass
            continue
        output = project_dir / f"{OUTPUT_STEM}{suffix}"
        os.replace(temp, output)
        break
    for temp in temps:  # merged copies of unmerged parts; originals remain
        try:
            temp.unlink()
        except OSError:
            pass
    if output is None:
        raise RecordingError(f"Could not join the recording: {last_error}")
    try:
        listing.unlink()
    except OSError:
        pass
    if not keep_parts:
        stems = {path.name[:7] for path in originals}
        for path in list(recording_dir(project_dir).iterdir()):
            # The verified parts plus yt-dlp leftovers (-FragN, .ytdl) of them.
            if path.is_file() and (path in originals or (
                    path.name[:7] in stems and (
                        ".ytdl" in path.name or "-Frag" in path.name))):
                try:
                    path.unlink()
                except OSError:
                    pass
    return output, round(total, 2)


def _mux_streams(files, run):
    """Merge a stopped from-start part's separate video and audio files.

    Stream copy only (no re-encode); the result is cut to the shorter stream
    so picture and sound stay in sync. Returns the merged file or None.
    """
    video = audio = None
    for path in files:
        _, shape = _ffprobe(path, run)
        if shape and shape[0]:
            video = video or path
        else:
            audio = audio or path
    if video is None and audio is None:
        return None
    if video is None or audio is None:
        return video or audio
    out = video.parent / f"merged_{video.name[:7]}.mkv"
    try:
        run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(audio),
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest",
             "-f", "matroska", str(out)])
    except (OSError, subprocess.SubprocessError):
        try:
            out.unlink()
        except OSError:
            pass
        return video
    return out


# --------------------------------------------------------------- recorder

def _backoff(attempt):
    return float(min(300, 5 * 2 ** max(0, attempt - 1)))


STOP_MESSAGES = {
    "stopped": "Stopped by you.",
    "max_duration": "Reached the maximum recording length.",
    "disk": "Stopped safely: free disk space is low.",
    "ended": "The stream ended.",
    "retries_exhausted": "Gave up reconnecting to the stream.",
    "timeout": "The stream did not start in time.",
    "unreachable": "Could not read the stream status.",
    "fatal": "The downloader hit an error that retrying cannot fix.",
}


class Recorder:
    """Drive one project's recording. Every dependency can be replaced in tests.

    ``stop_event``: finish gracefully and finalize what was recorded.
    ``abort_event``: Studio is closing; stop now and mark ``interrupted``.
    """

    def __init__(self, project_dir, stop_event=None, abort_event=None,
                 spawn=spawn, probe=None, finalize=finalize_parts,
                 free_bytes=None, clock=time.monotonic, wall=time.time,
                 wait=None, poll_seconds=POLL_SECONDS,
                 wait_poll_seconds=WAIT_POLL_SECONDS, stop_grace=STOP_GRACE):
        self.path = Path(project_dir)
        self.stop_event = stop_event or threading.Event()
        self.abort_event = abort_event or threading.Event()
        self.spawn = spawn
        self.probe = probe or ingest.probe_source
        self.finalize = finalize
        self.free_bytes = free_bytes or (
            lambda: shutil.disk_usage(str(self.path)).free)
        self.clock = clock
        self.wall = wall
        self._wait = wait
        self.poll_seconds = poll_seconds
        self.wait_poll_seconds = wait_poll_seconds
        self.stop_grace = stop_grace
        self._fatal_message = None

    # -- helpers
    def wait(self, seconds):
        """Sleep, returning True early if a stop or abort was requested."""
        if self._wait is not None:
            self._wait(seconds)
            return self.stopping()
        deadline = self.clock() + seconds
        while not self.stopping():
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            self.stop_event.wait(min(remaining, 0.5))
        return True

    def stopping(self):
        return self.stop_event.is_set() or self.abort_event.is_set()

    def recording(self):
        return projects.read_project(self.path)["recording"]

    def update(self, status=None, **fields):
        def mutate(state):
            state["recording"].update(fields, updated_at=self.wall())
            if status:
                state["status"] = status
                state["stage"] = "recording" if status == "recording" else status
        return projects.update_project(self.path, mutate)["recording"]

    def low_disk(self, settings):
        return self.free_bytes() < float(settings["min_free_gb"]) * 1024**3

    # -- main entry
    def run(self):
        rec = self.recording()
        settings = validate_settings(rec.get("settings"))
        self.update(status="recording", state="waiting", error=None,
                    stop_reason=None, started_at=rec.get("started_at") or self.wall(),
                    message="Checking the stream...", settings=settings)
        try:
            outcome = self._wait_until_live(rec["url"], settings)
            if outcome == "live":
                outcome = self._record_loop(rec["url"], settings)
        except Exception as exc:  # unexpected: keep parts, report clearly
            return self._fail(f"Recording stopped unexpectedly: {exc}")
        if self.abort_event.is_set():
            self.update(status="interrupted", state="interrupted", pid=None,
                        stop_reason="interrupted",
                        message="Studio closed during recording. Resume to "
                                "record more, or Finalize to use what exists.")
            return "interrupted"
        if part_groups(self.path):
            return self._finalize(outcome, settings)
        if int(self.recording().get("bytes") or 0) > 0:
            # Bytes arrived but no usable media file is left: never claim
            # that nothing was recorded.
            return self._fail("Data was downloaded but no playable media file "
                              "was found. See recording.log.",
                              stop_reason=outcome)
        if outcome == "stopped":
            self.update(status="new", state="stopped", pid=None,
                        stop_reason="stopped",
                        message="Stopped before anything was recorded.")
            return "stopped"
        rec = self.recording()
        reasons = {
            "ended": ingest.describe_source(
                {"url": True, "live_status": rec.get("live_status")})["message"],
            "timeout": STOP_MESSAGES["timeout"],
            "unreachable": STOP_MESSAGES["unreachable"],
            "disk": STOP_MESSAGES["disk"],
            "retries_exhausted": STOP_MESSAGES["retries_exhausted"],
            "fatal": rec.get("error") or STOP_MESSAGES["fatal"],
        }
        if rec.get("live_status") in {"not_live", "was_live"}:
            reasons["ended"] = ("This is not a live stream right now. Use Find "
                                "clips to process it as a normal video.")
        return self._fail(reasons.get(outcome, STOP_MESSAGES.get(outcome, outcome)),
                          stop_reason=outcome)

    def _fail(self, message, stop_reason=None):
        self.update(status="failed", state="failed", pid=None, error=message,
                    message=message, stop_reason=stop_reason,
                    ended_at=self.wall())
        projects.update_project(self.path, lambda s: s.update(error=message))
        return "failed"

    def _wait_until_live(self, url, settings):
        started = self.clock()
        failures = 0
        while True:
            if self.stopping():
                return "stopped"
            meta = self.probe(url)
            status = meta.get("live_status")
            self.update(live_status=status, title=meta.get("title"))
            if status == "is_live":
                return "live"
            if status == "is_upcoming":
                info = ingest.describe_source(meta)
                self.update(state="waiting", message=info["message"].replace(
                    " Start recording and Chopify waits for it.",
                    " Waiting for it to start..."))
                if self.clock() - started >= settings["max_wait"]:
                    return "timeout"
                if self.wait(self.wait_poll_seconds):
                    return "stopped"
                continue
            if status in {"was_live", "post_live", "not_live"}:
                return "ended"
            failures += 1
            if failures > settings["max_retries"]:
                return "unreachable"
            self.update(message=f"Could not read the stream status; retrying "
                                f"({failures}/{settings['max_retries']})...")
            if self.wait(_backoff(failures)):
                return "stopped"

    def _record_loop(self, url, settings):
        directory = recording_dir(self.path)
        directory.mkdir(parents=True, exist_ok=True)
        retries = 0
        while True:
            rec = self.recording()
            if self.stopping():
                return "stopped"
            if float(rec.get("seconds") or 0) >= settings["max_duration"]:
                return "max_duration"
            if self.low_disk(settings):
                return "disk"
            index = len(rec.get("parts") or []) + 1
            stem = directory / f"part{index:03d}"
            reason, part_seconds = self._record_part(url, stem, settings, rec)
            if reason in {"stopped", "max_duration", "disk"}:
                return reason
            fatal = self._fatal_message
            if fatal:
                self.update(error=fatal, message=fatal)
                return "fatal"
            if part_seconds >= HEALTHY_PART_SECONDS:
                retries = 0
            # The part ended by itself or stalled: is the stream still live?
            if self.stopping():
                return "stopped"
            status = self.probe(url).get("live_status")
            self.update(live_status=status)
            if status in {"was_live", "post_live", "not_live"}:
                return "ended"
            retries += 1
            if retries > settings["max_retries"]:
                return "retries_exhausted"
            self.update(retries=int(rec.get("retries") or 0) + 1,
                        message=f"Connection lost; reconnecting "
                                f"({retries}/{settings['max_retries']})...")
            if self.wait(_backoff(retries)):
                return "stopped"

    def _record_part(self, url, stem, settings, rec):
        log_path = self.path / "recording.log"
        parts = list(rec.get("parts") or [])
        base_seconds = float(rec.get("seconds") or 0)
        base_bytes = int(rec.get("bytes") or 0)
        part = {"file": stem.name, "started": self.wall(), "ended": None,
                "bytes": 0, "seconds": 0.0, "exit": None, "reason": None}
        self._fatal_message = None
        # Only the first part may start from the beginning of the stream. A
        # reconnect or Resume continues from "now"; downloading from the start
        # again would duplicate everything already recorded.
        part_settings = dict(settings)
        if parts:
            part_settings["from_start"] = False
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                      f"{stem.name} ===\n")
            log.flush()
            log_start = log.tell()
            try:
                handle = self.spawn(build_command(url, stem, part_settings), log,
                                    str(self.path))
            except OSError as exc:
                raise RecordingError(f"Could not start the downloader: {exc}")
            parts.append(part)
            self.update(state="recording", pid=getattr(handle, "pid", None),
                        parts=parts, message="Recording...")
            started = self.clock()
            last_size, last_growth, last_save = 0, started, 0.0
            first_byte = None  # recorded time only counts once media arrives
            reason = None
            while True:
                code = handle.poll()
                now = self.clock()
                size = _part_bytes(stem.parent, stem.name)
                if size > last_size:
                    last_size, last_growth = size, now
                    if first_byte is None:
                        first_byte = now
                seconds = base_seconds + (now - first_byte if first_byte else 0.0)
                if code is not None:
                    reason = "exited"
                elif self.stopping():
                    reason = "stopped"
                elif seconds >= settings["max_duration"]:
                    reason = "max_duration"
                elif self.low_disk(settings):
                    reason = "disk"
                elif now - last_growth > settings["stall_timeout"]:
                    reason = "stalled"
                if reason:
                    break
                if now - last_save >= SAVE_SECONDS:
                    part.update(bytes=max(0, size),
                                seconds=round(now - started, 1))
                    self.update(bytes=base_bytes + max(0, size),
                                seconds=round(seconds, 1), parts=parts)
                    last_save = now
                self.wait(self.poll_seconds)
            if code is None:
                self.update(state="stopping", message="Stopping safely...")
                grace = 5.0 if self.abort_event.is_set() else self.stop_grace
                code = handle.stop(grace)
            elif hasattr(handle, "close"):
                handle.close()
        # Bytes downloaded = the most ever seen (files may shrink or vanish).
        size = max(0, last_size, _part_bytes(stem.parent, stem.name))
        part_seconds = round(self.clock() - first_byte, 1) if first_byte else 0.0
        if reason == "exited" and code and not size:
            try:
                with log_path.open("r", encoding="utf-8", errors="replace") as log:
                    log.seek(log_start)
                    self._fatal_message = fatal_error(log.read())
            except OSError:
                self._fatal_message = None
        part.update(ended=self.wall(), bytes=size, seconds=part_seconds,
                    exit=code, reason=reason)
        self.update(parts=parts, pid=None, bytes=base_bytes + size,
                    seconds=round(base_seconds + part_seconds, 1))
        return reason, part_seconds

    def _finalize(self, reason, settings):
        self.update(state="finalizing", message="Joining the recording...",
                    stop_reason=reason)
        try:
            output, seconds = self.finalize(
                self.path, keep_parts=settings["keep_parts"])
        except Exception as exc:
            return self._fail(f"{exc} The recorded parts are kept in the "
                              f"'{RECORDING_DIR}' folder; use Finalize to retry.",
                              stop_reason=reason)
        message = STOP_MESSAGES.get(reason, "Recording finished.")

        def handoff(state):
            rec = state["recording"]
            state["source_url"] = rec.get("url")
            state["source"] = str(Path(output).resolve())
            state["status"] = "new"
            state["stage"] = "recorded"
            state["error"] = None
            rec.update(state="recorded", output=str(output), seconds=seconds,
                       pid=None, stop_reason=reason, ended_at=self.wall(),
                       error=None, message=f"{message} Saved {Path(output).name}.")
        projects.update_project(self.path, handoff)
        return "recorded"


def finalize_existing(project_dir, finalize=finalize_parts):
    """Finalize an interrupted or failed recording without recording more."""
    recorder = Recorder(project_dir, finalize=finalize)
    rec = recorder.recording()
    settings = validate_settings(rec.get("settings"))
    if not part_groups(project_dir):
        raise RecordingError("There is no recorded media to finalize.")
    return recorder._finalize(rec.get("stop_reason") or "stopped", settings)


def mark_interrupted(state):
    """Studio startup: an active recording cannot still be running."""
    rec = state.get("recording")
    if isinstance(rec, dict) and rec.get("state") in ACTIVE_STATES:
        rec.update(state="interrupted", pid=None,
                   message="Studio stopped during recording. Resume to record "
                           "more, or Finalize to use what was recorded.")
        return True
    return False


def start_recording_project(root, url, settings=None, options=None):
    """Create a project whose first stage is a live recording."""
    state = projects.create_project(root, url, options or {})
    path = projects.project_path(root, state["id"])
    return projects.update_project(path, lambda s: s.update(
        recording=new_recording(url, settings), stage="ready to record"))


# -------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Record a YouTube livestream into a Chopify project.")
    ap.add_argument("url", nargs="?", help="live or upcoming stream URL")
    ap.add_argument("--project", help="existing project folder (resume/finalize)")
    ap.add_argument("--projects-dir", default="projects")
    ap.add_argument("--from-start", action="store_true",
                    help="experimental: record from the start of the DVR window")
    ap.add_argument("--max-hours", type=float, default=12.0)
    ap.add_argument("--min-free-gb", type=float, default=2.0)
    ap.add_argument("--after", choices=AFTER_CHOICES, default="prepare")
    ap.add_argument("--finalize", action="store_true",
                    help="join what was already recorded and stop")
    args = ap.parse_args(argv)
    if args.project:
        path = Path(args.project).resolve()
        state = projects.read_project(path)
        if not isinstance(state.get("recording"), dict):
            ap.error("that project has no recording")
    elif args.url:
        settings = {"from_start": args.from_start,
                    "max_duration": int(args.max_hours * 3600),
                    "min_free_gb": args.min_free_gb, "after": args.after}
        state = start_recording_project(
            Path(args.projects_dir).resolve(), args.url, settings)
        path = projects.project_path(Path(args.projects_dir).resolve(), state["id"])
    else:
        ap.error("give a stream URL or --project")
    if args.finalize:
        result = finalize_existing(path)
    else:
        stop = threading.Event()
        box = {}
        worker = threading.Thread(
            target=lambda: box.update(result=Recorder(path, stop).run()))
        print(f"Recording into {path}\nPress Ctrl+C to stop and keep the "
              "recording.", flush=True)
        worker.start()
        while worker.is_alive():
            try:
                worker.join(1.0)
                rec = projects.read_project(path)["recording"]
                print(f"\r{rec['state']:<11} {float(rec.get('seconds') or 0) / 60:6.1f} "
                      f"min  {int(rec.get('bytes') or 0) / 1024**2:8.1f} MB  "
                      f"{rec.get('message') or ''}"[:110].ljust(110),
                      end="", flush=True)
            except KeyboardInterrupt:
                print("\nStopping safely...", flush=True)
                stop.set()
        result = box.get("result", "failed")
        print()
    rec = projects.read_project(path)["recording"]
    print(f"Recording {result}: {rec.get('message') or rec.get('error') or ''}")
    if result == "recorded":
        settings = validate_settings(rec.get("settings"))
        if settings["after"] != "none":
            # Hand off to the unchanged Chopify pipeline with the local file.
            cmd = [sys.executable, str(Path(__file__).resolve().parent / "chopify.py"),
                   "--project", str(path), "--action", settings["after"],
                   "--no-input"]
            print("Processing:", " ".join(cmd), flush=True)
            return subprocess.run(cmd).returncode
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
