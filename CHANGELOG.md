# Changelog

All notable changes to Chopify will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Story-based clip mode; Auto-publish category fix; caption fix
- `story.py`: clip mode "story" (default for new projects; "auto" scoring
  kept and still used by existing projects). The AI judge reads the whole
  transcript in ~5-minute parts with a running "story so far", proposes
  complete stories (hook, context, middle, payoff, coherence, engagement
  0-10); Chopify validates them against sentence timestamps, keeps clips
  15-120 s without cutting the payoff, penalises dead air, removes
  overlapping/duplicate stories and selects adaptively (floor 5.0, within
  2.5 of the best, length-based cap). Judgements are cached per part.
  Studio: "How clips are found" in project settings.
- Gateway/Groq: optional `reasoning` and `max_output_tokens` hints; story
  mode asks gpt-oss for low reasoning (default reasoning left long JSON
  answers empty - HTTP 400 "Failed to validate JSON").
- Fix: Auto-publish consent failed with HTTP 400 incompatibleParameters
  (videoCategories.list was sent `id` and `regionCode`; it accepts one).
  The region's categories are listed and category 20 is checked there.
- Fix: styled YouTube caption tracks doubled every word (each event comes
  once per pen); duplicates are skipped. `SUBTITLE_VERSION` 2 rebuilds
  caption transcripts on the next Find clips.

### YouTube downloading enabled (owner's decision, 2026-11-25)
- The Phase 0 YouTube download switch is removed (`CHOPIFY_YOUTUBE_DOWNLOADS`,
  `features.json` `youtube_downloads`, `compliance.require/blocked`). Find
  clips downloads YouTube watch, youtu.be, Shorts and live links with yt-dlp
  into the project folder and runs the normal pipeline through the
  scheduler; Check source reads links with yt-dlp (official API as
  fallback); the Record button works again.
- YouTube live streams can be live sessions (`live_ingest`: yt-dlp resolves
  the HLS address at every connect; links that are not live are refused).
- `ytdlp.py`: every yt-dlp call adds `--js-runtimes node` when Node.js is
  installed; `yt-dlp-ejs` (challenge solver) added; yt-dlp 2026.11.23.
- Network failures while downloading retry up to three times.
- Monitoring other channels stays opt-in (`CHOPIFY_YOUTUBE_MONITORING` /
  `features.json` `youtube_monitoring`); own-channel uploads now become projects.
- Fix: projects created by Studio's Find clips now follow the AI settings'
  routes (Groq) - form validation used to fill in local defaults first.
- docs/COMPLIANCE.md records the policy trade-off (YouTube API Developer
  Policies III.E.1) accepted by the owner.
- Fix: Find clips requests without an aspect ratio now get the new-project
  defaults (9:16 Shorts) instead of the CLI's 16:9.
- Real tests (openly licensed / public-domain material): NASA ScienceCasts
  watch link -> download, Groq transcription and judging, two 1080x1920
  Shorts; youtu.be and /shorts/ links -> download and transcription; NASA ISS
  live stream -> live session (175 s buffered at 720p). Groq Free, $0.

### Phase 9: integration, resilience and performance testing
- `winjob.py`: Studio's workers and live ffmpeg processes join a Windows Job
  Object (kill-on-close); killing Studio no longer leaves workers running.
- `ai/slots.py`: machine-wide limit of 2 concurrent requests per AI provider
  across Studio and all workers (OS file locks, released if a process dies);
  `max_concurrent_requests` setting (1-4).
- Renders encode to `_partial-<name>` and are renamed only after
  verification, so an interrupted render never looks like a finished clip;
  Studio lists ignore `_` files.
- Live judging transcribes peak-45 s..peak+20 s (smaller uploads); partial
  live segments left by a stop/crash are deleted.
- `ai/providers/simulated.py` (only with `CHOPIFY_AI_SIMULATE=1`) for load
  tests; harnesses `tools/stress.py`, `tools/recovery_test.py`,
  `tools/groq_check.py`, `tools/startup_check.py`, `tools/harness.py`.
- docs/WORKFLOWS.md: user guide for startup, project menu, five sources,
  OBS/SRT live sessions, live clipping, auto-publish and discovery.

### Phase 8: YouTube gaming discovery (official Data API)
- `discovery.py`: game/keyword search (`search.list`, Gaming category,
  optional live-only, user-chosen order), YouTube's most-popular Gaming
  chart per region, link-only watchlist ("Add" never downloads or creates a
  project; duplicates refused). Results shown as returned with "Results from
  YouTube" attribution, channel links and the YouTube Terms link; search
  cache 1 h; watchlist refreshed via `videos.list` after 25 days and deleted
  by 29 days; deleted videos removed; own search.list daily ledger (100);
  everything deleted on YouTube disconnect.
- Studio: Dashboard "Discover gaming videos" panel; CSP allows thumbnails
  from i.ytimg.com only.
- Fixes: concurrent discovery writes (shared lock, retrying replace);
  "View storage" now reads its own `/api/projects/<id>/storage` endpoint
  (the plan endpoint omitted totals).

### Phase 7: consent-based YouTube publishing
- `publish_consent.py`: per-project (or live session) consent with an
  explicitly chosen destination channel, visibility (no default; public
  needs typing PUBLIC), made-for-kids declaration (never assumed), Gaming
  category 20 checked as assignable for the user's region via
  `videoCategories.list`, approved title/description templates, daily limit
  and minimum score. `AutoPublisher` queues finished clips (live clips and
  exported VOD clips) through the existing approval-gated upload queue;
  refuses when the connected channel differs; revocation withdraws queued
  uploads and keeps history. Finished uploads are verified with
  `videos.list` after each upload run.
- Studio: "Auto-publish…" consent dialog in the project view, turn-off
  dialog, `/api/projects/<id>/consent|consent-preview|consent-revoke`.

### Phase 6: continuous live clipping
- `live_clipper.py`: per-session detection of loud moments (robust
  median/MAD baseline, cooldown, hourly cap), ring pins for setup/payoff,
  queued AI evaluation (gateway transcription + `highlights` judge) as
  scheduler `ai` tasks that retry with backoff on quota/rate limits while
  ingestion continues, boundary refinement (`boundaries.refine_segments`,
  120 s hard maximum) and rendering with the unchanged `render_clips.render`
  into the session's `clips` folder. Candidates persist in
  `live/candidates.json`. Flag `live_clipping` (on).
- Studio: live candidates and their state in the Live panel.

### Phase 5: live ingestion and ring buffers
- `live_ingest.py`: sessions from an OBS output folder (follows the growing
  MKV/FLV/TS, switches to new recordings), SRT (listener or caller) or an
  authorized direct HLS/RTMP/HTTP feed (YouTube refused, no yt-dlp); ~6 s
  MPEG-TS segments (stream copy); SQLite segment index; ring buffer (30 min
  default) with pins for potential clips; disk watchdog (shrink below
  10 GB free, stop below 5 GB, pins kept); reconnect with backoff; graceful
  ffmpeg stop; sessions resume after a Studio restart; 5 sessions max,
  sharing the scheduler's source slots (a sixth is refused, never queued).
- Studio: Dashboard "Live sessions" panel and `/api/live`, `/api/live/start`,
  `/api/live/<id>/stop`.

### Phase 4: gameplay-only scene classification (observation)
- `scenes.py`: keyframe motion, static/loop detection, HUD stability, audio
  RMS, RapidOCR keywords on a few frames, optional gateway vision (max 10
  frames, privacy rules apply). Labels gameplay / starting_soon / intro /
  menu / loading / brb / outro / static / unknown; `scenes.json` per project
  and a scene tag on each candidate. Nothing is cut (`scene_filtering` off).
- `scene_eval.py`: labelling page, time-weighted scoring against the targets,
  synthetic stream generator.
- Dependency: `rapidocr-onnxruntime` (optional; onnxruntime was present).

### Phase 3: project administration and dialogs
- Human-readable project titles (`project_admin.display_title`), rename,
  three-dot project menu (rename, open folder, view storage, delete).
- Safe project deletion: "delete project, keep final clips" (default) or
  "delete clips too" with the clip count typed; refuses junction/symlink
  project folders, removes links inside a project as links only, never
  unlinks a file resolving outside the project, refuses busy projects and
  pending uploads, keeps upload history.
- Studio dialog (`askDialog`) replaces every native alert/confirm/prompt.
- Sidebar shows scheduler source slots.

### Phase 2: five-source persistent scheduler
- `scheduler.py`: SQLite queue with 5 source slots, waiting queue (live
  first), separate ingest/analysis/AI/render pools (render 1, max 2), GPU
  resource slot, leases with crash recovery and leftover-worker stop, retries
  with backoff, independent cancellation that keeps finished clips, disk and
  memory floors. `features.py` flags; Studio uses it by default.
- Studio: Find clips / Export run through the scheduler; `/api/scheduler`
  settings; status includes source slots.

### Phase 1 validation (live, Groq Free)
- Groq health, `whisper-large-v3-turbo` / `whisper-large-v3` transcription and
  `gpt-oss-20b` strict JSON verified live on synthetic speech at $0; ledger
  accounting verified. New projects default to the Groq routes. See docs/AI.md.

### Configuration: `.env` for provider keys
- New `envconfig.py`: the single `.env` loader (python-dotenv parser). Reads
  `.env` from the Chopify folder regardless of the working directory; OS
  environment variables win; values stay out of `os.environ`; malformed or
  missing files never stop Chopify and are reported by line number only.
- Provider keys (`GROQ_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`,
  `NVIDIA_API_KEY`) and `CHOPIFY_YOUTUBE_API_KEY` come only from `.env` / the
  environment. Studio's Save/Remove key actions are gone (the API answers 410);
  the AI panel shows the `.env` path, configured status and last 4 characters.
  The old `ai_keys.bin` is no longer read and is reported, never deleted.
- `.env` spending settings are defaults and ceilings (`CHOPIFY_AI_*`); Groq
  plan from `CHOPIFY_GROQ_PLAN`, default free.
- Groq free plan: live `x-ratelimit-*` headers respected (pause before the
  limit), 429 without Retry-After waits for the reset, "limited" requests
  counted and shown separately; every audio piece size-checked before upload.
- NVIDIA trial: cannot be a project route; each model must be approved first.
- Logs also redact the exact values of secret variables. `.gitignore` covers
  `.env`, `.env.*` (not `.env.example`) and `ai_keys.bin`.

### AI Gateway (Phase 1)
- `ai/` package: one gateway for transcription, text/JSON and vision with
  interchangeable providers (local faster-whisper + Ollama, Groq, OpenRouter,
  Gemini, NVIDIA trial), normalized results, retries (backoff, Retry-After),
  quota cooldowns, request IDs and a usage ledger (SQLite, no content).
- Policy: free-only by default, paid calls off, $0 budgets, unknown prices
  treated as paid, no automatic paid fallback, optional local fallback. Real
  recordings only to providers whose terms allow it: Groq after confirming
  Zero Data Retention, OpenRouter with zdr + data_collection: deny and no
  fallbacks, Gemini free and NVIDIA trial synthetic-only.
- Keys stored with DPAPI or environment variables, shown only as the last 4
  characters; provider key formats redacted from logs.
- Studio dashboard: AI panel (providers, keys, privacy, health, routes,
  budgets) and AI usage table.
- Existing judging, metadata, Hinglish polishing and transcription use the
  gateway through `llm_provider: gateway` and `transcriber: provider:model`;
  saved projects keep their local settings.
- `python -m ai.benchmark` (transcription, judging, synthetic). See docs/AI.md.

### Compliance (Phase 0.1)
- Feature-level YouTube capabilities instead of one blanket block. Check
  source works again through the official Data API (`videos.list`: title,
  channel, duration, live state, views; clear "not found" and quota
  messages; never a yt-dlp fallback). Own-channel monitoring uses
  `channels.list` + `playlistItems.list` + `videos.list` and reports new
  uploads with a link (metadata only). Other channels are kept but marked
  inactive (Developer Policies III.E.2). Audiovisual downloads, URL live
  recording and unofficial captions stay off; `captions.download` deferred.
  API keys (`CHOPIFY_YOUTUBE_API_KEY`, optional) are redacted from logs.

### Compliance (Phase 0)
- YouTube downloads are turned off by default (YouTube API Developer
  Policies III.E.1: no copies of YouTube audiovisual content without written
  approval). Blocked for new work: URL audio/video, section and full
  downloads, yt-dlp metadata probes, caption fetching, live recording and
  channel monitoring. Studio, the CLI and batch mode explain why and create
  nothing. Local files, other sites, existing projects and the official Data
  API are unchanged. Reversible: `features.json` `{"youtube_downloads": true}`
  or `CHOPIFY_YOUTUBE_DOWNLOADS=1` (docs/COMPLIANCE.md).

### Shorts output and Studio corrections
- New projects default to Shorts: 9:16 (1080x1920), clips 15-120 s. For 9:16
  the 120 s limit is hard: settings above it are saved as 120, duration
  refinement rejects a clip that cannot be self-contained within the limit
  instead of cutting it, selection drops anything longer, and preview/export
  refuse an over-long edited clip. Landscape 16:9 stays available with its own
  maximum. The selection summary shows the real length range and the limit.
- Exports have square pixels (setsar=1); facecam-on-top 9:16 clips were
  1080x1920 with SAR 1955:1962 and displayed as 1080x1927. Render cache key
  versioned so a re-export writes a new, correct file.
- One shared player: Watch previews and rendered clips load into the main
  player with a PREVIEW / FINAL EXPORT label; clip cards no longer embed
  their own players (Play, download, publish and delete stay on the card).
- Titles come from each clip's own final range: YouTube caption artifacts
  ("[ __ ]", ">>"), stutters, repeated phrases and dangling endings removed.
- Status shows what is really running ("Export: Whisper word timing for 6
  clips", "Export: rendering clip 2 of 6"); a saved running status with no
  live job is shown as stale. Project page counts selected, queued,
  rendering, exported and failed clips separately.
- Section downloads get time in proportion to their length (they ran at
  ~1.3 s per second and every section over ~2 minutes "stalled"); the
  whole-video fallback no longer starts without room to download and merge it.

### Storage
- Storage management (docs/STORAGE.md): final clips are kept forever and
  never deleted automatically. Supporting files (downloads, previews,
  transcripts, caption/AI caches, intermediate files) are cleaned up only
  after every final clip of a project is uploaded, YouTube reports each
  video processed (videos.list), the clips pass media checks and nothing is
  running. Journaled, resumable and idempotent; failed, processing or
  unverifiable uploads keep everything. Automatic for new projects only.
- Manual deletion of final clips with a confirmation listing every file,
  its size and upload state.
- Dashboard Storage panel: usage by category and project, reclaimable space,
  clean up one or all eligible projects, delete clips.

### Performance
- Subtitle-first transcription: a URL's own YouTube captions (manual first,
  then YouTube's speech track, spoken language only, never translations)
  drive clip discovery after quality checks and a Whisper spot check of
  three 30-second windows (text, language and timeline). Only exported clips
  are transcribed with Whisper. A 62-minute video: speech-to-text 68 min ->
  40 s, Find clips 70 min -> 3 min. Refused captions fall back to Whisper
  with the reason shown. See docs/PERFORMANCE.md.
- Speed profiles Accurate / Balanced (default) / Fast. All keep Whisper
  medium, beam 5: small, beam 1 and batched decoding lost Hindi speech.
- New projects use Compute: Auto (GPU when it works); a CPU fallback is shown
  as a warning, and an explicit GPU request that cannot work fails clearly.
  Studio shows the planned mode before starting and the real device after.
- Whisper chunks are reused only with matching model/decoding/precision;
  a checkpoint is saved every 2 audio minutes inside a chunk; progress shows
  minutes transcribed and time left. The Whisper model is released before
  the AI judge needs the GPU.

### Fixed
- Studio's Find clips form never sent a device, so every new project was
  transcribed on the CPU.
- The project page could stay on "queued" when a job finished very quickly.

### Added
- Phase 6 automation dashboard (`#dashboard`): per-project stage pipeline with
  progress, job and upload queues, pause/resume of processing and uploads,
  retry and cancel, failure diagnosis with recovery advice, activity log.
- Duplicate-source prevention for batch and monitoring (YouTube video ID or
  resolved file path).
- Optional YouTube channel monitoring: new videos become projects, live
  streams are recorded; daily limit; never publishes.
- Restart recovery: interrupted processing jobs are queued again (setting).
- Output-language policy: captions, SRT, hooks, file names and YouTube
  metadata are Roman-script Hinglish/English, never Devanagari
  (`hinglish.py`, `transcript.display.json`).
- GPU transcription finds pip-installed CUDA DLLs in the virtual environment.
- AI judgments are saved one by one (atomic, locked), so a stopped run
  continues where it stopped.

### Fixed
- Studio "Find clips" did nothing: the hidden live-recording "Max hours" field
  (min 0.1, step 0.5, default 12) was invalid, so the browser blocked the form
  without a message or request. The step is now 0.1 and the form no longer
  depends on hidden recording options.
- Check source and downloads now show yt-dlp's real reason (e.g. "This video
  is unavailable") instead of a generic message.

### Added (diagnostics)
- Structured JSON-lines logs in `logs/` (request, project, job, stage ids,
  tracebacks; rotated; secrets redacted) and `python applog.py` to read them.
- Request IDs on every Studio response; unexpected errors answer HTTP 500
  with the request ID instead of a dropped connection.
- Find clips: duplicate-click guard, "Submitting..." state, time limits,
  connection-lost banner, project status panel (now / last done / progress,
  error and what to do). Projects whose job cannot be queued are marked
  failed, never left waiting.

### Changed
- Hindi/Hinglish YouTube metadata: English, Devanagari, repetitive or copied
  AI titles/descriptions are retried with a Hinglish instruction and otherwise
  replaced by the speaker's own lines (noted in the draft); English speech
  stays English. Outdated unpublished AI drafts are re-drafted when opened.
- More natural Hinglish transliteration (mast, saaf, liye, gaye, rehna);
  caption render caches are refreshed (policy version 2).
- `job.log` is appended per run (rotated at 2 MB) instead of overwritten.
- Studio ignores browser disconnects (WinError 10053) instead of printing
  tracebacks.

- `python chopify.py --version` prints the release version and exits 0 without
  requiring a source, ffmpeg or network.
- `python chopify.py --list-styles` prints the 13 caption preset names and
  exits 0 before any source check or preflight.
- `docs/CLI.md` documents every CLI flag, grouped by input, scoring/discovery,
  captions, output, batch/project and diagnostics; linked from the README and
  embedded in `llms-full.txt`.

## [1.4.0] - 2026-11-05

### Added
- Local browser dashboard with project review, progress, logs, previews, caption
  editing, candidate selection and downloadable exports.
- Caption Studio with 13 presets, full typography/layout controls, word motion,
  highlighting, boxed backgrounds, and a source-synced overlay with 5-second
  preview.
- Automatic per-cue emphasis for numbers, money, ALL-CAPS and power words, plus
  optional deterministic emoji, capped at two emphasis words and one emoji per cue.
- Reusable caption presets through brand JSON; project caption overrides persist
  in `project.json` and layer over preset and brand values.
- Shared caption CLI flags and `--caption-json` for `chopify.py` and
  `render_clips.py`.
- Optional `hidden` and `emphasis` flags on transcript words; both burnt captions
  and SRT honor them.
- Direct local video input (MP4, MOV, MKV, WebM, M4V, AVI).
- Resumable projects with stage invalidation, per-clip checkpoints, and distinct
  output names for render variants; partial failures retain successful clips.
- Automatic long-source path: sources of 2 hours or more, 8 GB estimated download,
  or unknown duration use an audio-first probe and resumable ingest. Short
  sources keep the original single-pass workflow.
- Long URLs download audio only, or lowest-resolution video when there is no
  audio; an audio track with no speech also switches to the low-resolution video
  so silent gameplay and streams can score from visual evidence. Local long files
  are read in place.
- Long-source Studio review: project detail exposes source metadata, progress,
  per-clip render errors, and whether the full source is playable. Watch on a
  long URL candidate queues a 480p preview from its downloaded section and plays
  the result.
- Resumable 30-minute transcription chunks with 30-second overlap merging and
  global word times (`transcript.partNNN.json`).
- Resumable 30-minute discovery blocks with `max_len` boundary context
  (`candidates.partNNN.json`).
- Selected-range section rendering for long URLs: 10-second padding, 30-second
  merge gap, 10-minute cap, `source_offset` global-to-local time mapping, and
  automatic cleanup after a successful final render.
- Per-clip section and render errors (`render_errors`) keep successful outputs
  and cached sections for retry.
- Exhaustive sentence-seeded discovery with an AI-chosen duration ladder,
  `candidates.json` / `selection.json` diagnostics, and paginated Studio review.
- Fully local audio-evidence timeline with cached 1 Hz features, content
  profiles, adaptive evidence weights, and a 7.0 text floor for speech-sparse
  windows.
- Action-only and speech-sparse clip discovery using audio event seeds, grid
  seeds for varying tracks without peaks, and a clear failure for flat sources.
- Fully local visual evidence from cached 2 fps analysis: motion spikes,
  scene/UI changes, facecam presence, action arcs, and boundary quiet, plus a
  sparse whole-source baseline.
- Silent and no-commentary gameplay, PC-game, and stream support: flat audio is
  treated as unavailable so visual action can score on its own.
- Visual shortlisting before frame decoding, visual-only seeds for silent
  footage, a 6.0 cap for flat action windows, and evidence-checked boundary
  refinement.
- Visual diagnostics in `candidates.json` and `selection.json`:
  `visual_baseline`, `visual_ranges`, `visual_sections`, and warnings.
- `evidence` fields in `candidates.json` and `segments.json`, plus
  `evidence_available` and profile metadata in `selection.json`.
- Guided CLI prompts for minimum score and preferred length, plus `--no-input`
  and `--explain` for scripts and review.
- Serial batch queue with isolated folders and aggregate failure reporting.
- Word/timestamp editing and clip-relative SRT, including retimed tightened cuts.
- Validated reusable JSON brand presets for fonts, colors, size, margins and logo.
  Brand files now accept every Caption Studio field.
- Semantic clip search with optional local Ollama embeddings and document caching.
- Vertical podcast split-screen with two adjustable fixed crop centers.
- Workflow, troubleshooting, local API, and machine-readable product documentation.
- Static documentation website with canonical/social metadata, JSON-LD, sitemap
  and a GitHub Pages deployment workflow.
- One-command installers for Windows and macOS/Linux, a contributor guide,
  issue templates, and a 1280x640 social preview for the documentation site.

### Changed
- Default minimum clip score is now 8.0 (was 6.5 heuristic / 8.0 LLM).
- Long sources select the audio-first, chunked, block-scanned, section-rendered
  path automatically; there is no duration ceiling or mode toggle.
- Render cache keys include the section offset and section identity, so long
  project retries reuse finished clips without re-downloading their sections.
- Every clip at or above the threshold is returned by default; the ten-clip cap
  is now opt-in through `--max-clips`.
- Clip length is selected from a duration ladder; `--preferred-len` is a soft
  preference rather than a fixed target.
- Default clip range is 15-300 seconds (was 20-75).
- Audio evidence adjusts the calibrated text score through the source profile
  and evidence weight; content profiles never change the user-facing threshold.
- Combined audio and visual evidence uses 60/40 weighting; one modality is
  shrunk toward neutral, and text remains the final fallback.
- Discovery cache keys include the visual feature version, so an analysis
  upgrade invalidates only affected work.
- Discovery and selection are cached separately, so changing the score
  re-filters without rescanning the transcript.
- Matching successful stages resume by default; `--no-resume` forces processing.
- Caption style resolution is named preset, then brand preset, then per-project
  override; aspect defaults fill unset font size and margin.
- SRT cue grouping follows the caption style's words-per-cue value on project
  renders.
- Docs clarify setup downloads, fixed framing, language limits and score meaning.

### Fixed
- Overlapping candidate windows are no longer blocked by marking covered
  sentences as taken.
- Zero passing clips now writes an empty segment list instead of a fallback clip.
- LLM reranking now adjusts the evidence-aware final score instead of the raw
  text score, and no longer compounds an earlier adjustment on rerun.
- Split-screen draft panels use even YUV420 dimensions to avoid crashes in older
  ffmpeg/libass builds; padding preserves the intended preview resolution.
- Changed URL downloads cannot reuse an older source file.
- LLM prompt definitions are initialized before standalone scorer execution.
- Render failures return nonzero instead of reporting success.
- Relative output paths resolve before ffmpeg changes its working directory.
- Identical clip titles cannot overwrite each other's project exports.
- A failed long-source section records a per-clip error instead of discarding
  successful renders; remaining sections still render and retry later.
- Long-source preview playback no longer draws the source caption overlay over
  the already-rendered draft.
- A stalled long-source section download falls back to a full-source download
  and continues rendering with `source_offset=0`.

## [1.3.0] - 2026-09-06

### Added
- **Complete-thought clips**: every clip is grown or trimmed to end on a sentence
  with terminal punctuation, so clips never start or end mid-thought.
- **Keyword search** (`--search "pricing"`): bypass virality scoring and build
  clips around transcript sentences containing all your keywords.
- **Review workflow** (`--clips 1,3,5`): scoring prints a numbered candidate
  table; render only the rows you pick.
- **Preview drafts** (`--preview`): render fast 480p cuts (no poster or
  metadata) to check your picks before the full-quality render.
- **Dependency pre-flight check**: before any stage, verifies ffmpeg/ffprobe are
  on PATH, the ffmpeg build can encode H.264 (libx264) and AAC, and required
  Python packages are installed — each problem reported with a fix hint. Bypass
  with `--no-preflight`.

### Changed
- Candidate selection now shares a single window-growth routine
  (`_grow_window`) between virality scoring and keyword search, both enforcing
  the complete-thought rule.

[Unreleased]: https://github.com/mehbul/chopify/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/mehbul/chopify/releases/tag/v1.4.0
[1.3.0]: https://github.com/mehbul/chopify/releases/tag/v1.3.0
