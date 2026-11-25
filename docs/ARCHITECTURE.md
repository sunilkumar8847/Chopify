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

## Upgrade Phase 2: persistent five-source scheduler

`scheduler.py` (feature flag `scheduler`, on; `features.py`) replaces the
in-memory "process" lane in Studio. State lives in `projects/.scheduler.db`
(SQLite WAL), so the queue survives restarts.

* **Sources**: a project (recorded video) or a live session. At most 5 are
  active; more wait (live first, then oldest) up to `max_waiting` (20), then
  are refused with a reason.
* **Stages and pools**: `ingest` 5, `analysis` 2, `ai` 2, `render` 1 (max 2).
  Each stage has its own workers, so a slow AI provider never blocks
  ingestion. Tasks can need resources: local Whisper takes the single `gpu`
  slot. Studio maps Find clips to `ingest -> analysis(prepare)` and Export to
  `render`; both still run the unchanged `chopify.py` worker.
* **Leases**: a task is leased for 60 s and renewed while it runs. On
  restart, leases from the previous process are released, a worker child it
  left behind is stopped (only if its command line is Chopify's), and the
  task runs again; after 3 interrupted attempts it fails instead of looping.
* **Retries**: per task `max_failures` with exponential backoff; worker
  failures (same input, same error) are not retried.
* **Cancel**: queued tasks never start, the running child is stopped, clips
  already written stay. The slot frees immediately.
* **Protection**: analysis/render wait while free disk < 5 GB or free RAM
  < 1500 MB; processing pause applies to analysis/render only.
* Settings: `%APPDATA%\Chopify\scheduler.json` or `POST /api/scheduler`
  (`{"pools": {"render": 2}}`). Turn the scheduler off with
  `CHOPIFY_FEATURE_SCHEDULER=0` to get the legacy one-at-a-time lane.

## Upgrade Phase 3: project administration

`project_admin.py` owns titles and deletion. Project rows show
`display_title` (saved title, source metadata title, or a cleaned file
name). Deleting a project removes its folder except `clips/*.mp4` and
`.meta.json` (kept with `REMOVED_PROJECT.txt`) unless the user chooses to
delete the clips too and types their count. The folder must be a real
directory `<root>/<12-hex id>`; links inside are removed without entering
them. API: `POST /api/projects/<id>/rename|open-folder|delete-plan|delete`.
All confirmations use Studio's `#app-dialog`.

## Upgrade Phase 4: scene classification (observation only)

After clip selection, `projects.observe_scenes` runs `scenes.analyze` on the
source video (cached by file identity) and stores `scenes.json`; Studio tags
candidates with the scene at their midpoint. Signals: keyframes at 1 fps
(64x36 gray), motion/loop/static/HUD, audio RMS, RapidOCR on up to 40 frames,
gateway vision on up to 10 ambiguous frames when a privacy-eligible vision
route exists. The `scene_filtering` flag (off) is reserved for removing
non-gameplay sections once real labelled recordings meet >= 95 % gameplay
kept and >= 90 % breaks identified (`python scene_eval.py score`).

## Upgrade Phase 5: live ingestion

`live_ingest.LiveManager` (Studio `self.live`) runs one `Session` thread per
live source. Each session is a project (`status: recording`, `live` block in
project.json) and an open scheduler source. ffmpeg's segment muxer writes
`live/segments/seg%08d.ts` and a CSV list; the session indexes completed
segments on a session timeline into `live/index.db`, trims the ring to
`buffer_seconds` except pinned ranges, and runs the disk watchdog every
second. `Ring.extract(start, end)` joins the covering segments for clipping.
A new segment calls Studio's `_live_segment` hook (Phase 6). SRT and
`-follow` are available in the bundled FFmpeg 9.0.2 build.

## Upgrade Phase 6: continuous live clipping

Segment hook -> `SessionClipper` thread (every 30 s of new footage: RMS at
0.5 s steps, peaks z >= 4 over a 10-minute median/MAD baseline, 45 s
cooldown) -> candidate pinned [peak-75 s, peak+45 s] -> when the payoff is
buffered, scheduler `ai` task `live:judge` (transcribe pinned audio via the
gateway route; refine to sentences, 15-120 s; judge with the generate
route; keep at >= 6.5/10) -> `render` task `live:render` (join segments,
`render_clips.render`, 9:16) -> pin released. Quota/rate limits raise a
retryable error: the scheduler retries (30 s doubling, max 10 min), the
candidate shows "ai waiting". With no AI route configured only strong
spikes (z >= 6) are kept, marked not AI-judged.

## Upgrade Phase 7: consent-based publishing

Consents live in `%APPDATA%\Chopify\publish_consents.json` (one active per
project; replaced/revoked ones kept). A clip finishing (live render or
scheduled Export) calls `Studio.offer_clip` -> `AutoPublisher.offer`, which
checks consent, score, connected channel and the consent's daily limit,
renders the templates, cleans them (`publish_meta.clean`) and calls the
existing `UploadQueue.draft` + `approve(confirm=True)` with `consent_id`.
Uploads keep using the resumable videos.insert flow with quota ledger and
duplicate prevention (SHA-256). OAuth scopes and credentials unchanged.
Chopify states that age restrictions and made-for-kids reviews are
YouTube's decisions.

## Upgrade Phase 8: discovery

Verified against the YouTube API Services Developer Policies (2026-11-23):
III.E.2 (no aggregation across content owners), III.C.5 / III.E.4.h (no
modified, re-ranked or derived results), III.F.2 and the Branding
Guidelines (YouTube attribution, links back), III.E.4.c/d (store or refresh
at most 30 days), III.E.1 (no downloads). Discovery is therefore a search
and link page: nothing is monitored per channel, nothing is combined across
channels, nothing is downloaded. `GET /api/discover?q=&live=&order=&region=`,
`/api/discover/popular`, `/api/discover/watch`, `POST /api/discover/add|remove`.

## Upgrade Phase 9: resilience and test harnesses

* **Process lifetime** (`winjob.py`): every worker (`chopify.py`) and live
  ffmpeg Studio starts joins a Windows Job Object with kill-on-close, so if
  Studio is killed its children end with it. The scheduler's restart
  recovery (leases + leftover stop) remains as the second line.
* **Machine-wide AI slots** (`ai/slots.py`): at most
  `max_concurrent_requests` (default 2) requests per provider run on this
  computer at once, across Studio and all workers, using OS file locks that
  are released automatically if a process dies. On a slow uplink this keeps
  uploads from starving each other; waiting for a slot respects cancel and
  becomes a temporary (retryable) error after the timeout.
* **Live judging upload size**: only peak-45 s .. peak+20 s is transcribed
  for a candidate (the ring still pins 75 s / 45 s of context for the clip).
* **Partial live segments**: a segment ffmpeg was writing when it was
  stopped or killed is deleted at the next session start/stop.
* **Simulated AI** (`ai/providers/simulated.py`): registered only when
  `CHOPIFY_AI_SIMULATE=1`; deterministic, free, never leaves the computer.
  Used by the stress harness so load tests do not depend on the network.
* **Harnesses** (`tools/`): `harness.py` (isolated run folders, sleep
  prevention and detection, run-scoped process checks, proof that the real
  projects folder is unchanged), `recovery_test.py`, `stress.py`,
  `groq_check.py`.

## YouTube downloading (2026-11-25)

The compliance switch is gone: `compliance.py` now only recognises YouTube
addresses, reports capabilities and keeps monitoring of other channels
opt-in. YouTube URLs follow the pre-Phase-0 paths: `ingest.probe_source`,
audio/section/full downloads and caption fetches through yt-dlp (all built
by `ytdlp.base()`), `live.py` recording, and `live_ingest` live sessions for
streams that are live. A Find clips job downloads inside the scheduler's
analysis task, so it has the scheduler's progress, cancellation (the Job
Object ends yt-dlp with the worker) and retries for network errors.

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
