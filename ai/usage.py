"""AI request ledger (SQLite in the Chopify config folder; stdlib only).

One row per provider request: provider, model, capability, data kind,
status, latency, audio seconds, tokens, images, estimated/actual cost and
whether it was free. Never media, prompts, responses or keys. Also keeps the
latest health check per provider and benchmark results.
"""
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone

import youtube_auth

DB_FILE = "ai_usage.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    request_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    capability TEXT NOT NULL,
    data_kind TEXT,
    status TEXT NOT NULL,           -- ok | error | blocked | limited
    error_kind TEXT,
    error TEXT,
    latency_ms INTEGER,
    audio_seconds REAL DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    images INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    free INTEGER DEFAULT 1,
    served_by TEXT,
    project_id TEXT
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
CREATE TABLE IF NOT EXISTS health (
    provider TEXT PRIMARY KEY,
    ts REAL, ok INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, suite TEXT, provider TEXT, model TEXT, sample TEXT,
    metrics TEXT, status TEXT, note TEXT
);
"""


class Ledger:
    def __init__(self, path=None):
        self.path = path or (youtube_auth.config_dir() / DB_FILE)
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def record(self, usage=None, *, provider=None, model=None, capability=None,
               status="ok", data_kind=None, error=None, request_id=None, project_id=None):
        row = {"ts": time.time(), "request_id": request_id, "data_kind": data_kind,
               "status": status, "project_id": project_id,
               "error_kind": getattr(error, "kind", None) if error else None,
               "error": str(error)[:300] if error else None}
        if usage is not None:
            row.update(provider=usage.provider, model=usage.model,
                       capability=usage.capability, latency_ms=usage.latency_ms,
                       audio_seconds=usage.audio_seconds, input_tokens=usage.input_tokens,
                       output_tokens=usage.output_tokens, images=usage.images,
                       cost_usd=usage.cost_usd, free=int(bool(usage.free)),
                       served_by=usage.served_by)
        else:
            row.update(provider=provider, model=model, capability=capability,
                       latency_ms=None, audio_seconds=0, input_tokens=0,
                       output_tokens=0, images=0, cost_usd=0, free=1, served_by=None)
        with self.lock, self._db() as db:
            db.execute(f"INSERT INTO requests ({', '.join(row)}) VALUES "
                       f"({', '.join('?' for _ in row)})", list(row.values()))

    def spent(self, period):
        """Paid USD spent today or this month (UTC)."""
        now = datetime.now(timezone.utc)
        start = (now.replace(hour=0, minute=0, second=0, microsecond=0) if period == "day"
                 else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
        with self._db() as db:
            value = db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM requests WHERE "
                               "ts >= ? AND free = 0 AND status = 'ok'",
                               (start.timestamp(),)).fetchone()[0]
        return float(value or 0.0)

    def summary(self, days=30):
        since = time.time() - days * 86400
        with self._db() as db:
            rows = db.execute("""
                SELECT provider, model, capability, COUNT(*),
                       SUM(status = 'ok'), SUM(status = 'error'), SUM(status = 'blocked'),
                       SUM(status = 'limited'),
                       COALESCE(SUM(audio_seconds), 0) / 60.0,
                       COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(cost_usd), 0), AVG(CASE WHEN status = 'ok' THEN latency_ms END),
                       MIN(free)
                FROM requests WHERE ts >= ? GROUP BY provider, model, capability
                ORDER BY provider, model, capability""", (since,)).fetchall()
        keys = ("provider", "model", "capability", "requests", "ok", "failed", "blocked", "limited",
                "audio_minutes", "input_tokens", "output_tokens", "cost_usd",
                "avg_latency_ms", "free")
        out = []
        for row in rows:
            item = dict(zip(keys, row))
            item["avg_latency_ms"] = round(item["avg_latency_ms"]) if item["avg_latency_ms"] else None
            item["audio_minutes"] = round(item["audio_minutes"], 2)
            item["cost_usd"] = round(item["cost_usd"], 6)
            item["free"] = bool(item["free"])
            out.append(item)
        return out

    def last_limited(self):
        """Latest rate/quota stop per provider: {provider: {ts, error}}."""
        with self._db() as db:
            rows = db.execute("SELECT provider, MAX(ts), error FROM requests "
                              "WHERE status = 'limited' GROUP BY provider").fetchall()
        return {p: {"ts": ts, "error": e} for p, ts, e in rows}

    def set_health(self, provider, ok, detail):
        with self.lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO health VALUES (?, ?, ?, ?)",
                       (provider, time.time(), int(bool(ok)), str(detail)[:300]))

    def health(self):
        with self._db() as db:
            rows = db.execute("SELECT provider, ts, ok, detail FROM health").fetchall()
        return {p: {"checked": ts, "ok": bool(ok), "detail": d} for p, ts, ok, d in rows}

    def add_benchmark(self, suite, provider, model, sample, metrics, status, note=""):
        with self.lock, self._db() as db:
            db.execute("INSERT INTO benchmarks (ts, suite, provider, model, sample, metrics, "
                       "status, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (time.time(), suite, provider, model, sample,
                        json.dumps(metrics, ensure_ascii=False), status, note))
