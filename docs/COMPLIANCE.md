# YouTube content access

**YouTube downloading is enabled** (changed on 2026-11-25 at the owner's
request; the Phase 0 switch `CHOPIFY_YOUTUBE_DOWNLOADS` / `features.json`
`youtube_downloads` no longer exists).

* **Find clips** accepts standard watch links, `youtu.be`, `/shorts/` and
  `/live/` links. The worker downloads the audio (and, for rendering, only
  the sections it needs) with yt-dlp into the project folder, then runs the
  normal pipeline: transcription, AI judging, clip selection, rendering and
  captions. It runs through the five-source scheduler with progress,
  cancellation and retries (network failures retry three times).
* **Check source** reads the link with yt-dlp; when yt-dlp cannot read it,
  the official Data API (`videos.list`) describes it instead.
* **Live**: a YouTube stream that is live now can be a live session
  (Dashboard > Live sessions > Direct media URL); yt-dlp resolves the
  stream's HLS address at every (re)connect and live clipping works as for
  OBS. The older Record button (yt-dlp recording) works again too.
* yt-dlp gets `--js-runtimes node` when Node.js is installed and Deno is not
  (`ytdlp.py`), so YouTube's full format list is available.

## The trade-off (accepted by the owner)

Chopify is a YouTube API client (uploads, upload verification, discovery).
YouTube's API Services Developer Policies, section III.E.1, say API clients
must not "download, import, backup, cache, or store copies of YouTube
audiovisual content without YouTube's prior written approval". Using both in
one application can put the Google Cloud project behind the YouTube
connection at risk. Copyright in downloaded videos is the user's
responsibility: clip videos you own or are licensed to use.

## Unchanged

* **Channel monitoring** watches your own connected channel through the
  official API (III.E.2: no combining API data across content owners); new
  uploads there now become projects. Monitoring other channels by listing
  them with yt-dlp stays off unless you turn it on:
  `features.json` -> `{"youtube_monitoring": true}` in the config folder, or
  `CHOPIFY_YOUTUBE_MONITORING=1`.
* **Discovery** and **publishing** use the official API only; "Add to list"
  in discovery still saves only the link.
