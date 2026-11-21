"""Structured diagnostic logging for Studio, jobs and the pipeline.

Every line in ``logs/chopify-YYYY-MM-DD.jsonl`` is one JSON object:

    {"time": "2026-11-21T14:03:12.345", "level": "INFO", "process": "studio",
     "operation": "project.create", "message": "...", "project_id": "...",
     "job_id": "...", "request_id": "...", "stage": "...", "error": "...",
     "traceback": "..."}

* One file per day for all processes (Studio and the chopify.py workers
  append to it; lines are written with a single append each). A file larger
  than MAX_BYTES continues as ``chopify-YYYY-MM-DD.1.jsonl``; files older than
  KEEP_DAYS are deleted.
* Secrets are removed before writing (OAuth tokens, client secrets,
  authorization codes, Bearer headers).
* ``python applog.py`` prints recent entries readably; filter with
  ``--project ID``, ``--request ID``, ``--errors`` or ``-n 200``.

The folder is ``<repo>/logs`` or ``$CHOPIFY_LOG_DIR``.
"""
import argparse
import contextlib
import contextvars
import datetime
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_BYTES = 10_000_000
KEEP_DAYS = 14
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
FIELDS = ("project_id", "job_id", "request_id", "stage")

_context = contextvars.ContextVar("chopify_log_context", default={})
_lock = threading.Lock()
_process = {"name": "studio" if Path(sys.argv[0]).stem == "dashboard" else
            Path(sys.argv[0]).stem or "python"}
_cleaned = {"done": False}

_SECRETS = [
    (re.compile(r"(?i)\b(access_token|refresh_token|id_token|client_secret|"
                r"authorization_code|code_verifier|code|token|password|secret)"
                r"(\"?\s*[:=]\s*\"?)([^\s\"'&,}]{6,})"), r"\1\2[redacted]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+"), "[redacted-token]"),
    (re.compile(r"\b1//[A-Za-z0-9._-]{10,}"), "[redacted-token]"),
    (re.compile(r"\bGOCSPX-[A-Za-z0-9_-]+"), "[redacted-secret]"),
    (re.compile(r"(?i)(x-chopify-token\"?\s*[:=]\s*\"?)[A-Za-z0-9_-]{8,}"), r"\1[redacted]"),
]


def log_dir():
    return Path(os.environ.get("CHOPIFY_LOG_DIR") or (HERE / "logs"))


def set_process(name):
    _process["name"] = str(name)


def redact(text):
    text = str(text)
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


# ----------------------------------------------------------------- context

def context():
    """The current ids (project, job, request, stage) as a dict."""
    values = dict(_context.get())
    for field, env in (("project_id", "CHOPIFY_PROJECT_ID"), ("job_id", "CHOPIFY_JOB_ID"),
                       ("request_id", "CHOPIFY_REQUEST_ID")):
        if field not in values and os.environ.get(env):
            values[field] = os.environ[env]
    return values


@contextlib.contextmanager
def bound(**fields):
    """Attach ids to every log line written inside this block."""
    token = _context.set({**_context.get(), **{k: v for k, v in fields.items() if v}})
    try:
        yield
    finally:
        _context.reset(token)


def bind(**fields):
    """Attach ids for the rest of this thread/process (no block)."""
    _context.set({**_context.get(), **{k: v for k, v in fields.items() if v}})


# ------------------------------------------------------------------- write

def _path_for(day):
    folder = log_dir()
    base = folder / f"chopify-{day}.jsonl"
    index = 0
    path = base
    while path.exists() and path.stat().st_size >= MAX_BYTES:
        index += 1
        path = folder / f"chopify-{day}.{index}.jsonl"
    return path


def _cleanup(folder):
    if _cleaned["done"]:
        return
    _cleaned["done"] = True
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in folder.glob("chopify-*.jsonl"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def write(level, operation, message="", exc=None, **fields):
    """Write one structured line. Never raises (logging must not break work)."""
    now = datetime.datetime.now()
    entry = {"time": now.isoformat(timespec="milliseconds"), "level": level,
             "process": _process["name"], "pid": os.getpid(),
             "operation": operation, "message": redact(message)}
    entry.update(context())
    for key, value in fields.items():
        if value is not None:
            entry[key] = redact(value) if isinstance(value, str) else value
    if exc is not None:
        entry["error"] = redact(f"{type(exc).__name__}: {exc}")
        entry["traceback"] = redact("".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__)))
    try:
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    except (TypeError, ValueError):
        line = json.dumps({**entry, "message": repr(message)}) + "\n"
    try:
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        _cleanup(folder)
        with _lock:
            with open(_path_for(now.date().isoformat()), "a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError:
        pass
    return entry


def info(operation, message="", **fields):
    return write("INFO", operation, message, **fields)


def warning(operation, message="", **fields):
    return write("WARNING", operation, message, **fields)


def error(operation, message="", exc=None, **fields):
    return write("ERROR", operation, message, exc=exc, **fields)


@contextlib.contextmanager
def step(operation, message="", **fields):
    """Log '<operation> started' and 'completed' (with seconds) or 'failed'."""
    started = time.time()
    info(operation, f"{message or operation} started", event="started", **fields)
    try:
        yield
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)) and not str(exc):
            warning(operation, f"{message or operation} interrupted", event="failed",
                    seconds=round(time.time() - started, 2), **fields)
        else:
            error(operation, f"{message or operation} failed", exc=exc, event="failed",
                  seconds=round(time.time() - started, 2), **fields)
        raise
    info(operation, f"{message or operation} completed", event="completed",
         seconds=round(time.time() - started, 2), **fields)


# -------------------------------------------------------------------- read

def read(limit=100, project_id=None, request_id=None, min_level="DEBUG", days=2):
    """Newest-first entries from the last ``days`` daily files."""
    folder = log_dir()
    def order(p):                   # chopify-DAY[.N].jsonl -> (DAY, N)
        parts = p.stem.split(".")
        index = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return parts[0], index
    files = sorted(folder.glob("chopify-*.jsonl"), key=order, reverse=True)
    recent = sorted({order(p)[0] for p in files}, reverse=True)[:days]
    files = [p for p in files if order(p)[0] in recent]
    entries = []
    floor = LEVELS.get(min_level, 10)
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if project_id and entry.get("project_id") != project_id:
                continue
            if request_id and entry.get("request_id") != request_id:
                continue
            if LEVELS.get(entry.get("level"), 20) < floor:
                continue
            entries.append(entry)
    entries.sort(key=lambda e: e.get("time", ""), reverse=True)
    return entries[:limit]


def _main():
    parser = argparse.ArgumentParser(description="Show recent Chopify log entries.")
    parser.add_argument("-n", type=int, default=60, help="how many entries (default 60)")
    parser.add_argument("--project", help="only this project ID")
    parser.add_argument("--request", help="only this request ID")
    parser.add_argument("--errors", action="store_true", help="only warnings and errors")
    parser.add_argument("--traceback", action="store_true", help="print tracebacks")
    args = parser.parse_args()
    entries = read(args.n, args.project, args.request,
                   "WARNING" if args.errors else "DEBUG", days=KEEP_DAYS)
    for entry in reversed(entries):
        ids = " ".join(f"{k.split('_')[0]}={entry[k]}" for k in FIELDS if entry.get(k))
        print(f"{entry['time']} {entry['level']:<7} {entry['process']:<8} "
              f"{entry['operation']:<22} {entry['message']}  {ids}")
        if entry.get("error"):
            print(f"{'':>40}error: {entry['error']}")
        if args.traceback and entry.get("traceback"):
            print(entry["traceback"])
    print(f"({len(entries)} entries from {log_dir()})")


if __name__ == "__main__":
    _main()
