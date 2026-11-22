"""YouTube content-access compliance switch (Phase 0).

YouTube's API Developer Policies (III.E.1) say an API client - and Chopify
is one, because it uploads and checks videos through the YouTube Data API -
must not "download, import, backup, cache, or store copies of YouTube
audiovisual content without YouTube's prior written approval".

Until that approval exists, every unofficial YouTube access path is off for
new work:

* URL audio/video downloads, section and full-source downloads (yt-dlp)
* metadata probes of YouTube URLs through yt-dlp ("Check source")
* caption fetching through yt-dlp
* yt-dlp live recording
* yt-dlp channel monitoring

Not affected: local files and recordings, other direct sources, everything
already stored in existing projects (viewing, editing, rendering from
cached media, publishing), and the official YouTube Data API (uploads,
verification).

Turning it back on (only once YouTube's written approval exists):
* config folder (``youtube_auth.config_dir()``, %APPDATA%\\Chopify by default):
  ``features.json`` -> ``{"youtube_downloads": true}``; or
* environment variable ``CHOPIFY_YOUTUBE_DOWNLOADS=1``.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import applog

FEATURES_FILE = "features.json"
ENV = "CHOPIFY_YOUTUBE_DOWNLOADS"
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "youtubekids.com")
MESSAGE = ("YouTube downloads are turned off. Chopify does not download, record or "
           "read YouTube videos, captions or channels without YouTube's written "
           "approval (YouTube API Developer Policies, section III.E.1). Use a local "
           "recording, an OBS output folder or another authorized direct source. "
           "Existing projects stay available.")


class YouTubeBlocked(ValueError):
    """An unofficial YouTube access path was attempted while it is off."""

    def __init__(self, operation=""):
        self.operation = operation
        super().__init__(MESSAGE)


def is_youtube(url):
    """True for any YouTube web address (videos, live, shorts, channels)."""
    try:
        host = (urlparse(str(url).strip()).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS)


def _config_file():
    import youtube_auth
    return Path(youtube_auth.config_dir()) / FEATURES_FILE


def youtube_downloads_allowed():
    """Off unless explicitly enabled (environment variable or features.json)."""
    value = os.environ.get(ENV)
    if value is not None:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        data = json.loads(_config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):        # missing, unreadable, not UTF-8, not JSON
        return False
    # Anything but a JSON object with a literal true keeps the restriction.
    return isinstance(data, dict) and data.get("youtube_downloads") is True


def blocked(url):
    return is_youtube(url) and not youtube_downloads_allowed()


def require(url, operation):
    """Raise YouTubeBlocked before any unofficial access to a YouTube URL."""
    if blocked(url):
        applog.warning("compliance.blocked", f"Blocked YouTube {operation}",
                       access=operation, url=str(url)[:200])
        raise YouTubeBlocked(operation)


# Each YouTube capability on its own (Phase 0.1). Only the audiovisual paths
# depend on the download switch; the rest use official APIs or wait.
CAPABILITIES = {
    "metadata_lookup": ("official_api", "Check source uses YouTube Data API videos.list."),
    "own_channel_monitoring": ("official_api", "Your connected channel only: "
                               "channels.list + playlistItems.list + videos.list."),
    "discovery": ("planned", "Official search.list / videos.list in the discovery phase."),
    "caption_download": ("deferred", "captions.download needs permission to edit the "
                         "video; not used. Speech-to-text is used instead."),
    "audiovisual_download": ("approval_required", "Off until YouTube grants written approval."),
    "url_live_recording": ("approval_required", "Off until YouTube grants written approval."),
}
EXTERNAL_CHANNEL = ("Only your own connected channel can be monitored through the official "
                    "YouTube API: YouTube Developer Policies III.E.2 do not allow combining "
                    "API data across different content owners. This channel is kept, "
                    "not deleted, and can be monitored again if that changes.")


def status():
    """What the Studio UI shows."""
    allowed = youtube_downloads_allowed()
    caps = {name: {"state": state, "note": note} for name, (state, note) in CAPABILITIES.items()}
    if allowed:
        for name in ("audiovisual_download", "url_live_recording"):
            caps[name]["state"] = "enabled"
    return {"youtube_downloads": allowed, "message": None if allowed else MESSAGE,
            "capabilities": caps}
