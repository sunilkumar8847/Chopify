# Chopify architecture and workflow

This is the current blueprint of the application after the Phase 0–6 upgrade:
a local-first tool that turns long videos and livestreams into vertical
Shorts and publishes them to YouTube, only with the user's approval.

## Workflow

```text
 source ──► ingest ──► transcribe ──► discover ──► select ──► refine ──► render ──► publish
 (URL,      (yt-dlp,    (faster-       (rule        (Phase 2   (Phase 3    (Phase 4    (Phase 5,
  file,      live        whisper,       scoring,     AI judge,  duration,   9:16,       approval-
  live       recorder)   chunked,       audio +      dedupe,    setup +     captions,   gated
  stream)                CPU or GPU)    visual       cap)       payoff)     loudness)   upload)
                                        evidence)
        ▲                                                                     │
        └── Phase 6: dashboard, queue control, batch, monitoring, recovery ───┘
            output policy: generated text is Roman-script Hinglish/English
```

| Stage | Module(s) | Output in the project folder |
|---|---|---|
| Ingest (Phase 1) | `ingest.py`, `live.py`, `sections.py` | `source_meta`, `recording/`, `audio.webm` / `source.mp4` |
| Transcribe | `download_and_transcribe.py` | `transcript.json` (+ `transcript.partNNN.json`) |
| Discover + score | `score_clips.py`, `evidence.py`, `visual.py` | `candidates.json` |
| Select (Phase 2) | `highlights.py`, `llm.py` | `segments.json`, `selection.json`, `highlights_cache.json` |
| Refine (Phase 3) | `boundaries.py` | clip start/end in `segments.json` |
| Output policy | `hinglish.py` | `transcript.display.json`, `hinglish_cache.json` |
| Render (Phase 4) | `render_clips.py`, `framing.py`, `captions.py` | `clips/*.mp4`, `.srt`, `.png`, `.meta.json` |
| Publish (Phase 5) | `youtube_auth.py`, `youtube_upload.py`, `publish_meta.py` | `%APPDATA%\Chopify` (encrypted) |
| Orchestration (Phase 6) | `dashboard.py`, `jobs.py`, `automation.py`, `projects.py` | `<projects>/.studio/` |

`projects.run_project` runs the stages for one project and caches each one by
a key of its inputs, so a retry continues where the previous run stopped.
`chopify.py` is the CLI and the child process Studio starts for every job.

## Studio (local web app)

`dashboard.py` serves Studio on `127.0.0.1` only. Every state-changing request
needs the per-session token and same-origin headers.

* **Job lanes** (`jobs.py`): `process` (one job at a time: transcription and
  rendering need the whole machine), `record` (up to four recordings next to
  processing) and `upload` (one at a time). A lane can be paused: the running
  job finishes, queued jobs wait and can still be cancelled.
* **Projects** are folders; `project.json` holds status, stage, progress,
  cache keys and `last_action` (what to repeat on Retry or after a restart).
* **Uploads** live in the upload queue (`youtube_upload.py`): drafts never
  upload; only an explicit, confirmed approval queues one clip.

## Phase 6: automation layer

| Feature | Where | Notes |
|---|---|---|
| Unified dashboard | `GET /api/overview`, `#dashboard` view | per-project stage dots, progress, jobs, uploads, events |
| Pause / resume | `POST /api/automation/pause` | `processing` (job lane) and `uploads` (upload starts); saved across restarts |
| Retry | `POST /api/projects/<id>/retry` | repeats the last action; cached stages are reused |
| Cancel | `POST /api/projects/<id>/cancel` | stops the child process or a queued/waiting job |
| Batch | `POST /api/batch` | one project per source; duplicates skipped |
| Duplicate prevention | `automation.source_key` | YouTube video ID or resolved file path; uploads are de-duplicated by SHA-256 (Phase 5) |
| Daily limits | `.studio/automation.json` | new sources from monitoring per day; YouTube quota and Chopify upload limit from Phase 5 |
| Channel monitoring | `automation.Monitor` | `yt-dlp --flat-playlist` of `/videos` and `/streams`; first check only records what exists |
| Event log | `.studio/events.jsonl` | append-only JSON lines, rotated at 1 MB (2 old files kept) |
| Job logs | `<project>/job.log` | appended per run with a header line, rotated at 2 MB |
| Recovery | `Studio.start_automation` | interrupted processing jobs are queued again on start (setting) |
| Diagnosis | `automation.diagnose` | failed stage + problem + what to do, from the error and log tail |

**Safety rules built into the automation layer:** monitoring and batch create
*processing* jobs only. No code path drafts or approves an upload by itself;
publishing still needs the per-clip confirmation in Studio, and new uploads
are private. Settings cannot enable automatic publishing (there is no such
setting).

## Output-language policy (Roman Hinglish)

Whisper's original transcript (`transcript.json`) is kept unchanged for
scoring and AI judging. Everything generated for people is Roman script:

* captions and SRT come from `transcript.display.json`, a word-for-word
  conversion with the same timestamps (burned-in and SRT captions share the
  same words and cue grouping);
* clip hooks, file names, `.meta.json`, AI summaries and YouTube metadata are
  converted or generated in Roman script;
* `publish_meta.clean` rejects Devanagari in titles, descriptions, tags and
  hashtags (also for manual edits) and suggests a Roman spelling.

Conversion (`hinglish.py`): rule-based Devanagari → Hinglish transliteration
with inherent-vowel deletion and dictionaries of common Hindi words and
English words that Whisper spells in Devanagari; optional LLM spelling polish
(`hinglish_ai` project setting) that is accepted word by word only when it is
Roman and sounds like the same word; a final validator removes any
Devanagari or other non-Latin script. English and Romanized Hinglish speech
pass through unchanged.

## Data locations

| What | Where |
|---|---|
| Projects, clips, logs, caches | `--projects-dir` (default `projects/`; keep it on the large drive) |
| Studio automation state | `<projects>/.studio/` |
| YouTube client + sign-in (DPAPI-encrypted) | `%APPDATA%\Chopify` or `$CHOPIFY_CONFIG` |
| Whisper / YuNet models | Hugging Face cache, `$CHOPIFY_MODELS` |
| GPU libraries (optional) | `.venv\Lib\site-packages\nvidia\*\bin`, found per process |
