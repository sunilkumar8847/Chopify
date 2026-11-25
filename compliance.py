"""YouTube access rules.

**YouTube downloads are enabled.** On 2026-11-25 the user asked for the
Phase 0 download switch to be removed: Find clips, live recording, section
and caption downloads now use yt-dlp for YouTube URLs (standard videos,
youtu.be, Shorts and live streams) like any other source.

The user accepted the trade-off: YouTube's API Services Developer Policies
(III.E.1) do not allow an API client - Chopify is one, for uploads, Check
source fallback and discovery - to download YouTube audiovisual content
without YouTube's written approval, so this can affect the Google Cloud
project behind the YouTube connection. Copyright in downloaded videos is the
user's responsibility.

Still official-API only, unchanged:
* channel monitoring covers the connected channel only (III.E.2). Monitoring
  other channels by listing them with yt-dlp stays off unless the user turns
  it on explicitly: ``features.json`` -> ``{"youtube_monitoring": true}`` in
  the config folder, or ``CHOPIFY_YOUTUBE_MONITORING=1``;
* discovery (search.list / videos.list) and publishing.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

FEATURES_FILE = "features.json"
MONITOR_ENV = "CHOPIFY_YOUTUBE_MONITORING"
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "youtubekids.com")


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


def external_monitoring_allowed():
    """Monitoring channels other than your own (yt-dlp listing). Off unless enabled."""
    value = os.environ.get(MONITOR_ENV)
    if value is not None:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        data = json.loads(_config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):        # missing, unreadable, not UTF-8, not JSON
        return False
    # Anything but a JSON object with a literal true keeps it off.
    return isinstance(data, dict) and data.get("youtube_monitoring") is True


CAPABILITIES = {
    "metadata_lookup": ("enabled", "Check source reads YouTube links with yt-dlp; the official "
                        "Data API is used when yt-dlp cannot read the link."),
    "audiovisual_download": ("enabled", "Find clips downloads YouTube videos with yt-dlp."),
    "url_live_recording": ("enabled", "YouTube live streams can be recorded."),
    "own_channel_monitoring": ("official_api", "Your connected channel only: "
                               "channels.list + playlistItems.list + videos.list."),
    "discovery": ("official_api", "search.list / videos.list."),
    "caption_download": ("enabled", "Caption tracks are fetched with yt-dlp when available."),
}
EXTERNAL_CHANNEL = ("Only your own connected channel can be monitored through the official "
                    "YouTube API: YouTube Developer Policies III.E.2 do not allow combining "
                    "API data across different content owners. This channel is kept, "
                    "not deleted, and can be monitored again if that changes.")


def status():
    """What the Studio UI shows."""
    caps = {name: {"state": state, "note": note} for name, (state, note) in CAPABILITIES.items()}
    return {"youtube_downloads": True, "message": None,
            "external_monitoring": external_monitoring_allowed(), "capabilities": caps}
