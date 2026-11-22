# YouTube content access (compliance switch)

Chopify uses the YouTube Data API (uploads, upload verification), so it is an
API client. YouTube's API Developer Policies, section III.E.1, say API clients
must not "download, import, backup, cache, or store copies of YouTube
audiovisual content without YouTube's prior written approval".

Until that written approval exists, **YouTube downloads are turned off** for
new work. This is the default.

## Turned off

| Path | Where | What you see |
|---|---|---|
| Find clips with a YouTube URL | Studio, CLI, `--batch` | "YouTube downloads are turned off..." and no project is created |
| Check source on a YouTube URL | Studio | The same explanation; no metadata is read |
| Audio, video, section and full-source downloads | `ingest`, `sections`, `download_and_transcribe` | Blocked before yt-dlp starts |
| Caption fetching | `subtitles.fetch` | Blocked; Whisper is used for anything already local |
| Live recording | `live`, Studio Record | Blocked before a project or process is created |
| Channel monitoring | `automation.Monitor` | Not scheduled; channels and their history are kept |

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
