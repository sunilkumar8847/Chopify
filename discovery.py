"""Gaming discovery through the official YouTube Data API (Phase 8).

What it does
* **Search** games and keywords (``search.list``, ``type=video``,
  ``videoCategoryId=20``), optionally only live streams
  (``eventType=live``), in the order YouTube returns for the order the user
  picked (relevance, date or view count).
* **Popular gaming videos**: YouTube's own most-popular chart for Gaming in
  the user's region (``videos.list chart=mostPopular``).
* **Watchlist** ("Add"): remembers a result's link, title and channel so the
  user can open it later. Adding never downloads, records or creates a
  project; duplicates are refused.

Policy constraints this follows (YouTube API Services Developer Policies):
* Results are shown as YouTube returned them: no re-ranking, merging with
  other sources, or scores derived from YouTube metrics (III.C.5, III.E.4.h).
* Every result is attributed to YouTube, names its channel and links to
  YouTube (III.F.2, Branding Guidelines); Studio links YouTube's Terms of
  Service (III.A.1).
* No aggregation across content owners (III.E.2): nothing is combined,
  counted or monitored per channel; results are a transient list.
* Storage: search results are cached for at most ``CACHE_SECONDS`` (1 h);
  watchlist entries are refreshed through ``videos.list`` or deleted before
  30 days (III.E.4.c/d), and a video YouTube no longer returns is removed.
* No downloads, no yt-dlp, no scraping (III.E.1, III.E.5, III.I.14).
* Quota: search.list has its own daily bucket (default 100 calls); Chopify
  keeps a ledger and refuses searches above ``search_calls_per_day``.
"""
import json
import re
import threading
import time
from pathlib import Path

import youtube_auth

CACHE_SECONDS = 3600
WATCH_MAX_AGE = 25 * 86400       # refresh before the 30-day limit
WATCH_DELETE_AGE = 29 * 86400    # never keep un-refreshed data past 29 days
SEARCH_DEFAULT_DAILY = 100
ORDERS = ("relevance", "date", "viewCount")
FILE = "discovery.json"
ATTRIBUTION = "Results from YouTube"
TERMS_URL = "https://www.youtube.com/t/terms"


class DiscoveryError(ValueError):
    pass


_LOCK = threading.RLock()        # one lock for every Discovery (Studio makes one per request)


def _clean_query(text):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not 2 <= len(text) <= 100:
        raise DiscoveryError("Type 2 to 100 characters to search.")
    return text


def _item(video_id, snippet, live=None, kind="video"):
    thumbs = snippet.get("thumbnails") or {}
    thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {})
    return {"id": video_id, "kind": kind, "title": snippet.get("title", ""),
            "channel_title": snippet.get("channelTitle", ""),
            "channel_id": snippet.get("channelId", ""),
            "published_at": snippet.get("publishedAt", ""),
            "live": live or snippet.get("liveBroadcastContent", "none"),
            "thumbnail": thumb.get("url", ""),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "channel_url": f"https://www.youtube.com/channel/{snippet.get('channelId', '')}"}


class Discovery:
    def __init__(self, data_client, directory=None, clock=time.time, daily_limit=None):
        self.client = data_client
        self.dir = Path(directory or youtube_auth.config_dir())
        self.path = self.dir / FILE
        self.clock = clock
        self.daily_limit = int(daily_limit or SEARCH_DEFAULT_DAILY)
        self.lock = _LOCK

    # ------------------------------------------------------------- storage
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        data.setdefault("cache", {})
        data.setdefault("watch", [])
        data.setdefault("usage", {})
        return self._expire(data)

    def _save(self, data):
        self.dir.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        for attempt in range(10):
            try:
                temp.replace(self.path)
                return
            except PermissionError:      # Windows: a reader has the file open
                time.sleep(0.05 * (attempt + 1))
        temp.replace(self.path)

    def _expire(self, data):
        now = self.clock()
        data["cache"] = {k: v for k, v in data["cache"].items()
                         if now - v.get("at", 0) < CACHE_SECONDS}
        data["watch"] = [w for w in data["watch"]
                         if now - w.get("refreshed_at", w.get("added_at", 0)) < WATCH_DELETE_AGE]
        return data

    def _day(self):
        import youtube_upload
        return youtube_upload.quota_day(self.clock())

    def usage(self):
        with self.lock:
            used = self._load()["usage"].get(self._day(), 0)
        return {"search_calls": used, "search_limit": self.daily_limit,
                "search_left": max(0, self.daily_limit - used)}

    def _charge_search(self, data):
        day = self._day()
        used = data["usage"].get(day, 0)
        if used >= self.daily_limit:
            raise DiscoveryError(f"Today's search limit ({self.daily_limit} searches, YouTube's "
                                 "search quota) is used up. It resets at midnight Pacific Time.")
        data["usage"] = {day: used + 1}

    # -------------------------------------------------------------- search
    def search(self, query, live_only=False, order="relevance", region=None):
        query = _clean_query(query)
        if order not in ORDERS:
            raise DiscoveryError("Order must be relevance, date or viewCount.")
        region = (region or "").upper()
        if region and not re.fullmatch(r"[A-Z]{2}", region):
            raise DiscoveryError("Region must be two letters.")
        key = json.dumps([query.lower(), live_only, order, region])
        with self.lock:
            data = self._load()
            cached = data["cache"].get(key)
            if cached:
                return {**cached["result"], "cached": True}
            self._charge_search(data)
            self._save(data)
        params = {"part": "snippet", "q": query, "type": "video", "videoCategoryId": "20",
                  "maxResults": 25, "order": order, "safeSearch": "moderate"}
        if live_only:
            params["eventType"] = "live"
        if region:
            params["regionCode"] = region
        raw = self.client._get("search", params)
        items, seen = [], set()
        for entry in raw.get("items") or []:
            video_id = (entry.get("id") or {}).get("videoId")
            if not video_id or video_id in seen:            # duplicate prevention
                continue
            seen.add(video_id)
            items.append(_item(video_id, entry.get("snippet") or {}))
        result = {"query": query, "live_only": live_only, "order": order, "items": items,
                  "attribution": ATTRIBUTION, "terms_url": TERMS_URL,
                  "fetched_at": self.clock()}
        with self.lock:
            data = self._load()
            data["cache"][key] = {"at": self.clock(), "result": result}
            self._save(data)
        return {**result, "cached": False}

    def popular(self, region):
        region = (region or "").upper()
        if not re.fullmatch(r"[A-Z]{2}", region):
            raise DiscoveryError("Choose your two-letter region for popular gaming videos.")
        key = json.dumps(["popular", region])
        with self.lock:
            cached = self._load()["cache"].get(key)
        if cached:
            return {**cached["result"], "cached": True}
        raw = self.client._get("videos", {"part": "snippet", "chart": "mostPopular",
                                          "videoCategoryId": "20", "regionCode": region,
                                          "maxResults": 25})
        items = [_item(v["id"], v.get("snippet") or {}) for v in raw.get("items") or []
                 if v.get("id")]
        result = {"region": region, "items": items, "attribution": ATTRIBUTION,
                  "terms_url": TERMS_URL, "fetched_at": self.clock()}
        with self.lock:
            data = self._load()
            data["cache"][key] = {"at": self.clock(), "result": result}
            self._save(data)
        return {**result, "cached": False}

    # ----------------------------------------------------------- watchlist
    def watchlist(self):
        with self.lock:
            try:
                before = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                before = None
            data = self._load()
            if before is not None and data != before:
                self._save(data)            # expired entries are deleted for good
            return data["watch"]

    def add(self, item):
        """Remember a result (link only). Never downloads or creates a project."""
        if not isinstance(item, dict) or not re.fullmatch(r"[\w-]{6,20}", str(item.get("id"))):
            raise DiscoveryError("That is not a YouTube result.")
        with self.lock:
            data = self._load()
            if any(w["id"] == item["id"] for w in data["watch"]):
                raise DiscoveryError("Already in your list.")
            entry = {k: str(item.get(k, ""))[:300] for k in (
                "id", "title", "channel_title", "channel_id", "thumbnail", "live")}
            entry.update(url=f"https://www.youtube.com/watch?v={entry['id']}",
                         added_at=self.clock(), refreshed_at=self.clock())
            data["watch"].append(entry)
            self._save(data)
            return entry

    def remove(self, video_id):
        with self.lock:
            data = self._load()
            before = len(data["watch"])
            data["watch"] = [w for w in data["watch"] if w["id"] != video_id]
            self._save(data)
            return before - len(data["watch"])

    def refresh(self):
        """Refresh entries older than 25 days through videos.list; drop deleted ones."""
        with self.lock:
            data = self._load()
            stale = [w for w in data["watch"]
                     if self.clock() - w["refreshed_at"] >= WATCH_MAX_AGE]
        if not stale:
            return {"refreshed": 0, "removed": 0}
        found = {}
        for start in range(0, len(stale), 50):
            ids = [w["id"] for w in stale[start:start + 50]]
            raw = self.client._get("videos", {"part": "snippet", "id": ",".join(ids)})
            for v in raw.get("items") or []:
                found[v["id"]] = v.get("snippet") or {}
        refreshed = removed = 0
        with self.lock:
            data = self._load()
            keep = []
            for w in data["watch"]:
                if w["id"] in {s["id"] for s in stale}:
                    if w["id"] not in found:
                        removed += 1
                        continue
                    snippet = found[w["id"]]
                    w.update(title=snippet.get("title", w["title"]),
                             channel_title=snippet.get("channelTitle", w["channel_title"]),
                             live=snippet.get("liveBroadcastContent", w.get("live", "none")),
                             refreshed_at=self.clock())
                    refreshed += 1
                keep.append(w)
            data["watch"] = keep
            self._save(data)
        return {"refreshed": refreshed, "removed": removed}

    def forget_all(self):
        """Delete every stored discovery result (e.g. on disconnect)."""
        with self.lock:
            self._save({"cache": {}, "watch": [], "usage": self._load()["usage"]})
