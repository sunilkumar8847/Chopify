"""Phase 0.1: official YouTube Data API for Source Check and own-channel monitoring."""
import json
import subprocess
import threading
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

import applog
import automation
import compliance
import dashboard
import projects
import youtube_auth
import youtube_data

pytestmark = pytest.mark.compliance_default

VIDEO = "aaaaaaaaaaa"
OWN = "UCownownownownownownown1"
UPLOADS = "UUownownownownownownown1"


def video_item(vid, title="My upload", channel=OWN, live="none", duration="PT1H2M3S"):
    return {"id": vid, "snippet": {"title": title, "channelId": channel,
                                   "channelTitle": "My Channel", "publishedAt": "2026-11-20T10:00:00Z",
                                   "liveBroadcastContent": live, "categoryId": "20"},
            "contentDetails": {"duration": duration},
            "status": {"privacyStatus": "public", "uploadStatus": "processed", "madeForKids": False},
            "statistics": {"viewCount": "1234"}}


class FakeAccount:
    """Stands in for YouTubeAccount: answers official endpoints only."""

    def __init__(self, connected=True, videos=None, uploads=(), quota=False):
        self.connected = connected
        self.videos = {v["id"]: v for v in (videos or [])}
        self.uploads = list(uploads)
        self.quota = quota
        self.calls = []

    def status(self):
        return {"connected": self.connected}

    def authorized(self, method, url, headers=None, body=None, timeout=120):
        assert url.startswith(youtube_auth.API + "/"), url
        self.calls.append(url)
        if self.quota:
            return 403, {}, json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}],
                                                  "message": "quota"}}).encode()
        parts = urlsplit(url)
        resource = parts.path.rsplit("/", 1)[-1]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if resource == "videos":
            items = [self.videos[i] for i in query["id"].split(",") if i in self.videos]
        elif resource == "channels":
            assert query.get("mine") == "true", "only the connected channel is asked about"
            items = [{"id": OWN, "snippet": {"title": "My Channel", "customUrl": "@mychannel"},
                      "contentDetails": {"relatedPlaylists": {"uploads": UPLOADS}}}]
        elif resource == "playlistItems":
            assert query["playlistId"] == UPLOADS
            items = [{"contentDetails": {"videoId": v}} for v in self.uploads]
        else:
            raise AssertionError(f"unexpected endpoint {resource}")
        return 200, {}, json.dumps({"items": items}).encode()


@pytest.fixture(autouse=True)
def yt_dlp_cannot_read(monkeypatch):
    """Check source reads YouTube with yt-dlp first; these tests cover the
    official videos.list fallback used when yt-dlp cannot read the link."""
    import ingest
    monkeypatch.setattr(ingest, "probe_source", lambda source: {
        "source": source, "is_url": True, "probe_error": "yt-dlp could not read it",
        "live_status": None, "duration": None, "title": None})


@pytest.fixture
def no_processes(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a process was started: {args[0] if args else kwargs}")
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


# -------------------------------------------------------- Source Check

def test_source_check_uses_videos_list_and_shows_metadata(no_processes):
    account = FakeAccount(videos=[video_item(VIDEO, "Big race", live="none")])
    result = dashboard.detect_source(f"https://youtu.be/{VIDEO}", youtube_data.DataClient(account))
    assert result["label"] == "YouTube video" and result["title"] == "Big race"
    assert result["channel"] == "My Channel" and result["duration"] == 3723
    assert "1:02:03" in result["message"] and "1,234 views" in result["message"]
    assert result["blocked"] is False, "YouTube downloads are enabled"
    assert result["can_record"] is False and result["metadata_source"] == "youtube_data_api"
    assert len(account.calls) == 1 and "/videos?" in account.calls[0]
    assert "part=snippet%2CcontentDetails%2Cstatus" in account.calls[0]


@pytest.mark.parametrize("live,label", [("live", "Live now on YouTube"),
                                        ("upcoming", "Upcoming YouTube stream")])
def test_source_check_reports_live_state(live, label, no_processes):
    account = FakeAccount(videos=[video_item(VIDEO, live=live)])
    result = dashboard.detect_source(f"https://www.youtube.com/live/{VIDEO}",
                                     youtube_data.DataClient(account))
    assert result["label"] == label and result["can_record"] is False


def test_missing_video_and_non_video_links_are_explained(no_processes):
    client = youtube_data.DataClient(FakeAccount())
    result = dashboard.detect_source(f"https://www.youtube.com/watch?v={VIDEO}", client)
    assert result["label"] == "Video not found" and VIDEO in result["message"]
    result = dashboard.detect_source("https://www.youtube.com/@somebody", client)
    assert result["label"] == "Not a YouTube video link"


def test_quota_or_connection_failure_reports_the_yt_dlp_problem(no_processes):
    """Both readers failed: the yt-dlp problem is reported, nothing crashes."""
    for client in (youtube_data.DataClient(FakeAccount(quota=True)),
                   youtube_data.DataClient(FakeAccount(connected=False), api_key="")):
        result = dashboard.detect_source(f"https://youtu.be/{VIDEO}", client)
        assert result["probe_error"] == "yt-dlp could not read it"


def test_api_key_stays_on_the_server_and_out_of_logs(no_processes, monkeypatch):
    key = "AIzaSyTESTKEY0000000000000000000000000"
    seen, usage = [], []

    def http(method, url, headers, body, timeout):
        seen.append(url)
        applog.info("test.http", f"GET {url}")             # even if someone logs the URL
        return 200, {}, json.dumps({"items": [video_item(VIDEO)]}).encode()
    client = youtube_data.DataClient(FakeAccount(connected=False), record_usage=lambda *a:
                                     usage.append(a), http=http, api_key=key)
    result = dashboard.detect_source(f"https://youtu.be/{VIDEO}", client)
    assert seen and f"key={key}" in seen[0], "sent to YouTube"
    assert key not in json.dumps(result), "never in the response to the browser"
    assert usage == [(youtube_auth.GENERAL_BUCKET, 1)], "counted in the quota ledger"
    raw = "".join(p.read_text(encoding="utf-8") for p in applog.log_dir().glob("*.jsonl"))
    assert key not in raw and "key=[redacted]" in raw


def test_studio_check_source_endpoint_uses_the_official_client(tmp_path, monkeypatch, no_processes):
    monkeypatch.setattr(dashboard.Studio, "_run", lambda *a: 0)
    account = FakeAccount(videos=[video_item(VIDEO, "From the API")])
    monkeypatch.setattr(dashboard.Studio, "youtube_data",
                        lambda self: youtube_data.DataClient(account))
    httpd = dashboard.make_server(tmp_path / "projects", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        req = urllib.request.Request(base + "/api/detect", data=json.dumps(
            {"source": f"https://youtu.be/{VIDEO}"}).encode(), headers={
            "Content-Type": "application/json", "X-Chopify-Token": httpd.app.token})
        body = json.load(urllib.request.urlopen(req))
        assert body["title"] == "From the API" and httpd.app.list_projects() == []
        config = json.load(urllib.request.urlopen(base + "/api/config"))
        assert "AIza" not in json.dumps(config) and "key" not in json.dumps(config["compliance"])
    finally:
        httpd.shutdown()
        httpd.server_close()
        httpd.app.close()
        thread.join()


# -------------------------------------------------- own-channel monitoring

def monitor_with(tmp_path, account, channels=()):
    settings = automation.Settings(tmp_path)
    settings.save_channels(list(channels))
    monitor = automation.Monitor(settings)
    monitor.official = youtube_data.DataClient(account)
    return settings, monitor


EXTERNAL = {"id": "@other", "url": "https://www.youtube.com/@other", "label": "Other",
            "added": 1, "baseline": True, "seen": ["x1", "x2"], "last_check": 5,
            "last_error": None, "found": 4}


def test_own_channel_is_added_and_new_uploads_found_through_official_calls(tmp_path, no_processes):
    account = FakeAccount(videos=[video_item("vid00000001"), video_item("vid00000002")],
                          uploads=["vid00000001"])
    settings, monitor = monitor_with(tmp_path, account)
    entry = monitor.add("mine")
    assert entry["id"] == OWN and entry["own"] and entry["source"] == "youtube_data_api"
    assert monitor.add.__self__ is monitor
    with pytest.raises(ValueError, match="already monitored"):
        monitor.add("https://www.youtube.com/@mychannel")
    handled = []
    monitor.check(lambda kind, e, c: handled.append((kind, e["id"])) or True)
    assert handled == [], "first check only records what exists"
    account.uploads = ["vid00000002", "vid00000001"]
    monitor.check(lambda kind, e, c: handled.append((kind, e["id"])) or True)
    assert handled == [("video", "vid00000002")]
    monitor.check(lambda kind, e, c: handled.append((kind, e["id"])) or True)
    assert handled == [("video", "vid00000002")], "never reported twice"
    endpoints = {urlsplit(u).path.rsplit("/", 1)[-1] for u in account.calls}
    assert endpoints == {"channels", "playlistItems", "videos"}


def test_external_channels_are_kept_but_marked_inactive(tmp_path, no_processes):
    account = FakeAccount(uploads=[])
    settings, monitor = monitor_with(tmp_path, account, [EXTERNAL])
    with pytest.raises(ValueError, match="III.E.2"):
        monitor.add("https://www.youtube.com/@other")
    results = monitor.check(lambda *a: True)
    stored = settings.channels()[0]
    assert stored["inactive"] and "III.E.2" in stored["inactive_reason"]
    assert {k: stored[k] for k in EXTERNAL} == EXTERNAL, "history and settings untouched"
    assert results == [{"channel": "@other", "inactive": True}]
    endpoints = [urlsplit(u).path.rsplit("/", 1)[-1] for u in account.calls]
    assert "playlistItems" in endpoints and all("other" not in u for u in account.calls), \
        "no API data about the other creator is requested"


def test_monitor_quota_failure_changes_nothing_and_never_falls_back(tmp_path, no_processes):
    own = {"id": OWN, "url": f"https://www.youtube.com/channel/{OWN}", "label": "Mine",
           "added": 1, "baseline": True, "seen": ["vid00000001"], "last_check": 5,
           "last_error": None, "found": 0, "own": True}
    settings, monitor = monitor_with(tmp_path, FakeAccount(quota=True), [own, EXTERNAL])
    results = monitor.check(lambda *a: pytest.fail("nothing may be handled"))
    channels = {c["id"]: c for c in settings.channels()}
    assert "quota" in channels[OWN]["last_error"]
    assert channels["@other"] == EXTERNAL, "not touched without knowing the own channel"
    assert all("quota" in r.get("error", "") for r in results)


def test_studio_turns_new_uploads_into_projects(tmp_path, monkeypatch, no_processes):
    account = FakeAccount(videos=[video_item("vid00000003")], uploads=["vid00000003"])
    monkeypatch.setattr(dashboard.Studio, "youtube_data",
                        lambda self: youtube_data.DataClient(account))
    app = dashboard.Studio(tmp_path / "projects")
    try:
        app.monitor.official = youtube_data.DataClient(account)
        app.settings.save_channels([{
            "id": OWN, "url": f"https://www.youtube.com/channel/{OWN}", "label": "Mine",
            "added": 1, "baseline": True, "seen": [], "last_check": None,
            "last_error": None, "found": 0, "own": True}])
        app.settings.update({"monitor_enabled": True, "new_source_action": "prepare"})
        queued = []
        monkeypatch.setattr(app, "enqueue", lambda pid, action, *a, **k: queued.append(
            (pid, action)))
        app.check_channels()
        sources = [p["source"] for p in app.list_projects()]
        assert sources == ["https://www.youtube.com/watch?v=vid00000003"], \
            "a new upload on your channel becomes a project (downloads are enabled)"
        assert queued and queued[0][1] == "prepare"
        events = [e["message"] for e in app.events.tail(50)]
        assert any("new video" in m for m in events)
        app._schedule_monitor()
        assert app.monitor_timer is not None, "own channel monitoring is scheduled"
    finally:
        app.close()


def test_duration_parser():
    assert youtube_data.parse_duration("PT45S") == 45
    assert youtube_data.parse_duration("PT1H") == 3600
    assert youtube_data.parse_duration("P1DT1M") == 86460
    assert youtube_data.parse_duration("") is None and youtube_data.parse_duration("PT") is None
