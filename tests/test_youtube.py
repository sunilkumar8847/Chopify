"""Phase 5: YouTube OAuth, approval-gated uploads, quota and metadata.

Google is replaced by FakeGoogle, an in-memory implementation of the parts of
the OAuth 2.0 and YouTube Data API v3 protocols Chopify uses (token endpoint
with PKCE check, refresh, revoke, channels.list, resumable videos.insert).
No test ever talks to Google.
"""
import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import publish_meta
import youtube_auth
import youtube_upload
from youtube_auth import TOKEN_URI, REVOKE_URI, API, YouTubeAccount
from youtube_upload import UPLOAD_URI, UploadQueue, UploadError

CLIENT = {"installed": {"client_id": "1234-abc.apps.googleusercontent.com",
                        "client_secret": "GOCSPX-super-secret",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": TOKEN_URI}}


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeGoogle:
    """Token endpoint, channels.list, revoke and a resumable upload server."""

    def __init__(self, clock):
        self.clock = clock
        self.challenge = None
        self.access = None
        self.access_expires = 0
        self.refresh = None
        self.revoked = False
        self.refreshes = 0
        self.inserts = []
        self.sessions = {}
        self.calls = []
        self.scope = " ".join(youtube_auth.SCOPES)
        self.insert_error = None          # (status, reason)
        self.chunk_failures = []          # statuses to return for the next PUTs
        self.raise_on_put = 0             # OSError for the next N chunk PUTs
        self.expire_sessions = False

    def _token(self):
        self.access = "ya29." + base64.b16encode(str(self.clock()).encode()).decode()
        self.access_expires = self.clock() + 3600
        return {"access_token": self.access, "expires_in": 3600,
                "scope": self.scope, "token_type": "Bearer"}

    def _authed(self, headers):
        return (headers.get("Authorization") == f"Bearer {self.access}"
                and self.clock() < self.access_expires and not self.revoked)

    def __call__(self, method, url, headers=None, body=None, timeout=60):
        headers = headers or {}
        self.calls.append((method, url))
        if url == TOKEN_URI:
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            assert form["client_secret"] == "GOCSPX-super-secret"
            if form["grant_type"] == "authorization_code":
                verifier = form["code_verifier"]
                expected = base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if form["code"] != "good-code" or expected != self.challenge:
                    return 400, {}, b'{"error": "invalid_grant"}'
                self.refresh = "1//refresh-secret"
                return 200, {}, json.dumps({**self._token(),
                                            "refresh_token": self.refresh}).encode()
            if form["grant_type"] == "refresh_token":
                if self.revoked or form["refresh_token"] != self.refresh:
                    return 400, {}, b'{"error": "invalid_grant"}'
                self.refreshes += 1
                return 200, {}, json.dumps(self._token()).encode()
        if url == REVOKE_URI:
            self.revoked = True
            return 200, {}, b""
        if url.startswith(f"{API}/channels"):
            if not self._authed(headers):
                return 401, {}, b'{"error": {"code": 401}}'
            return 200, {}, json.dumps({"items": [
                {"id": "UC123", "snippet": {"title": "My Channel"}}]}).encode()
        if url.startswith(UPLOAD_URI) and method == "POST":
            if not self._authed(headers):
                return 401, {}, b""
            if self.insert_error:
                status, reason = self.insert_error
                return status, {}, json.dumps({"error": {"code": status, "message": reason,
                                                         "errors": [{"reason": reason}]}}).encode()
            uri = f"https://upload.example/session/{len(self.sessions) + 1}"
            self.sessions[uri] = {"size": int(headers["X-Upload-Content-Length"]),
                                  "data": b"", "meta": json.loads(body), "done": False}
            self.inserts.append(json.loads(body))
            return 200, {"location": uri}, b""
        if url in self.sessions and method == "PUT":
            session = self.sessions[url]
            if not self._authed(headers):
                return 401, {}, b""
            if self.expire_sessions:
                return 404, {}, b""
            content_range = headers["Content-Range"]
            if content_range.startswith("bytes */"):
                if session["done"]:
                    return 200, {}, json.dumps({"id": "vid-" + url[-1],
                                                "status": {"uploadStatus": "uploaded"}}).encode()
                got = len(session["data"])
                return 308, ({"range": f"bytes=0-{got - 1}"} if got else {}), b""
            if self.raise_on_put:
                self.raise_on_put -= 1
                raise ConnectionResetError("connection dropped")
            if self.chunk_failures:
                return self.chunk_failures.pop(0), {}, b'{"error": {"errors": [{"reason": "backendError"}]}}'
            start = int(content_range.split()[1].split("-")[0])
            assert start == len(session["data"]), "chunk must continue where YouTube stopped"
            session["data"] += body
            if len(session["data"]) >= session["size"]:
                session["done"] = True
                return 201, {}, json.dumps({"id": "vid-" + url[-1],
                                            "status": {"uploadStatus": "uploaded"}}).encode()
            return 308, {"range": f"bytes=0-{len(session['data']) - 1}"}, b""
        raise AssertionError(f"unexpected request {method} {url}")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def google(clock):
    return FakeGoogle(clock)


@pytest.fixture
def account(tmp_path, google, clock):
    client = tmp_path / "client_secret_test.json"
    client.write_text(json.dumps(CLIENT))
    acct = YouTubeAccount(tmp_path / "config", http=google, clock=clock)
    acct.import_client(client)
    return acct


def sign_in(account, google):
    """Run the real loopback flow, playing the browser's part."""
    url = account.begin_signin()
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    google.challenge = query["code_challenge"]
    callback = f"{query['redirect_uri']}/?state={query['state']}&code=good-code"
    with urllib.request.urlopen(callback, timeout=10) as response:
        assert b"connected" in response.read()
    for _ in range(100):
        if account.status().get("signin", {}).get("state") != "waiting":
            break
        time.sleep(0.05)
    return query


@pytest.fixture
def connected(account, google):
    sign_in(account, google)
    assert account.status()["connected"]
    return account


def make_clip(tmp_path, name="001-clip.mp4", size=50_000):
    path = tmp_path / "clips" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(bytes((i * 7) % 251 for i in range(size)))
    return path


def queue_for(account, clock, **kwargs):
    sleeps = []
    queue = UploadQueue(account, clock=clock, sleep=sleeps.append, chunk_size=16_384,
                        probe=kwargs.pop("probe", lambda p: (1080, 1920, 45.0)), **kwargs)
    queue.sleeps = sleeps
    return queue


META = {"title": "He jumped the gap", "description": "Twenty feet, no brakes.",
        "hashtags": ["bike"], "tags": ["bike jump", "stunt"]}


def approved(queue, tmp_path, name="001-clip.mp4", size=50_000):
    entry = queue.draft("proj1", make_clip(tmp_path, name, size), META)
    return queue.approve(entry["id"], confirm=True)


# ================================================================ OAuth

def test_client_file_must_be_a_desktop_client(tmp_path):
    assert youtube_auth.parse_client(CLIENT)["client_id"].endswith(".apps.googleusercontent.com")
    with pytest.raises(youtube_auth.AuthError, match="Desktop app"):
        youtube_auth.parse_client({"web": CLIENT["installed"]})
    with pytest.raises(youtube_auth.AuthError):
        youtube_auth.parse_client({"installed": {"client_id": "x", "client_secret": "y"}})


def test_client_secret_is_stored_encrypted(account):
    raw = account.client_path.read_bytes()
    assert b"GOCSPX-super-secret" not in raw and b"1234-abc" not in raw
    assert account.client()["client_secret"] == "GOCSPX-super-secret"


def test_signin_uses_pkce_state_loopback_and_minimal_scopes(account, google):
    query = sign_in(account, google)
    assert query["code_challenge_method"] == "S256"
    assert len(query["state"]) >= 24
    assert query["redirect_uri"].startswith("http://127.0.0.1:")
    assert set(query["scope"].split()) == set(youtube_auth.SCOPES)
    assert query["access_type"] == "offline"
    status = account.status()
    assert status["connected"] and status["channel"] == {"id": "UC123", "title": "My Channel"}


def test_tokens_are_encrypted_and_never_exposed(connected, google):
    raw = connected.token_path.read_bytes()
    assert google.refresh.encode() not in raw and google.access.encode() not in raw
    text = json.dumps(connected.status())
    for secret in (google.refresh, google.access, "GOCSPX-super-secret"):
        assert secret not in text


def test_wrong_state_is_rejected(account, google):
    url = account.begin_signin()
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    google.challenge = query["code_challenge"]
    with urllib.request.urlopen(f"{query['redirect_uri']}/?state=forged&code=good-code",
                                timeout=10) as response:
        assert b"could not connect" in response.read()
    for _ in range(100):
        if account.status()["signin"]["state"] != "waiting":
            break
        time.sleep(0.05)
    assert account.status()["signin"]["state"] == "error"
    assert not account.status()["connected"]


def test_cancelled_signin_reports_an_error(account, google):
    url = account.begin_signin()
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    urllib.request.urlopen(f"{query['redirect_uri']}/?state={query['state']}&error=access_denied",
                           timeout=10).read()
    for _ in range(100):
        if account.status()["signin"]["state"] != "waiting":
            break
        time.sleep(0.05)
    assert "cancelled" in account.status()["signin"]["message"]


def test_missing_upload_permission_is_refused(account, google):
    google.scope = youtube_auth.SCOPES[1]          # read-only granted
    sign_in(account, google)
    assert not account.status()["connected"]
    assert "Upload permission" in account.status()["signin"]["message"]


def test_access_token_is_cached_then_refreshed(connected, google, clock):
    first = connected.access_token()
    assert connected.access_token() == first and google.refreshes == 0
    clock.now += 3600
    assert connected.access_token() != first and google.refreshes == 1


def test_api_call_refreshes_once_on_401(connected, google):
    google.access = "something-else"               # server-side invalidation
    status, _, _ = connected.authorized("GET", f"{API}/channels?part=snippet&mine=true")
    assert status == 200 and google.refreshes == 1


def test_revoked_access_asks_to_reconnect(connected, google, clock):
    google.revoked = True
    clock.now += 4000
    with pytest.raises(youtube_auth.AuthError, match="Connect the channel again"):
        connected.access_token()
    assert not connected.status()["connected"]


def test_disconnect_revokes_and_deletes_tokens(connected, google):
    result = connected.disconnect()
    assert result == {"disconnected": True, "revoked": True}
    assert google.revoked and not connected.token_path.exists()
    assert not connected.status()["connected"]


def test_protect_round_trip_and_tamper_detection():
    blob = youtube_auth.protect(b"hello")
    assert blob != b"hello" and youtube_auth.unprotect(blob) == b"hello"
    with pytest.raises(youtube_auth.AuthError):
        youtube_auth.unprotect(b"garbage")


def test_config_folder_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CHOPIFY_CONFIG", str(tmp_path))
    assert youtube_auth.config_dir() == tmp_path


def test_no_hardcoded_credentials_in_the_code():
    import re
    real_client_id = re.compile(r"\d{6,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com")
    real_secret = re.compile(r"GOCSPX-[A-Za-z0-9_-]{10,}")
    token = re.compile(r"ya29\.[A-Za-z0-9_-]{20,}|1//[A-Za-z0-9_-]{20,}")
    for name in ("youtube_auth.py", "youtube_upload.py", "publish_meta.py", "dashboard.py",
                 "assets/studio/app.js"):
        text = (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")
        assert not real_client_id.search(text), name
        assert not real_secret.search(text), name
        assert not token.search(text), name


def test_secrets_are_git_ignored():
    ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
    for pattern in ("client_secret*.json", "*token*.json", "youtube_*.bin"):
        assert pattern in ignore


# ================================================================ approval

def test_drafts_never_upload(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    queue.draft("proj1", make_clip(tmp_path), META)
    calls = len(google.calls)
    assert queue.process() == []
    assert len(google.calls) == calls and google.inserts == []


def test_approval_needs_explicit_confirmation(connected, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = queue.draft("proj1", make_clip(tmp_path), META)
    for confirm in (False, None, "yes", 1):
        with pytest.raises(UploadError, match="explicit confirmation"):
            queue.approve(entry["id"], confirm=confirm)
    assert queue.get(entry["id"])["state"] == "draft"


def test_approval_needs_a_connected_channel(account, clock, tmp_path):
    queue = queue_for(account, clock)
    entry = queue.draft("proj1", make_clip(tmp_path), META)
    with pytest.raises(UploadError, match="Connect"):
        queue.approve(entry["id"], confirm=True)


@pytest.mark.parametrize("probe,message", [
    (lambda p: (1920, 1080, 30.0), "landscape"),
    (lambda p: (1080, 1920, 240.0), "3 minutes"),
])
def test_only_shorts_are_accepted(connected, clock, tmp_path, probe, message):
    queue = queue_for(connected, clock, probe=probe)
    entry = queue.draft("proj1", make_clip(tmp_path), META)
    with pytest.raises(UploadError, match=message):
        queue.approve(entry["id"], confirm=True)


def test_clip_changed_after_drafting_is_refused(connected, clock, tmp_path):
    queue = queue_for(connected, clock)
    clip = make_clip(tmp_path)
    entry = queue.draft("proj1", clip, META)
    clip.write_bytes(b"different")
    with pytest.raises(UploadError, match="changed"):
        queue.approve(entry["id"], confirm=True)


def test_editing_approved_metadata_withdraws_the_approval(connected, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    edited = queue.update_metadata(entry["id"], {**META, "title": "New title"})
    assert edited["state"] == "draft" and edited["approved_at"] is None


# ================================================================ uploading

def test_approved_clip_uploads_in_chunks_as_private(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    seen = []
    assert queue.process(progress=lambda i, share: seen.append(share)) == [entry["id"]]
    done = queue.get(entry["id"])
    assert done["state"] == "uploaded" and done["video_id"] == "vid-1"
    assert done["url"] == "https://www.youtube.com/shorts/vid-1"
    assert seen == sorted(seen) and 0 < seen[0] < 1
    session = google.sessions["https://upload.example/session/1"]
    assert session["data"] == Path(entry["file"]).read_bytes()
    sent = google.inserts[0]
    assert sent["status"]["privacyStatus"] == "private"
    assert sent["status"]["selfDeclaredMadeForKids"] is False
    assert sent["snippet"]["title"] == "He jumped the gap"
    assert "#Shorts" in sent["snippet"]["description"]
    assert sent["snippet"]["tags"] == ["bike jump", "stunt"]
    usage = queue.usage()
    # one videos.insert call (1 unit in its own bucket), not 1600 units
    assert usage["upload_calls"] == 1 and usage["uploads_started"] == 1
    assert usage["general_units"] == 0, "uploading costs nothing in the general bucket"


def test_uploaded_clip_is_never_uploaded_again(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    queue.process()
    with pytest.raises(UploadError, match="Already uploaded"):
        queue.approve(entry["id"], confirm=True)
    with pytest.raises(UploadError):
        queue.retry(entry["id"])
    again = queue.draft("proj1", Path(entry["file"]), META)
    assert again["id"] == entry["id"], "same file -> same entry"
    copy = make_clip(tmp_path, "copy.mp4")               # identical bytes, new name
    assert queue.draft("proj2", copy, META)["id"] == entry["id"]
    queue.process()
    assert len(google.inserts) == 1


def test_interrupted_upload_resumes_without_a_second_video(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    google.raise_on_put = 99                            # network drops after session start
    google.chunk_failures = []
    queue._mutate(entry["id"], lambda e: None)
    with pytest.raises(Exception):
        # bypass the retry loop to simulate the process dying mid-upload
        queue._upload(entry["id"], None)
    session_uri = queue.get(entry["id"])["session"]
    assert session_uri and queue.get(entry["id"])["state"] == "uploading"
    # partially received on YouTube's side
    google.sessions[session_uri]["data"] = Path(entry["file"]).read_bytes()[:16_384]
    google.raise_on_put = 0
    restarted = queue_for(connected, clock)              # e.g. Studio restarted
    assert restarted.process() == [entry["id"]]
    assert len(google.inserts) == 1, "resumed, not re-inserted"
    assert google.sessions[session_uri]["data"] == Path(entry["file"]).read_bytes()


def test_finished_upload_found_after_a_crash_is_not_resent(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    queue.process()
    # Simulate: YouTube finished, but Chopify crashed before saving it.
    session = "https://upload.example/session/1"
    queue._mutate(entry["id"], lambda e: e.update(state="uploading", session=session,
                                                  video_id=None))
    puts_before = sum(1 for m, u in google.calls if m == "PUT")
    queue.process()
    assert queue.get(entry["id"])["state"] == "uploaded"
    assert sum(1 for m, u in google.calls if m == "PUT") == puts_before + 1  # status check only
    assert len(google.inserts) == 1


def test_temporary_errors_are_retried_with_backoff(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    google.chunk_failures = [503, 500]
    queue.process()
    assert queue.get(entry["id"])["state"] == "uploaded"
    assert queue.sleeps == [2.0, 4.0]
    assert len(google.inserts) == 1


def test_persistent_errors_fail_and_retry_later(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    google.chunk_failures = [503] * 50
    queue.process()
    failed = queue.get(entry["id"])
    assert failed["state"] == "failed" and failed["retryable"]
    assert failed["next_attempt"] == clock.now + youtube_upload.AUTO_RETRY_DELAY
    assert queue.due() == []
    clock.now = failed["next_attempt"]
    google.chunk_failures = []
    assert [e["id"] for e in queue.due()] == [entry["id"]]
    queue.process()
    assert queue.get(entry["id"])["state"] == "uploaded"
    assert len(google.inserts) == 1, "the same session was resumed"


def test_api_quota_exhaustion_waits_for_the_pacific_reset(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    first = approved(queue, tmp_path, "a.mp4", 1000)
    second = approved(queue, tmp_path, "b.mp4", 1200)
    google.insert_error = (403, "quotaExceeded")
    queue.process()
    waiting = queue.get(first["id"])
    assert waiting["state"] == "quota_wait"
    assert waiting["next_attempt"] == youtube_upload.next_quota_reset(clock.now)
    assert queue.get(second["id"])["state"] == "queued", "not hammered after a quota error"
    usage = queue.usage()
    assert usage["upload_bucket_exhausted"] and usage["uploads_left_today"] == 0
    assert usage["blocked_by"] == "upload_quota"
    assert usage["upload_calls"] == 1, "the refused insert still counts"
    assert usage["uploads_started"] == 0


def test_channel_upload_limit_waits_a_day(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    google.insert_error = (400, "uploadLimitExceeded")
    queue.process()
    waiting = queue.get(entry["id"])
    assert waiting["state"] == "quota_wait"
    assert waiting["next_attempt"] == clock.now + 24 * 3600


def test_configured_daily_limit_is_respected_without_calling_youtube(connected, google, clock,
                                                                   tmp_path):
    queue = queue_for(connected, clock)
    queue.update_settings({"max_uploads_per_day": 1})
    first = approved(queue, tmp_path, "a.mp4", 1000)
    second = approved(queue, tmp_path, "b.mp4", 1200)
    queue.process()
    assert queue.get(first["id"])["state"] == "uploaded"
    assert queue.get(second["id"])["state"] == "quota_wait"
    assert len(google.inserts) == 1
    clock.now = queue.get(second["id"])["next_attempt"]  # next quota day
    queue.process()
    assert queue.get(second["id"])["state"] == "uploaded"


def test_default_quota_model_matches_youtube():
    settings = youtube_upload.DEFAULT_SETTINGS
    assert settings["upload_quota_calls"] == 100       # videos.insert bucket
    assert settings["general_quota_units"] == 10000    # all other methods
    assert settings["max_uploads_per_day"] == 6        # Chopify's own limit
    assert settings["privacy"] == "private"
    assert "upload_cost" not in settings and "daily_quota_units" not in settings


def test_requests_are_charged_to_the_right_bucket():
    charge = youtube_auth.quota_charge
    insert = youtube_auth.UPLOAD_ENDPOINT + "?uploadType=resumable&part=snippet,status"
    assert charge("POST", insert) == ("videos.insert", 1)
    assert charge("PUT", youtube_auth.UPLOAD_ENDPOINT + "?uploadType=resumable&upload_id=x") is None
    assert charge("PUT", "https://upload.example/session/1") is None
    assert charge("GET", API + "/channels?part=snippet&mine=true") == ("general", 1)
    assert charge("POST", API + "/thumbnails/set?videoId=x") == ("general", 50)
    assert charge("POST", TOKEN_URI) is None and charge("POST", REVOKE_URI) is None


def test_channel_lookup_is_counted_in_the_general_bucket(account, google, clock, tmp_path):
    queue = queue_for(account, clock)                 # attaches the ledger
    sign_in(account, google)                           # calls channels.list once
    usage = queue.usage()
    assert usage["general_units"] == 1 and usage["upload_calls"] == 0


def test_app_limit_and_youtube_upload_bucket_are_independent(connected, clock):
    queue = queue_for(connected, clock)
    queue.update_settings({"max_uploads_per_day": 6, "upload_quota_calls": 2})
    assert queue.usage()["uploads_left_today"] == 2        # YouTube bucket is smaller
    queue.update_settings({"max_uploads_per_day": 1, "upload_quota_calls": 100})
    assert queue.usage()["uploads_left_today"] == 1        # Chopify limit is smaller
    usage = queue.usage()
    assert usage["upload_calls_left"] == 100 and usage["app_uploads_left"] == 1


def test_failed_and_retried_inserts_each_count(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    responses = iter([(503, "backendError"), (500, "backendError"), None])
    original = google.__call__

    def flaky(method, url, headers=None, body=None, timeout=60):
        if method == "POST" and url.startswith(UPLOAD_URI):
            failure = next(responses)
            if failure:
                return failure[0], {}, json.dumps({"error": {"errors": [
                    {"reason": failure[1]}]}}).encode()
        return original(method, url, headers, body, timeout)
    queue.account.http = flaky
    queue.process()
    usage = queue.usage()
    assert queue.get(entry["id"])["state"] == "uploaded"
    assert usage["upload_calls"] == 3, "two failed inserts + the successful one"
    assert usage["uploads_started"] == 1, "only one upload actually started"


def test_resuming_does_not_spend_more_upload_quota(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    google.raise_on_put = 99
    with pytest.raises(Exception):
        queue._upload(entry["id"], None)
    assert queue.usage()["upload_calls"] == 1
    google.raise_on_put = 0
    queue.process()
    usage = queue.usage()
    assert queue.get(entry["id"])["state"] == "uploaded"
    assert usage["upload_calls"] == 1 and usage["uploads_started"] == 1
    assert len(google.inserts) == 1


def test_upload_bucket_limit_blocks_before_calling_youtube(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    queue.update_settings({"upload_quota_calls": 1})
    first = approved(queue, tmp_path, "a.mp4", 1000)
    second = approved(queue, tmp_path, "b.mp4", 1200)
    queue.process()
    assert queue.get(first["id"])["state"] == "uploaded"
    waiting = queue.get(second["id"])
    assert waiting["state"] == "quota_wait" and "videos.insert" in waiting["error"]
    assert waiting["next_attempt"] == youtube_upload.next_quota_reset(clock.now)
    assert len(google.inserts) == 1


def test_app_limit_message_names_the_chopify_limit(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    queue.update_settings({"max_uploads_per_day": 1})
    approved(queue, tmp_path, "a.mp4", 1000)
    second = approved(queue, tmp_path, "b.mp4", 1200)
    queue.process()
    assert "Chopify's limit of 1 upload" in queue.get(second["id"])["error"]


def test_quota_counters_reset_on_the_pacific_day(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    approved(queue, tmp_path)
    queue.process()
    assert queue.usage()["upload_calls"] == 1
    clock.now = queue.usage()["resets_at"]
    usage = queue.usage()
    assert usage["upload_calls"] == 0 and usage["uploads_started"] == 0
    assert usage["general_units"] == 0 and usage["uploads_left_today"] == 6


def test_old_ledger_and_settings_files_still_load(connected, clock):
    queue = queue_for(connected, clock)
    day = youtube_upload.quota_day(clock.now)
    (queue.dir / youtube_upload.USAGE_FILE).write_text(json.dumps(
        {day: {"units": 1600, "uploads": 1, "exhausted": False}}))
    (queue.dir / youtube_upload.SETTINGS_FILE).write_text(json.dumps(
        {"daily_quota_units": 10000, "upload_cost": 1600, "max_uploads_per_day": 4}))
    usage = queue.usage()
    assert usage["max_uploads_per_day"] == 4            # still honoured
    assert usage["upload_quota_calls"] == 100           # old keys ignored
    assert "units" not in usage and usage["uploads_left_today"] >= 1


def test_privacy_is_always_sent_explicitly(connected, google, clock, tmp_path):
    # YouTube makes an upload PUBLIC when privacyStatus is missing.
    queue = queue_for(connected, clock)
    entry = queue.draft("proj1", make_clip(tmp_path), {"title": "No privacy given"})
    queue.approve(entry["id"], confirm=True)
    queue.process()
    assert google.inserts[0]["status"]["privacyStatus"] == "private"
    queue.update_settings({"privacy": "unlisted"})
    other = queue.draft("proj1", make_clip(tmp_path, "b.mp4", 999), {"title": "b"})
    queue.approve(other["id"], confirm=True)
    queue.process()
    assert google.inserts[1]["status"]["privacyStatus"] == "unlisted"


def test_bad_metadata_fails_with_a_hint_and_no_retry(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    google.insert_error = (400, "invalidTitle")
    queue.process()
    failed = queue.get(entry["id"])
    assert failed["state"] == "failed" and "Edit the title" in failed["error"]
    assert failed["next_attempt"] is None and queue.sleeps == []


def test_expired_session_starts_a_fresh_one(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    queue._mutate(entry["id"], lambda e: e.update(
        state="uploading", session="https://upload.example/session/9"))
    google.sessions["https://upload.example/session/9"] = {"size": 1, "data": b"", "meta": {},
                                                           "done": False}
    google.expire_sessions = True
    original = google.__call__

    def expire_once(method, url, headers=None, body=None, timeout=60):
        if url.endswith("/9"):
            return 404, {}, b""
        google.expire_sessions = False
        return original(method, url, headers, body, timeout)
    queue.account.http = expire_once
    queue.process()
    assert queue.get(entry["id"])["state"] == "uploaded"
    assert len(google.inserts) == 1


def test_lost_authorisation_stops_the_upload(connected, google, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = approved(queue, tmp_path)
    google.revoked = True
    clock.now += 4000
    queue.process()
    failed = queue.get(entry["id"])
    assert failed["state"] == "failed" and "Connect the channel again" in failed["error"]


def test_cancel_and_retry_rules(connected, clock, tmp_path):
    queue = queue_for(connected, clock)
    entry = queue.draft("proj1", make_clip(tmp_path), META)
    with pytest.raises(UploadError, match="Approve"):
        queue._mutate(entry["id"], lambda e: e.update(state="failed")) and queue.retry(entry["id"])
    assert queue.cancel(entry["id"])["state"] == "cancelled"
    assert queue.process() == []


def test_settings_are_validated_and_saved(connected, clock):
    queue = queue_for(connected, clock)
    saved = queue.update_settings({"max_uploads_per_day": 3, "privacy": "unlisted"})
    assert saved["max_uploads_per_day"] == 3 and queue.settings()["privacy"] == "unlisted"
    for bad in ({"max_uploads_per_day": -1}, {"privacy": "everyone"},
                {"upload_quota_calls": -1}, {"general_quota_units": 1.5},
                {"upload_cost": 1600}, {"nonsense": 1}):
        with pytest.raises((UploadError, ValueError)):
            queue.update_settings(bad)


# ================================================================ quota day

def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("moment,day,reset", [
    (utc(2026, 7, 1, 6, 59), "2026-06-30", utc(2026, 7, 1, 7, 0)),     # summer, PDT
    (utc(2026, 12, 1, 8, 0), "2026-12-01", utc(2026, 12, 2, 8, 0)),    # winter, PST
    (utc(2026, 11, 1, 5, 0), "2026-10-31", utc(2026, 11, 1, 7, 0)),    # night DST ends
    (utc(2026, 3, 8, 7, 0), "2026-03-07", utc(2026, 3, 8, 8, 0)),      # night DST starts
])
def test_quota_day_follows_pacific_time(moment, day, reset):
    assert youtube_upload.quota_day(moment) == day
    assert youtube_upload.next_quota_reset(moment) == reset


# ================================================================ metadata

def test_metadata_is_cleaned_to_youtube_limits():
    meta = publish_meta.clean({
        "title": "<b>" + "x" * 150, "description": "Great <script> moment",
        "hashtags": ["#Gaming", "gaming", "GTA 6!", "shorts"],
        "tags": ["tag, with comma"] + [f"keyword number {i}" for i in range(60)]})
    assert len(meta["title"]) <= 100 and "<" not in meta["title"]
    assert "<" not in meta["description"] and ">" not in meta["description"]
    assert meta["hashtags"][0].lower() in {"gaming", "shorts"}
    assert len({h.lower() for h in meta["hashtags"]}) == len(meta["hashtags"])
    assert "GTA6" in meta["hashtags"]
    assert sum(len(t) + (2 if " " in t else 0) for t in meta["tags"]) + len(meta["tags"]) - 1 <= 500
    assert all("," not in t for t in meta["tags"])
    assert meta["privacy"] == "private"
    assert "#Shorts" in meta["description"] or "#shorts" in meta["description"]


def test_metadata_rejects_bad_values():
    with pytest.raises(ValueError):
        publish_meta.clean({"title": "  "})
    with pytest.raises(ValueError):
        publish_meta.clean({"title": "ok", "privacy": "everyone"})
    with pytest.raises(ValueError):
        publish_meta.clean({"title": "ok", "category_id": "999"})
    long = publish_meta.clean({"title": "ok", "description": "é" * 6000})
    assert len(long["description"].encode()) <= 5000


class FakeLLM:
    def __init__(self, reply=None, ok=True):
        self.reply, self.ok, self.prompts = reply, ok, []

    def available(self):
        return (self.ok, "" if self.ok else "Ollama is not running.")

    def chat_json(self, system, user, schema=None):
        self.prompts.append(user)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_ai_hinglish_metadata_is_kept_as_written():
    reply = {"title": "Papa ke saamne prank ulta pad gaya", "description": "Poora paisa vasool.",
             "hashtags": ["prank", "papa"], "tags": ["prank", "comedy"]}
    llm = FakeLLM(reply)
    meta = publish_meta.generate({"hook": "papa", "text": "Bhai maine socha...",
                                  "language": "hi"}, llm)
    assert meta["source"] == "ai" and meta["title"] == "Papa ke saamne prank ulta pad gaya"
    assert "papa" in meta["hashtags"] and "Shorts" in meta["hashtags"]
    assert "Detected language: hi" in llm.prompts[0] and len(llm.prompts) == 1
    assert "note" not in meta and meta["policy"] == "hinglish-roman"


def test_devanagari_from_the_model_is_retried_then_transliterated():
    # Output policy: never Devanagari. Ask again; if the model insists,
    # transliterate and tell the user to review.
    reply = {"title": "पापा के सामने prank उल्टा पड़ गया", "description": "पूरा पैसा वसूल।",
             "hashtags": ["prank", "हिंदी"], "tags": ["prank", "मज़ा"]}
    llm = FakeLLM(reply)
    meta = publish_meta.generate({"hook": "papa", "text": "Bhai maine socha...",
                                  "language": "hi"}, llm)
    assert len(llm.prompts) == 3 and "Devanagari" in llm.prompts[1]
    assert not publish_meta.devanagari_fields(meta)
    assert meta["title"] == "paapa ke saamne prank ulta pad gaya"
    assert "transliterated" in meta["note"]
    assert "maza" in meta["tags"]


def test_model_retry_can_fix_devanagari():
    good = {"title": "Papa ke saamne prank", "description": "Mast.",
            "hashtags": ["prank"], "tags": ["prank"]}
    replies = iter([{**good, "title": "पापा prank"}, good])

    class Flaky(FakeLLM):
        def chat_json(self, system, user, schema=None):
            self.prompts.append(user)
            return next(replies)
    llm = Flaky()
    meta = publish_meta.generate({"hook": "papa", "text": "x"}, llm)
    assert meta["title"] == "Papa ke saamne prank" and "note" not in meta


def test_the_model_only_ever_sees_roman_text():
    llm = FakeLLM({"title": "ok", "description": "", "hashtags": [], "tags": []})
    publish_meta.generate({"hook": "पहली बार", "text": "आज मैंने पहली बार GTA 6 खेला",
                           "summary": "भाई", "source_title": "मेरा वीडियो",
                           "language": "hi"}, llm)
    assert not any("ऀ" <= ch <= "ॿ" for ch in llm.prompts[0])
    assert "aaj maine pehli baar GTA 6 khela" in llm.prompts[0]


def test_manual_devanagari_edits_are_rejected_with_a_suggestion():
    with pytest.raises(ValueError) as exc:
        publish_meta.clean({"title": "आज मैंने पहली बार GTA 6 खेला"})
    assert "Roman" in str(exc.value) and "aaj maine pehli baar GTA 6 khela" in str(exc.value)
    for field, value in (("description", "मज़ा"), ("tags", ["ok", "मज़ा"]),
                         ("hashtags", ["भाई"])):
        with pytest.raises(ValueError):
            publish_meta.clean({"title": "fine", field: value})
    assert publish_meta.clean({"title": "Bhai kya scene hai"})["title"] == "Bhai kya scene hai"


def test_rule_fallback_metadata_is_roman():
    meta = publish_meta.generate({"hook": "भाई ये गेम", "text": "भाई ये गेम में फिसिक्स बहुत क्रेजी हैं!"},
                                 FakeLLM(ok=False))
    assert meta["source"] == "rules" and meta["title"] == "Bhai ye game"
    assert not publish_meta.devanagari_fields(meta)
    assert "physics" in meta["tags"] and "hain" not in meta["tags"]


def test_metadata_falls_back_when_ai_is_unavailable():
    info = {"hook": "nobody believed me", "text": "Nobody believed me. Then I won the race!"}
    meta = publish_meta.generate(info, FakeLLM(ok=False))
    assert meta["source"] == "rules" and meta["title"] == "Nobody believed me"
    assert "Ollama is not running" in meta["note"]
    junk = publish_meta.generate(info, FakeLLM(reply={"title": ""}))
    assert junk["source"] == "rules"


# ================================================================ Studio

@pytest.fixture
def server(tmp_path):
    import dashboard
    httpd = dashboard.make_server(tmp_path / "projects", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()
    httpd.app.close()
    thread.join()


def call(base, path, data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Chopify-Token"] = token
    request = urllib.request.Request(base + path, headers=headers,
                                     data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def exported_project(httpd, tmp_path):
    import projects
    state = projects.create_project(httpd.app.root, "recording.mp4", {"llm": None})
    path = httpd.app.root / state["id"]
    clip = make_clip(path, "001-abc-nobody-believed-me.mp4")
    clip.with_suffix(".meta.json").write_text(json.dumps({"start": 0.0, "end": 2.0,
                                                          "title": "nobody believed me #shorts"}))
    projects.atomic_json(path / "transcript.json", {"language": "en", "words": [
        {"word": "Nobody", "start": 0.1, "end": 0.4},
        {"word": "believed", "start": 0.5, "end": 0.9}, {"word": "me.", "start": 1.0, "end": 1.2}]})
    return state["id"], clip


def test_studio_publish_flow_needs_approval(server, tmp_path):
    httpd, base = server
    project_id, clip = exported_project(httpd, tmp_path)
    state = call(base, f"/api/youtube?project={project_id}")
    assert state["account"]["connected"] is False and state["uploads"] == []
    entry = call(base, f"/api/projects/{project_id}/publish", {"clip": clip.name},
                 httpd.app.token)
    assert entry["state"] == "draft" and entry["metadata"]["title"] == "Nobody believed me"
    assert "session" not in entry
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(base, f"/api/uploads/{entry['id']}/approve", {}, httpd.app.token)
    assert exc.value.code == 400 and "confirmation" in json.load(exc.value)["error"]
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(base, f"/api/uploads/{entry['id']}/approve", {"confirm": True}, httpd.app.token)
    assert "Connect" in json.load(exc.value)["error"]
    edited = call(base, f"/api/uploads/{entry['id']}/metadata",
                  {"metadata": {**entry["metadata"], "title": "My edit"}}, httpd.app.token)
    assert edited["metadata"]["title"] == "My edit" and edited["state"] == "draft"


def test_studio_youtube_routes_need_the_studio_token(server, tmp_path):
    httpd, base = server
    for path in ("/api/youtube/connect", "/api/youtube/disconnect",
                 "/api/youtube/client", "/api/uploads/x/approve"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            call(base, path, {"confirm": True})
        assert exc.value.code == 403


def test_studio_refuses_previews_and_paths_outside_clips(server, tmp_path):
    httpd, base = server
    project_id, _ = exported_project(httpd, tmp_path)
    for clip in ("../transcript.json", "nope.mp4", "..\\..\\x.mp4"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            call(base, f"/api/projects/{project_id}/publish", {"clip": clip}, httpd.app.token)
        assert exc.value.code == 400


def test_studio_state_never_contains_secrets(server, tmp_path, google, clock):
    httpd, base = server
    client = tmp_path / "client_secret.json"
    client.write_text(json.dumps(CLIENT))
    call(base, "/api/youtube/client", {"path": str(client)}, httpd.app.token)
    text = json.dumps(call(base, "/api/youtube"))
    assert "GOCSPX" not in text and "1234-abc" not in text
    assert json.loads(text)["account"]["client_configured"] is True
