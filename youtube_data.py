"""Read-only YouTube Data API v3: public video metadata and your own channel.

Only official endpoints are used, and never audiovisual content:

* ``videos.list``        - Source Check metadata (1 quota unit per call)
* ``channels.list``      - the connected channel and its uploads playlist
                           (``mine=true``, 1 unit)
* ``playlistItems.list`` - that channel's newest uploads (1 unit)

Requests use the connected YouTube account (scope ``youtube.readonly``, which
Chopify already has) or, for public lookups only, a server-side API key in the
``CHOPIFY_YOUTUBE_API_KEY`` environment variable. The key is never sent to the
browser or written to logs. Every request is counted in the upload queue's
quota ledger. When a request fails - quota used up, network, permissions -
the caller gets a clear error; there is no unofficial fallback (no yt-dlp,
no scraping, no RSS).

Developer Policies III.E.2: API data from different content owners must not be
aggregated, so monitoring covers only the connected channel.
"""
import json
import re
from urllib.parse import urlencode

import envconfig
import youtube_auth

API_KEY_ENV = "CHOPIFY_YOUTUBE_API_KEY"
_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


class DataApiError(RuntimeError):
    """The official API could not answer; the message says why and what to do."""


def parse_duration(value):
    """ISO 8601 duration (PT1H2M3S) -> seconds, or None."""
    match = _DURATION.match(str(value or ""))
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = match.groups()
    return (int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes or 0) * 60
            + float(seconds or 0))


def _reason(body):
    try:
        error = json.loads(body.decode("utf-8")).get("error") or {}
    except (ValueError, AttributeError):
        return "", ""
    items = error.get("errors") or [{}]
    return str(items[0].get("reason") or ""), str(error.get("message") or "")


class DataClient:
    def __init__(self, account, record_usage=None, http=None, api_key=None):
        self.account = account
        self.record_usage = record_usage          # (bucket, units) for API-key calls
        self.http = http or youtube_auth.http_request
        self.api_key = api_key if api_key is not None else (envconfig.get(API_KEY_ENV) or "")

    def connected(self):
        try:
            return bool(self.account.status().get("connected"))
        except Exception:                          # noqa: BLE001 - treated as not connected
            return False

    def _get(self, resource, params, need_oauth=False):
        query = urlencode(params)
        if self.connected():
            try:
                status, _, body = self.account.authorized(
                    "GET", f"{youtube_auth.API}/{resource}?{query}", None, None, 30)
            except youtube_auth.AuthError as exc:
                raise DataApiError(f"The YouTube connection needs attention: {exc}") from exc
            except OSError as exc:
                raise DataApiError(f"YouTube could not be reached: {exc}") from exc
        elif self.api_key and not need_oauth:
            if self.record_usage:
                self.record_usage(youtube_auth.GENERAL_BUCKET, 1)
            try:
                status, _, body = self.http(
                    "GET", f"{youtube_auth.API}/{resource}?{query}&{urlencode({'key': self.api_key})}",
                    {}, None, 30)
            except OSError as exc:
                raise DataApiError(f"YouTube could not be reached: {exc}") from exc
        elif need_oauth:
            raise DataApiError("Connect your YouTube channel in Studio (Publish > Connect) "
                               "to monitor it through the official API.")
        else:
            raise DataApiError("Connect your YouTube channel in Studio (Publish > Connect), "
                               f"or set {API_KEY_ENV} on this computer, to look up YouTube "
                               "videos through the official API.")
        if status == 200:
            return json.loads(body.decode("utf-8"))
        reason, message = _reason(body)
        if reason in {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
            raise DataApiError("YouTube's daily API quota is used up (it resets at midnight "
                               "Pacific Time). Nothing else was tried.")
        if reason in {"keyInvalid", "accessNotConfigured", "forbidden"} or status in (401, 403):
            raise DataApiError(f"YouTube refused the API request ({reason or status}). "
                               "Check the API key or the channel connection.")
        raise DataApiError(f"YouTube API error {status} {reason}: {message}".strip())

    # ------------------------------------------------------------- videos
    def videos(self, video_ids):
        """Public metadata for up to 50 video IDs: {id: info}."""
        if not video_ids:
            return {}
        data = self._get("videos", {
            "part": "snippet,contentDetails,status,liveStreamingDetails,statistics",
            "id": ",".join(video_ids[:50]), "maxResults": 50})
        return {item["id"]: describe(item) for item in data.get("items") or []}

    def video(self, video_id):
        """Metadata for one video, or None when YouTube has no such public video."""
        return self.videos([video_id]).get(video_id)

    # ---------------------------------------------------------- own channel
    def own_channel(self):
        data = self._get("channels", {"part": "snippet,contentDetails", "mine": "true"},
                         need_oauth=True)
        items = data.get("items") or []
        if not items:
            raise DataApiError("The connected Google account has no YouTube channel.")
        item = items[0]
        return {"id": item["id"], "title": (item.get("snippet") or {}).get("title", ""),
                "handle": (item.get("snippet") or {}).get("customUrl", ""),
                "uploads": ((item.get("contentDetails") or {}).get("relatedPlaylists")
                            or {}).get("uploads")}

    def own_uploads(self, limit=15):
        """Newest uploads of the connected channel (official API only)."""
        channel = self.own_channel()
        if not channel["uploads"]:
            return channel, []
        data = self._get("playlistItems", {
            "part": "contentDetails", "playlistId": channel["uploads"],
            "maxResults": max(1, min(50, int(limit)))}, need_oauth=True)
        ids = [(i.get("contentDetails") or {}).get("videoId")
               for i in data.get("items") or []]
        ids = [i for i in ids if i]
        info = self.videos(ids)
        return channel, [info[i] for i in ids if i in info]


def describe(item):
    """The fields Chopify shows, from one videos.list item."""
    snippet = item.get("snippet") or {}
    status = item.get("status") or {}
    live = item.get("liveStreamingDetails") or {}
    stats = item.get("statistics") or {}
    broadcast = snippet.get("liveBroadcastContent") or "none"
    live_status = ("is_live" if broadcast == "live" else
                   "is_upcoming" if broadcast == "upcoming" else
                   "was_live" if live.get("actualEndTime") else "not_live")
    return {
        "id": item.get("id"),
        "url": f"https://www.youtube.com/watch?v={item.get('id')}",
        "title": snippet.get("title") or "",
        "channel_id": snippet.get("channelId"),
        "channel_title": snippet.get("channelTitle") or "",
        "published_at": snippet.get("publishedAt"),
        "duration": parse_duration((item.get("contentDetails") or {}).get("duration")),
        "live_status": live_status,
        "scheduled_start": live.get("scheduledStartTime"),
        "privacy_status": status.get("privacyStatus"),
        "upload_status": status.get("uploadStatus"),
        "made_for_kids": status.get("madeForKids"),
        "view_count": int(stats["viewCount"]) if str(stats.get("viewCount", "")).isdigit() else None,
        "category_id": snippet.get("categoryId"),
    }
