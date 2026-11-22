"""Phase 8: gaming discovery through the official Data API (client faked)."""
import json
from pathlib import Path

import pytest

import discovery
from discovery import Discovery, DiscoveryError


class FakeClient:
    def __init__(self):
        self.calls = []
        self.missing = set()

    def _get(self, resource, params, need_oauth=False):
        self.calls.append((resource, dict(params)))
        if resource == "search":
            items = [{"id": {"videoId": f"vid{i:08d}"}, "snippet": {
                "title": f"GTA 6 moment {i}", "channelTitle": f"Creator {i}",
                "channelId": f"UC{i}", "liveBroadcastContent": "live" if params.get(
                    "eventType") else "none",
                "thumbnails": {} if getattr(self, "no_thumbnails", False) else {"medium": {"url": f"https://i.ytimg.com/vi/vid{i:08d}/mq.jpg"}}}}
                for i in range(3)]
            items.append(items[0])                       # YouTube can repeat an item
            return {"items": items}
        if resource == "videos":
            if params.get("chart") == "mostPopular":
                return {"items": [{"id": "pop00000001", "snippet": {"title": "Top", "channelTitle": "C",
                                                             "channelId": "UCp"}}]}
            ids = params["id"].split(",")
            return {"items": [{"id": i, "snippet": {"title": f"{i} renamed", "channelTitle": "C"}}
                              for i in ids if i not in self.missing]}
        raise AssertionError(resource)


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def finder(tmp_path):
    clock = Clock()
    client = FakeClient()
    found = Discovery(client, tmp_path, clock=clock, daily_limit=3)
    found.fake, found.tick = client, clock
    return found


def test_search_uses_the_official_api_as_returned_with_attribution(finder):
    result = finder.search("  GTA 6 ", live_only=True, order="date", region="in")
    resource, params = finder.fake.calls[0]
    assert resource == "search" and params["q"] == "GTA 6" and params["eventType"] == "live"
    assert params["videoCategoryId"] == "20" and params["order"] == "date"
    assert params["regionCode"] == "IN"
    assert [i["id"] for i in result["items"]] == ["vid00000000", "vid00000001", "vid00000002"], \
        "YouTube's order, duplicates dropped, nothing re-ranked"
    first = result["items"][0]
    assert first["url"] == "https://www.youtube.com/watch?v=vid00000000"
    assert first["channel_title"] == "Creator 0" and first["live"] == "live"
    assert result["attribution"] == "Results from YouTube"
    assert result["terms_url"] == "https://www.youtube.com/t/terms"


def test_results_are_cached_briefly_and_searches_are_counted(finder):
    finder.search("gta 6")
    finder.search("GTA 6")
    assert len(finder.fake.calls) == 1 and finder.usage()["search_calls"] == 1
    finder.tick.now += discovery.CACHE_SECONDS + 1
    finder.search("gta 6")
    assert len(finder.fake.calls) == 2
    finder.search("minecraft")
    with pytest.raises(DiscoveryError, match="search limit"):
        finder.search("valorant")
    assert len(finder.fake.calls) == 3, "nothing sent past the daily limit"


@pytest.mark.parametrize("bad", ["", "x", "y" * 101])
def test_queries_are_validated(finder, bad):
    with pytest.raises(DiscoveryError):
        finder.search(bad)
    assert finder.fake.calls == []


def test_popular_is_youtubes_own_chart(finder):
    result = finder.popular("us")
    assert finder.fake.calls[0] == ("videos", {"part": "snippet", "chart": "mostPopular",
                                               "videoCategoryId": "20", "regionCode": "US",
                                               "maxResults": 25})
    assert result["items"][0]["id"] == "pop00000001"
    with pytest.raises(DiscoveryError):
        finder.popular("")


def test_add_remembers_links_only_and_refuses_duplicates(finder, tmp_path):
    item = finder.search("gta 6")["items"][1]
    saved = finder.add(item)
    assert saved["url"] == "https://www.youtube.com/watch?v=vid00000001"
    with pytest.raises(DiscoveryError, match="Already"):
        finder.add(item)
    with pytest.raises(DiscoveryError):
        finder.add({"id": "../../etc"})
    assert finder.remove("vid00000001") == 1 and finder.watchlist() == []
    assert not any(p.suffix in (".mp4", ".webm", ".m4a") for p in tmp_path.rglob("*"))


def test_stored_results_are_refreshed_or_deleted_within_30_days(finder):
    for item in finder.search("gta 6")["items"]:
        finder.add(item)
    finder.fake.missing = {"vid00000002"}
    finder.tick.now += discovery.WATCH_MAX_AGE + 1
    assert finder.refresh() == {"refreshed": 2, "removed": 1}
    titles = {w["id"]: w["title"] for w in finder.watchlist()}
    assert titles == {"vid00000000": "vid00000000 renamed", "vid00000001": "vid00000001 renamed"}
    finder.tick.now += discovery.WATCH_DELETE_AGE + 1
    assert finder.watchlist() == [], "never kept past the limit without a refresh"


def test_forget_all_deletes_stored_api_data(finder):
    finder.add(finder.search("gta 6")["items"][0])
    finder.forget_all()
    data = json.loads(Path(finder.path).read_text(encoding="utf-8"))
    assert data["watch"] == [] and data["cache"] == {}


def test_discovery_never_downloads_or_scrapes():
    source = Path(discovery.__file__).read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    for word in ("yt_dlp", "yt-dlp", "subprocess", "urlopen", "http_request", "ffmpeg"):
        assert word not in code
