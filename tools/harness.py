"""Shared helpers for the Phase 9 harnesses (tools/stress.py, tools/recovery_test.py).

* Everything runs in an isolated run folder with its own projects folder,
  Chopify config and logs. ``real_projects_snapshot`` lets a harness prove
  the user's real projects were not touched.
* ``keep_awake`` asks Windows not to sleep while the harness runs, and
  ``Pacer`` notices when the computer slept or stalled anyway (a loop
  iteration that took far longer than planned), so results are flagged.
* ``run_processes`` finds only processes whose command line mentions the run
  folder, so the user's own Studio and workers are never touched.
"""
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REAL_PROJECTS = HERE / "projects"


def keep_awake(on=True):
    if sys.platform == "win32":
        flags = 0x80000000 | (0x00000001 if on else 0)   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(flags)


class Pacer:
    """Sleeps between samples and records gaps that mean sleep or a stall."""

    def __init__(self, step):
        self.step = step
        self.last = time.time()
        self.gaps = []

    def wait(self):
        time.sleep(self.step)
        now = time.time()
        if now - self.last > self.step * 4 + 30:
            self.gaps.append({"at": round(now, 1), "seconds": round(now - self.last, 1)})
        self.last = now


def api(base, token, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, headers={
        "Content-Type": "application/json", "X-Chopify-Token": token or ""})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def start_studio(run, port, env):
    proc = subprocess.Popen([sys.executable, str(HERE / "dashboard.py"), "--no-browser",
                             "--port", str(port), "--projects-dir", str(run / "projects")],
                            cwd=HERE, env=env, stdout=(run / "studio.out").open("ab"),
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError("Studio exited during start; see studio.out")
        try:
            _, config = api(base, "", "/api/config", timeout=5)
            return proc, base, config["token"]
        except OSError:
            time.sleep(1)
    raise RuntimeError("Studio did not start")


def run_processes(run, exclude=()):
    """[(pid, name, cmd)] of python/ffmpeg processes working on this run folder
    (``exclude``: the harness's own simulators, e.g. the OBS writers)."""
    marker = str(run).replace("'", "''")
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='ffmpeg.exe'\" | "
          f"Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains('{marker}') }} | "
          "ForEach-Object { \"$($_.ProcessId)|$($_.Name)|$($_.CommandLine)\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                         text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) not in exclude:
            rows.append((int(parts[0]), parts[1], parts[2]))
    return rows


def workers(run):
    """Chopify worker processes for this run. A venv python.exe launches the
    real interpreter with the same command line, so one worker can show as two
    processes; they are counted once (``len(set(...))`` of command lines)."""
    return [p for p in run_processes(run) if p[1] == "python.exe" and "chopify.py" in p[2]]


def worker_count(run):
    return len({cmd.split("chopify.py", 1)[1] for _, _, cmd in workers(run)})


def kill_studio_only(proc):
    """Like a crash: Studio's process alone, not its children (/T is not used)."""
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/F"], capture_output=True)
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        pass


def kill_run(run, proc=None):
    """End of a harness: stop Studio and anything still working on this run."""
    if proc is not None and proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    for pid, _, _ in run_processes(run):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


def real_projects_snapshot():
    """{relative path: (size, mtime)} of the user's real projects folder."""
    out = {}
    if REAL_PROJECTS.is_dir():
        for file in REAL_PROJECTS.rglob("*"):
            try:
                if file.is_file():
                    info = file.stat()
                    out[str(file.relative_to(REAL_PROJECTS))] = (info.st_size, info.st_mtime)
            except OSError:
                pass
    return out


def isolated_env(run, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHOPIFY_")}
    env.update({"CHOPIFY_CONFIG": str(run / "config"), "CHOPIFY_LOG_DIR": str(run / "logs")})
    env.update({k: str(v) for k, v in extra.items()})
    return env


def speech(out, seconds=60):
    """Synthetic speech (Windows TTS) looped to ``seconds``."""
    wav = out.with_suffix(".tts.wav")
    script = ("Welcome back to the stream everyone. Okay the enemy team is pushing from the "
              "left side. Wait, wait, watch this jump. Oh my god, that was insane! Did you see "
              "that clutch? Four kills in ten seconds. Lets go again, one more round.")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Add-Type -AssemblyName System.Speech; "
                    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    f"$s.SetOutputToWaveFile('{wav}'); $s.Speak('{script}'); $s.Dispose()"],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-stream_loop", "-1", "-i", str(wav), "-t",
                    str(seconds), "-ar", "16000", "-ac", "1", str(out)], check=True)
    return out


def obs_writer(folder, speech_wav, seconds, burst, offset, size="854x480", bitrate="1500k"):
    """A growing OBS-style MKV: picture + speech + a loud burst every ``burst`` s."""
    loud = (f"aevalsrc='0.8*sin(2*PI*660*t)*between(mod(t+{offset}\\,{burst})\\,{burst - 4}"
            f"\\,{burst - 1})':s=16000")
    return subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-re", "-f", "lavfi", "-i",
         f"testsrc2=size={size}:rate=30", "-stream_loop", "-1", "-i", str(speech_wav),
         "-f", "lavfi", "-i", loud, "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[a]",
         "-map", "0:v", "-map", "[a]", "-t", str(seconds), "-c:v", "libx264", "-preset",
         "veryfast", "-g", "60", "-b:v", bitrate, "-c:a", "aac",
         str(Path(folder) / time.strftime("%Y-%m-%d %H-%M-%S.mkv"))], stdin=subprocess.DEVNULL)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,width,height:format=duration", "-of", "json",
                          str(path)], capture_output=True, text=True).stdout
    try:
        data = json.loads(out)
    except ValueError:
        return None
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"width": video.get("width"), "height": video.get("height"),
            "duration": float((data.get("format") or {}).get("duration") or 0)}


def system_sample(root):
    ps = ("$c=(Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average;"
          "$m=Get-CimInstance Win32_OperatingSystem;"
          "$p=Get-Process python,ffmpeg -ErrorAction SilentlyContinue | "
          "Measure-Object WorkingSet64 -Sum;"
          "$n=Get-NetAdapterStatistics -ErrorAction SilentlyContinue | "
          "Measure-Object SentBytes,ReceivedBytes -Sum;"
          "Write-Output \"$c|$($m.FreePhysicalMemory)|$($m.TotalVisibleMemorySize)|$($p.Sum)|"
          "$($p.Count)|$($n[0].Sum)|$($n[1].Sum)\"")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                         text=True).stdout.strip().split("|")
    gpu = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu",
                          "--format=csv,noheader,nounits"], capture_output=True,
                         text=True).stdout.strip().split(",")

    def num(i):
        try:
            return float(out[i])
        except (IndexError, ValueError):
            return None
    import shutil
    return {"t": time.time(), "cpu_pct": num(0),
            "ram_free_mb": round(num(1) / 1024) if num(1) else None,
            "ram_total_mb": round(num(2) / 1024) if num(2) else None,
            "python_ffmpeg_rss_mb": round(num(3) / 1e6) if num(3) else None,
            "python_ffmpeg_count": num(4), "net_sent_bytes": num(5), "net_recv_bytes": num(6),
            "vram_used_mb": int(gpu[0]) if gpu and gpu[0].strip().isdigit() else None,
            "gpu_util_pct": int(gpu[1]) if len(gpu) > 1 and gpu[1].strip().isdigit() else None,
            "disk_free_gb": round(shutil.disk_usage(str(root)).free / 1e9, 2),
            "run_bytes": folder_bytes(root)}


def folder_bytes(root):
    """Size of a folder that is changing under us (files appear and vanish)."""
    total = 0
    for file in Path(root).rglob("*"):
        try:
            if file.is_file():
                total += file.stat().st_size
        except OSError:
            pass
    return total


def stats(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    if not values:
        return None
    return {"avg": round(sum(values) / len(values), 1), "max": max(values), "min": min(values)}
