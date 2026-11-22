# YouTube content access (compliance switch)

Chopify uses the YouTube Data API (uploads, upload verification), so it is an
API client. YouTube's API Developer Policies, section III.E.1, say API clients
must not "download, import, backup, cache, or store copies of YouTube
audiovisual content without YouTube's prior written approval".

Until that written approval exists, **YouTube downloads are turned off** for
new work. This is the default.

## Capabilities

| Capability | State | How |
|---|---|---|
| Public video metadata (Check source) | On | Official Data API `videos.list` (1 quota unit) |
| Own-channel monitoring | On | `channels.list?mine=true` + `playlistItems.list` + `videos.list`, your connected channel only |
| Gaming discovery | Planned | Official `search.list` / `videos.list` (discovery phase) |
| Official caption download | Deferred | `captions.download` needs permission to edit the video |
| YouTube audio/video download | Off | Needs YouTube's written approval |
| YouTube URL live recording | Off | Needs YouTube's written approval |

API requests use the connected YouTube account (scope `youtube.readonly`,
already granted) or, for public lookups, a server-side key in
`CHOPIFY_YOUTUBE_API_KEY`. Keys never reach the browser and are redacted from
logs. Every call is counted in the quota ledger. If a request fails (quota,
network, permissions) Studio shows why; there is never an unofficial
fallback (no yt-dlp, scraping or RSS).

Monitoring: YouTube Developer Policies III.E.2 do not allow aggregating API
data across different content owners, so only your own connected channel is
monitored. Other channels already in the list are kept with their history
and marked inactive with the reason. New uploads are reported with a link
(metadata only); nothing is downloaded or processed. Events keep the video
link, not API titles, to respect the 30-day API data rules.

## Turned off

| Path | Where | What you see |
|---|---|---|
| Find clips with a YouTube URL | Studio, CLI, `--batch` | "YouTube downloads are turned off..." and no project is created |
| yt-dlp metadata probe of a YouTube URL | `ingest.probe_source` | Blocked; Check source uses the official API instead |
| Audio, video, section and full-source downloads | `ingest`, `sections`, `download_and_transcribe` | Blocked before yt-dlp starts |
| Caption fetching | `subtitles.fetch` | Blocked; Whisper is used for anything already local |
| Live recording | `live`, Studio Record | Blocked before a project or process is created |
| yt-dlp channel monitoring | `automation.ytdlp_lister` | Blocked; your own channel is monitored through the official API |

Other websites, local files and recordings (OBS output folders, files on
disk) work as before. The official YouTube Data API keeps working
(publishing, verification, storage cleanup).

## Existing projects

Nothing is deleted, renamed or migrated. Projects made from YouTube URLs stay
viewable: clips play, transcripts and candidates open, titles and settings
can be edited, published clips keep their history, and Find clips reuses the
media already stored in the project. Only steps that would download from
YouTube again (for example a missing section for a new export) stop with the
explanation above.

## Turning it back on

Only after YouTube has granted written approval:

* create `%APPDATA%\Chopify\features.json` (or `$CHOPIFY_CONFIG\features.json`)
  containing `{"youtube_downloads": true}`, then restart Studio; or
* set the environment variable `CHOPIFY_YOUTUBE_DOWNLOADS=1` for the Studio
  process.

Delete the file (or set the variable to `0`) to turn it off again. Every
blocked attempt is written to the structured log as `compliance.blocked`
(`python applog.py --errors` shows warnings too).
