"""Phase 5: an approval-gated, resumable upload queue for YouTube Shorts.

Life of an upload (one entry per rendered clip, keyed by the file's SHA-256):

    draft      - created when the user opens "Publish"; metadata is editable.
    queued     - ONLY after the user explicitly approves this clip.
    uploading  - a resumable upload session exists; bytes are being sent.
    uploaded   - YouTube returned the video id. Never uploaded again.
    quota_wait - the daily quota is used up; waits for the next quota day.
    failed     - needs attention (or retries itself for temporary errors).
    cancelled  - the user withdrew it before it finished.

Nothing is ever uploaded without approval: ``approve(..., confirm=True)`` is
the only way into the queue, and every entry records when it was approved.

Uploads use the YouTube Data API v3 resumable protocol (videos.insert,
uploadType=resumable). The session URI is saved before any bytes are sent, so
after a crash, restart or network drop the upload asks YouTube how much
arrived and continues from there - and if YouTube already finished it, the
entry is simply marked uploaded. That, plus refusing a file whose fingerprint
is already uploaded, is what prevents duplicates.

Quota (YouTube Data API v3, granular quota system since June 2026):
* videos.insert has its own bucket: 100 calls per day by default, each call
  1 unit. Every insert request counts, including failed ones and retries.
* Every other method (channels.list, ...) shares a separate bucket of 10,000
  units per day. search.list has a third bucket; Chopify does not use it.
* Chopify's own "uploads per day" safety limit (default 6) is separate from
  both and counts uploads started.
The quota day resets at midnight Pacific Time. The queue keeps a ledger of
all three, never starts an upload that would exceed any of them, and parks
entries in quota_wait when YouTube says a quota (or the channel's upload
limit) is exhausted.
"""
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import publish_meta
import youtube_auth

UPLOAD_URI = "https://www.googleapis.com/upload/youtube/v3/videos"
QUEUE_FILE = "uploads.json"
USAGE_FILE = "upload_usage.json"
SETTINGS_FILE = "upload_settings.json"
STATES = ("draft", "queued", "uploading", "uploaded", "quota_wait", "failed",
          "cancelled")
CHUNK = 8 * 1024 * 1024           # multiple of 256 KiB, as the API requires
DEFAULT_SETTINGS = {
    "upload_quota_calls": 100,     # videos.insert bucket (1 per call)
    "general_quota_units": 10000,  # all other methods (channels.list = 1)
    "max_uploads_per_day": 6,      # Chopify's own safety limit
    "privacy": "private",         # new uploads default to private
    "category_id": "22",
    "made_for_kids": False,
    "notify_subscribers": False,
}
SHORTS_MAX_SECONDS = 180
RETRY_ATTEMPTS = 5                # transient errors per run, with backoff
EMPTY_DAY = {"upload_calls": 0,          # videos.insert requests (upload bucket)
             "general_units": 0,         # other Data API methods (general bucket)
             "uploads_started": 0,       # sessions created (Chopify's limit)
             "upload_bucket_exhausted": False,
             "general_exhausted": False}
AUTO_RETRY_DELAY = 15 * 60        # then the worker tries again later
RETRYABLE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError",
                     "internalError"}
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
CHANNEL_LIMIT_REASONS = {"uploadLimitExceeded"}


class UploadError(ValueError):
    """A queue action the user asked for is not allowed."""


class _Transient(Exception):
    pass


class _Stop(Exception):
    def __init__(self, state, message, retry_at=None, retryable=False):
        super().__init__(message)
        self.state, self.message = state, message
        self.retry_at, self.retryable = retry_at, retryable


# ------------------------------------------------------------- quota day

def _nth_sunday(year, month, n):
    day = datetime(year, month, 1, tzinfo=timezone.utc)
    first = day + timedelta(days=(6 - day.weekday()) % 7)
    return first + timedelta(weeks=n - 1)


def pacific_offset(ts):
    """UTC offset (hours) of US Pacific time at timestamp ``ts``.

    Daylight time runs from 2:00 local on the second Sunday of March to 2:00
    local on the first Sunday of November (no tz database needed).
    """
    moment = datetime.fromtimestamp(ts, timezone.utc)
    start = _nth_sunday(moment.year, 3, 2) + timedelta(hours=10)   # 02:00 PST
    end = _nth_sunday(moment.year, 11, 1) + timedelta(hours=9)     # 02:00 PDT
    return -7 if start <= moment < end else -8


def quota_day(ts):
    offset = pacific_offset(ts)
    return (datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=offset)).date().isoformat()


def next_quota_reset(ts):
    """Timestamp of the next midnight Pacific Time after ``ts``."""
    offset = pacific_offset(ts)
    local = datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=offset)
    midnight = datetime(local.year, local.month, local.day, tzinfo=timezone.utc) + timedelta(days=1)
    reset = (midnight - timedelta(hours=offset)).timestamp()
    # crossing a DST change: recompute with the offset in force at that moment
    return (midnight - timedelta(hours=pacific_offset(reset))).timestamp()


# ------------------------------------------------------------- helpers

def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_clip(path):
    """(width, height, duration) via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    return (int(stream.get("width") or 0), int(stream.get("height") or 0),
            float((data.get("format") or {}).get("duration") or 0))


def shorts_problem(width, height, duration):
    if not width or not height:
        return "The clip has no video stream."
    if width > height:
        return ("This clip is landscape. Shorts must be vertical or square: "
                "render the project in 9:16 first.")
    if duration > SHORTS_MAX_SECONDS + 0.5:
        return f"Shorts can be at most 3 minutes; this clip is {duration:.0f}s."
    return None


def _error_reason(body):
    try:
        error = json.loads(body.decode("utf-8")).get("error") or {}
    except (ValueError, UnicodeDecodeError, AttributeError):
        return "", ""
    reasons = [e.get("reason", "") for e in error.get("errors") or []]
    return (reasons[0] if reasons else error.get("status", "")), error.get("message", "")


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{secrets.token_hex(4)}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------- the queue

class UploadQueue:
    def __init__(self, account, directory=None, clock=time.time, sleep=time.sleep,
                 chunk_size=CHUNK, probe=probe_clip):
        self.account = account
        self.dir = Path(directory or account.dir)
        self.clock, self.sleep = clock, sleep
        self.chunk_size = chunk_size
        self.probe = probe
        self.lock = threading.RLock()
        account.quota_hook = self.record_api_call

    # ----------------------------------------------------------- storage
    def entries(self):
        with self.lock:
            return _read(self.dir / QUEUE_FILE, {"entries": []})["entries"]

    def _save(self, entries):
        _atomic(self.dir / QUEUE_FILE, {"version": 1, "entries": entries})

    def _mutate(self, entry_id, change):
        with self.lock:
            entries = self.entries()
            for entry in entries:
                if entry["id"] == entry_id:
                    result = change(entry)
                    entry["updated"] = self.clock()
                    self._save(entries)
                    return entry if result is None else result
        raise UploadError("Unknown upload.")

    def get(self, entry_id):
        for entry in self.entries():
            if entry["id"] == entry_id:
                return entry
        raise UploadError("Unknown upload.")

    def settings(self):
        stored = _read(self.dir / SETTINGS_FILE, {})
        return {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items()
                                       if k in DEFAULT_SETTINGS}}

    def update_settings(self, changes):
        settings = self.settings()
        for key, value in (changes or {}).items():
            if key not in DEFAULT_SETTINGS:
                raise UploadError(f"Unknown upload setting: {key}")
            settings[key] = value
        for key in ("upload_quota_calls", "general_quota_units", "max_uploads_per_day"):
            value = settings[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000_000:
                raise UploadError(f"{key} must be a whole number.")
        publish_meta.clean({"title": "x", "privacy": settings["privacy"],
                            "category_id": settings["category_id"],
                            "made_for_kids": settings["made_for_kids"]})
        if not isinstance(settings["notify_subscribers"], bool):
            raise UploadError("notify_subscribers must be true or false.")
        _atomic(self.dir / SETTINGS_FILE, settings)
        return settings

    def _today(self, ledger, day):
        raw = ledger.get(day) or {}
        return {key: raw.get(key, default) for key, default in EMPTY_DAY.items()}

    def usage(self):
        """Today's use of each quota bucket and of Chopify's own limit.

        The quota day is the Pacific Time calendar day (YouTube resets every
        bucket at midnight PT).
        """
        day = quota_day(self.clock())
        today = self._today(_read(self.dir / USAGE_FILE, {}), day)
        settings = self.settings()
        upload_left = max(0, settings["upload_quota_calls"] - today["upload_calls"])
        general_left = max(0, settings["general_quota_units"] - today["general_units"])
        app_left = max(0, settings["max_uploads_per_day"] - today["uploads_started"])
        blocked = None
        if today["upload_bucket_exhausted"] or upload_left < 1:
            blocked = "upload_quota"
        elif app_left < 1:
            blocked = "app_limit"
        return {"day": day, **today,
                "upload_quota_calls": settings["upload_quota_calls"],
                "upload_calls_left": upload_left,
                "general_quota_units": settings["general_quota_units"],
                "general_units_left": general_left,
                "max_uploads_per_day": settings["max_uploads_per_day"],
                "app_uploads_left": app_left,
                "uploads_left_today": 0 if blocked else min(upload_left, app_left),
                "blocked_by": blocked,
                "resets_at": next_quota_reset(self.clock())}

    def record_api_call(self, bucket, units):
        """quota_hook for the account: one Data API request is about to be sent."""
        if bucket == youtube_auth.UPLOAD_BUCKET:
            self._record_usage(upload_calls=units)
        else:
            self._record_usage(general_units=units)

    def _record_usage(self, upload_calls=0, general_units=0, uploads_started=0,
                      upload_bucket_exhausted=None, general_exhausted=None):
        with self.lock:
            day = quota_day(self.clock())
            ledger = _read(self.dir / USAGE_FILE, {})
            today = self._today(ledger, day)
            today["upload_calls"] += upload_calls
            today["general_units"] += general_units
            today["uploads_started"] += uploads_started
            if upload_bucket_exhausted is not None:
                today["upload_bucket_exhausted"] = upload_bucket_exhausted
            if general_exhausted is not None:
                today["general_exhausted"] = general_exhausted
            ledger[day] = today
            for old in sorted(ledger)[:-14]:      # keep two weeks
                ledger.pop(old)
            _atomic(self.dir / USAGE_FILE, ledger)

    # ----------------------------------------------------------- user actions
    def draft(self, project_id, clip_path, metadata, extra=None):
        """Create (or return the existing) entry for a rendered clip."""
        clip_path = Path(clip_path).resolve()
        if not clip_path.is_file():
            raise UploadError("Rendered clip not found.")
        digest = file_sha256(clip_path)
        with self.lock:
            entries = self.entries()
            for entry in entries:
                if entry["sha256"] == digest and entry["state"] != "cancelled":
                    return entry
            entry = {"id": secrets.token_hex(8), "project_id": project_id,
                     "file": str(clip_path), "name": clip_path.name,
                     "sha256": digest, "size": clip_path.stat().st_size,
                     "state": "draft", "metadata": metadata, "created": self.clock(),
                     "updated": self.clock(), "approved_at": None, "session": None,
                     "progress": 0.0, "attempts": 0, "next_attempt": None,
                     "error": None, "video_id": None, "url": None,
                     **(extra or {})}
            entries.append(entry)
            self._save(entries)
            return entry

    def update_metadata(self, entry_id, metadata):
        cleaned = publish_meta.clean(metadata, self.settings())

        def change(entry):
            if entry["state"] in ("uploading", "uploaded"):
                raise UploadError("This clip is already uploading or uploaded.")
            if entry["state"] == "queued":
                # Changing approved metadata withdraws the approval.
                entry["state"] = "draft"
                entry["approved_at"] = None
            entry["metadata"] = {**cleaned, "source": entry["metadata"].get("source", "edited")}
        return self._mutate(entry_id, change)

    def approve(self, entry_id, metadata=None, confirm=False):
        """The user's explicit go-ahead for ONE clip. Nothing else queues uploads."""
        if confirm is not True:
            raise UploadError("Uploading needs explicit confirmation.")
        if not self.account.status().get("connected"):
            raise UploadError("Connect a YouTube channel first.")
        entry = self.get(entry_id)
        cleaned = publish_meta.clean(metadata if metadata is not None else entry["metadata"],
                                     self.settings())
        path = Path(entry["file"])
        if not path.is_file():
            raise UploadError("The rendered clip no longer exists.")
        if file_sha256(path) != entry["sha256"]:
            raise UploadError("The clip changed since it was added. Open Publish again.")
        problem = shorts_problem(*self.probe(path))
        if problem:
            raise UploadError(problem)
        for other in self.entries():
            if other["sha256"] == entry["sha256"] and other["id"] != entry_id \
                    and other["state"] in ("queued", "uploading", "uploaded"):
                raise UploadError("This clip is already queued or uploaded.")

        def change(item):
            if item["state"] == "uploaded":
                raise UploadError(f"Already uploaded: {item['url']}")
            if item["state"] in ("queued", "uploading"):
                raise UploadError("This clip is already queued.")
            item.update(state="queued", approved_at=self.clock(), error=None,
                        next_attempt=None, attempts=0,
                        metadata={**cleaned, "source": item["metadata"].get("source", "edited")})
        return self._mutate(entry_id, change)

    def retry(self, entry_id):
        def change(entry):
            if entry["state"] not in ("failed", "quota_wait"):
                raise UploadError("Only failed or waiting uploads can be retried.")
            if not entry.get("approved_at"):
                raise UploadError("Approve this upload first.")
            entry.update(state="queued", next_attempt=None, error=None, attempts=0)
        return self._mutate(entry_id, change)

    def cancel(self, entry_id):
        def change(entry):
            if entry["state"] in ("uploaded", "uploading"):
                raise UploadError("An upload in progress or finished cannot be cancelled here.")
            entry.update(state="cancelled", next_attempt=None)
        return self._mutate(entry_id, change)

    # ----------------------------------------------------------- worker
    def due(self):
        now = self.clock()
        return [e for e in sorted(self.entries(), key=lambda e: e.get("approved_at") or 0)
                if e.get("approved_at") and (
                    e["state"] in ("queued", "uploading")
                    or (e["state"] in ("quota_wait", "failed") and e.get("next_attempt")
                        and e["next_attempt"] <= now))]

    def next_wakeup(self):
        times = [e["next_attempt"] for e in self.entries()
                 if e["state"] in ("quota_wait", "failed") and e.get("next_attempt")]
        return min(times) if times else None

    def process(self, progress=None):
        """Upload every due, approved entry in approval order. Returns ids done."""
        done = []
        for entry in self.due():
            entry = self.get(entry["id"])
            if not entry.get("approved_at") or entry["state"] in ("uploaded", "cancelled", "draft"):
                continue
            try:
                self._upload_with_retries(entry["id"], progress)
                done.append(entry["id"])
            except _Stop as stop:
                self._mutate(entry["id"], lambda e: e.update(
                    state=stop.state, error=stop.message, next_attempt=stop.retry_at,
                    retryable=stop.retryable))
                if stop.state == "quota_wait":
                    # The rest would hit the same wall; leave them queued.
                    break
        return done

    def _upload_with_retries(self, entry_id, progress):
        delay = 2.0
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            self._mutate(entry_id, lambda e: e.update(attempts=e.get("attempts", 0) + 1))
            try:
                return self._upload(entry_id, progress)
            except (_Transient, OSError) as exc:
                if attempt == RETRY_ATTEMPTS:
                    raise _Stop("failed", f"Temporary YouTube or network problem: {exc}. "
                                "Chopify will retry automatically.",
                                self.clock() + AUTO_RETRY_DELAY, retryable=True)
                self.sleep(min(delay, 64))
                delay *= 2
            except youtube_auth.AuthError as exc:
                raise _Stop("failed", str(exc))

    def _upload(self, entry_id, progress):
        entry = self.get(entry_id)
        path = Path(entry["file"])
        if not path.is_file():
            raise _Stop("failed", "The rendered clip no longer exists.")
        size = path.stat().st_size
        if size != entry["size"] or file_sha256(path) != entry["sha256"]:
            raise _Stop("failed", "The clip changed after approval; approve it again.")
        offset = 0
        if entry.get("session"):
            outcome, value = self._session_status(entry, size)
            if outcome == "done":
                return self._finish(entry_id, value)
            if outcome == "resume":
                offset = value
            else:                        # expired: YouTube has nothing; start over
                self._mutate(entry_id, lambda e: e.update(session=None))
                entry = self.get(entry_id)
        if not entry.get("session"):
            self._start_session(entry_id, size)
        entry = self.get(entry_id)
        self._mutate(entry_id, lambda e: e.update(state="uploading", error=None))
        with open(path, "rb") as handle:
            while offset < size:
                handle.seek(offset)
                chunk = handle.read(self.chunk_size)
                end = offset + len(chunk) - 1
                status, headers, body = self.account.authorized(
                    "PUT", entry["session"], {
                        "Content-Length": str(len(chunk)),
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{end}/{size}"}, chunk, 600)
                if status == 308:
                    offset = _received(headers)
                    share = offset / size if size else 1.0
                    self._mutate(entry_id, lambda e: e.update(progress=round(share, 4)))
                    if progress:
                        progress(entry_id, share)
                elif status in (200, 201):
                    return self._finish(entry_id, json.loads(body.decode("utf-8")))
                elif status in (404, 410):
                    self._mutate(entry_id, lambda e: e.update(session=None, progress=0.0))
                    raise _Transient("upload session expired")
                else:
                    self._api_error(status, body)
        # every byte sent but no final answer: ask YouTube once more
        outcome, value = self._session_status(self.get(entry_id), size)
        if outcome == "done":
            return self._finish(entry_id, value)
        raise _Transient("YouTube did not confirm the upload")

    def _start_session(self, entry_id, size):
        entry = self.get(entry_id)
        usage, settings = self.usage(), self.settings()
        if usage["blocked_by"] == "upload_quota":
            raise _Stop("quota_wait", "Today's YouTube upload quota (videos.insert "
                        f"calls: {usage['upload_calls']} of {usage['upload_quota_calls']}) "
                        "is used up; this clip uploads automatically after the "
                        "reset at midnight Pacific Time.", usage["resets_at"])
        if usage["blocked_by"] == "app_limit":
            raise _Stop("quota_wait", "Chopify's limit of "
                        f"{usage['max_uploads_per_day']} upload(s) per day is reached; "
                        "this clip uploads automatically after midnight Pacific Time.",
                        usage["resets_at"])
        meta = entry["metadata"]
        resource = {
            "snippet": {"title": meta["title"], "description": meta["description"],
                        "tags": meta.get("tags") or [],
                        "categoryId": meta.get("category_id", settings["category_id"])},
            "status": {"privacyStatus": meta.get("privacy", settings["privacy"]),
                       "selfDeclaredMadeForKids": bool(meta.get("made_for_kids", False)),
                       "embeddable": True},
        }
        query = urlencode({"uploadType": "resumable", "part": "snippet,status",
                           "notifySubscribers": str(bool(settings["notify_subscribers"])).lower()})
        status, headers, body = self.account.authorized(
            "POST", f"{UPLOAD_URI}?{query}", {
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4"},
            json.dumps(resource).encode("utf-8"), 120)
        # (the insert call itself was counted in the upload bucket by the
        # account's quota hook, before it was sent - even if it fails)
        if status != 200 or not headers.get("location"):
            self._api_error(status, body)
        self._record_usage(uploads_started=1)       # Chopify's daily limit
        session = headers["location"]
        self._mutate(entry_id, lambda e: e.update(session=session, progress=0.0,
                                                  state="uploading"))

    def _session_status(self, entry, size):
        status, headers, body = self.account.authorized(
            "PUT", entry["session"], {"Content-Length": "0",
                                      "Content-Range": f"bytes */{size}"}, b"", 60)
        if status in (200, 201):
            return "done", json.loads(body.decode("utf-8"))
        if status == 308:
            return "resume", _received(headers)
        if status in (404, 410):
            return "expired", None
        if status >= 500:
            raise _Transient(f"HTTP {status}")
        self._api_error(status, body)

    def _finish(self, entry_id, video):
        video_id = video.get("id")
        if not video_id:
            raise _Transient("YouTube answered without a video id")
        url = f"https://www.youtube.com/shorts/{video_id}"
        self._mutate(entry_id, lambda e: e.update(
            state="uploaded", video_id=video_id, url=url, progress=1.0,
            session=None, error=None, next_attempt=None, uploaded_at=self.clock(),
            youtube_status=(video.get("status") or {}).get("uploadStatus")))
        return video_id

    def _api_error(self, status, body):
        reason, message = _error_reason(body)
        if status >= 500 or reason in RETRYABLE_REASONS or status == 429:
            raise _Transient(f"HTTP {status} {reason}".strip())
        if reason in QUOTA_REASONS:
            # The queue only calls videos.insert and its upload session, so
            # the exhausted bucket is the upload bucket.
            self._record_usage(upload_bucket_exhausted=True)
            raise _Stop("quota_wait", "YouTube says today's upload quota is used up; "
                        "the upload continues after the reset at midnight Pacific Time.",
                        next_quota_reset(self.clock()))
        if reason in CHANNEL_LIMIT_REASONS:
            raise _Stop("quota_wait", "This channel reached YouTube's daily upload "
                        "limit; Chopify will try again in 24 hours.",
                        self.clock() + 24 * 3600)
        if status == 401:
            raise _Stop("failed", "YouTube rejected the sign-in. Connect the channel again.")
        hint = {"invalidTitle": "Edit the title.",
                "invalidDescription": "Edit the description.",
                "invalidTags": "Edit the tags.",
                "forbidden": "This account may not upload to this channel.",
                "youtubeSignupRequired": "Create a YouTube channel for this account first."}
        raise _Stop("failed", f"YouTube refused the upload ({reason or status}): "
                    f"{message or ''} {hint.get(reason, '')}".strip())


def _received(headers):
    """Next byte to send, from a 308 Range header ("bytes=0-1234")."""
    match = re.search(r"bytes=0-(\d+)", headers.get("range", ""))
    return int(match.group(1)) + 1 if match else 0
