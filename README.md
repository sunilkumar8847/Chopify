<p align="center">
  <img src="assets/banner.svg" alt="Chopify" width="720">
</p>

# Chopify

**Chopify turns long gaming and entertainment videos and live streams into
ready-to-post YouTube Shorts.** It transcribes the footage, finds complete,
story-driven moments with AI, frames them for vertical video, burns in
animated captions, and can publish them to your YouTube channel - with your
explicit consent for every setting.

Everything runs from **Chopify Studio**, a local web app on your own
computer. AI work goes through a provider-independent gateway: **Groq**
(free plan) in the cloud, or **local models** (faster-whisper and Ollama)
when you prefer to keep everything offline.

---

## Contents

- [What it does](#what-it-does)
- [Key features](#key-features)
- [How it works](#how-it-works)
- [Tech stack and project layout](#tech-stack-and-project-layout)
- [Installation (Windows)](#installation-windows)
- [Configuration](#configuration)
- [YouTube setup (Data API v3)](#youtube-setup-data-api-v3)
- [Using Chopify](#using-chopify)
- [Command line](#command-line)
- [Testing](#testing)
- [Documentation](#documentation)
- [Credits and license](#credits-and-license)

---

## What it does

| You give it | Chopify gives you |
|---|---|
| A local recording, a YouTube link (video, Shorts or live) or a direct media URL | Transcript, scored story candidates to review, and rendered 9:16 Shorts with captions |
| Up to five live sources at once (OBS output folders, SRT feeds, direct streams, YouTube live) | A rolling buffer of each stream and clips cut automatically while the stream continues |
| A game or keyword | YouTube search results and the most-popular Gaming chart, with links and a watch list |
| Your consent and publishing choices | Clips uploaded to your channel with your visibility, category and template choices |

## Key features

### Clip finding
- **Story-based clipping** (default for new projects): the AI judge reads the
  whole transcript in order, with a running "story so far", and proposes
  complete stories - a hook, enough context, a build-up and the payoff.
  Candidates are scored for hook, context, middle, payoff, coherence and
  engagement; overlaps and duplicates are removed.
- **Adaptive selection**: instead of one fixed score threshold, Chopify keeps
  the best stories relative to the video (never below a quality floor), up to
  a sensible number of clips for its length.
- **Automatic score mode** is still available: scored windows from speech,
  audio energy and visual evidence, judged by the AI and filtered by a
  minimum score.
- **Dynamic auto-duration**: clip boundaries snap to whole sentences, add the
  setup or payoff a moment needs, trim dead air at the edges and respect the
  Shorts limit (15-120 seconds).
- **Gameplay scene detection** (observation mode): labels gameplay, starting
  soon, menus, loading, BRB and outro screens with motion, audio and OCR cues,
  and shows the label on each candidate. It does not discard footage yet -
  that waits for an accuracy evaluation on real labelled recordings
  (`scene_eval.py`).

### Live streams
- **Live sessions** from an **OBS output folder** (Chopify follows the
  growing recording), **SRT** (listener or caller), **direct HLS/RTMP/HTTP
  feeds** or a **YouTube live stream**.
- Six-second segments in a **bounded ring buffer** (30 minutes by default),
  with **pins** that keep possible clips until they are rendered.
- **Continuous live clipping**: loud moments are detected against each
  stream's own baseline, judged by the AI once the payoff has been recorded,
  refined to whole sentences and rendered as Shorts while ingestion keeps
  running.
- Reconnects with backoff, a disk watchdog, and automatic resume after a
  restart.

### Processing and reliability
- **Five-source scheduler**: up to five sources (live and recorded together)
  run at once; a sixth video waits in a queue, a sixth live session is
  refused with a reason. Separate worker pools for ingestion, analysis, AI and
  rendering, so a slow AI provider never blocks recording.
- **Persistent queue** (SQLite) with leases: interrupted work resumes once
  after a restart; worker processes end with Studio (Windows Job Object).
- **Safe cancellation**: cancelling stops the running step; clips already
  made are kept. Interrupted renders never leave a broken file that looks
  like a finished clip.
- Disk and memory floors pause heavy work instead of failing.

### AI
- **AI Gateway** for transcription, text/JSON and vision with interchangeable
  providers: **Groq** (Whisper large-v3 turbo and GPT-OSS models),
  **local** faster-whisper and **Ollama** (Qwen2.5 3B by default), plus
  OpenRouter, Gemini and NVIDIA adapters with strict privacy rules.
- **Free-only by default**: paid calls are off, budgets are $0, unknown
  prices count as paid, and there is no automatic switch to a paid provider.
- Rate limits are respected (Groq's own response headers); when a quota runs
  out, work pauses and is retried later instead of failing silently.
- Usage ledger in Studio: requests, audio minutes, tokens and cost per model.

### Captions and output
- Vertical 1080x1920 Shorts (also 16:9 and 1:1), face-aware framing,
  loudness normalization, silence tightening.
- Animated burned-in captions with 14 caption styles, a Caption Studio for
  fonts, colours, position and word motion, reusable brand presets and SRT
  export.
- Hindi and Hinglish speech is captioned in Roman script.

### YouTube
- **YouTube links** (watch, `youtu.be`, Shorts, live) are downloaded with
  yt-dlp and processed like any other source.
- **Gaming discovery** with the official YouTube Data API: search a game or
  keyword (optionally live only, by relevance, date or views) or open
  YouTube's most-popular Gaming chart for your region. Results open on
  YouTube; "Add to list" keeps the link only; duplicate results are removed
  and videos you already have a project for are marked.
- **Publishing** through the YouTube Data API v3 resumable upload: drafts
  with AI-written titles and descriptions, an approval step, a daily limit,
  quota tracking and duplicate-upload prevention. New uploads are private
  unless you choose otherwise.
- **Consent-based auto-publish** per project or live session: you choose the
  channel, visibility (no default; public needs an extra confirmation), the
  made-for-kids declaration, region (Gaming category checked for it), title
  and description templates, a daily limit and a minimum score. Every upload
  is verified afterwards; consent can be withdrawn at any time.

### Projects and storage
- **Readable project names** taken from the video title or file name, with
  **rename**.
- **Three-dot project menu**: rename, open the project folder, view its
  storage, delete.
- **Safe deletion**: delete a project while keeping its final clips, or
  delete the clips too after typing their number. Links (junctions and
  symlinks) are never followed, and projects with uploads in progress are
  protected.
- **Storage management**: after verified uploads, supporting files
  (source media, transcripts, caches) can be cleaned up; final clips are never
  deleted automatically.

### Studio
- A local, non-blocking web UI: background jobs with live progress, in-app
  dialogs and notifications, a dashboard for live sessions, discovery, AI
  settings, storage and the upload queue.

## How it works

```
source (file, YouTube link, OBS/SRT/live feed)
   │
   ├─ ingest ......... download or follow the source; live: 6 s segments in a ring buffer
   ├─ transcribe ..... YouTube captions when available, otherwise Whisper
   │                   (Groq Whisper large-v3 turbo or local faster-whisper)
   ├─ analyse ........ audio and visual evidence, scene labels (observation)
   ├─ find clips ..... Story mode: AI reads the transcript in parts -> complete stories
   │                   Automatic mode: scored windows + AI judge + minimum score
   ├─ refine ......... sentence-aligned boundaries, setup/payoff, 15-120 s
   ├─ render ......... 9:16 framing, captions, loudness -> MP4 + SRT + poster
   └─ publish ........ approval or consent -> resumable upload -> verification
```

The **scheduler** runs these steps for up to five sources at a time, with
separate pools for ingestion, analysis, AI and rendering. The **AI Gateway**
decides which provider handles each request and enforces privacy and
spending rules before anything is sent.

## Tech stack and project layout

- **Python 3.11+** (tested on 3.12), standard-library HTTP server for Studio,
  vanilla JavaScript front end
- **FFmpeg / ffprobe** for media, **yt-dlp** (with Node.js) for YouTube
- **faster-whisper**, **Ollama** (optional, local), **Groq** (optional, cloud)
- **OpenCV / NumPy** for visual analysis, **RapidOCR** for scene text
- **SQLite** for the scheduler, live segment indexes and the AI usage ledger

```
dashboard.py          Chopify Studio (local web app and HTTP API)
chopify.py            command-line interface
projects.py           project pipeline (transcribe -> find clips -> render)
scheduler.py          five-source persistent scheduler
story.py              story-based clip selection
score_clips.py        automatic scoring mode
highlights.py         AI judging of clip candidates
boundaries.py         sentence-aligned clip boundaries
render_clips.py       rendering, framing and captions
live_ingest.py        live sessions and ring buffers
live_clipper.py       continuous live clipping
scenes.py             gameplay scene classification (observation mode)
discovery.py          YouTube gaming discovery (Data API)
youtube_upload.py     upload queue
publish_consent.py    consent-based auto-publish
project_admin.py      titles, rename and safe deletion
storage.py            storage overview and cleanup
ai/                   AI Gateway, policy, usage ledger and providers
assets/studio/        Studio front end (HTML, CSS, JavaScript)
docs/                 detailed documentation
tests/                automated tests (unit, integration and browser)
tools/                test harnesses and helper scripts
```

## Installation (Windows)

### 1. Prerequisites

- **Python 3.11 or newer** (3.12 recommended) - <https://www.python.org/downloads/>
  (tick "Add python.exe to PATH")
- **Git** - <https://git-scm.com/download/win>
- **FFmpeg** on your PATH, for example:
  ```powershell
  winget install --id Gyan.FFmpeg
  ```
- **Node.js** (used by yt-dlp to read YouTube) - <https://nodejs.org/>
- Optional: an **NVIDIA GPU** with CUDA for faster local Whisper
- Optional: **Ollama** for local AI judging - <https://ollama.com/>

### 2. Get the code and create a virtual environment

```powershell
git clone <your-repository-url> chopify
cd chopify
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

For running the tests as well: `.venv\Scripts\python -m pip install -r requirements-dev.txt`.

## Configuration

### `.env` (API keys and spending limits)

Copy the template and fill in what you use:

```powershell
copy .env.example .env
notepad .env
```

```dotenv
GROQ_API_KEY=            # https://console.groq.com/keys - free plan is enough
OPENROUTER_API_KEY=      # optional
GEMINI_API_KEY=          # optional (free tier: synthetic test data only)
NVIDIA_API_KEY=          # optional (trial: synthetic test data only)
CHOPIFY_YOUTUBE_API_KEY= # optional, public Data API lookups without signing in

CHOPIFY_AI_FREE_ONLY=true
CHOPIFY_AI_ALLOW_PAID=false
CHOPIFY_AI_DAILY_BUDGET_USD=0
CHOPIFY_AI_MONTHLY_BUDGET_USD=0
CHOPIFY_AI_LOCAL_FALLBACK=false
CHOPIFY_GROQ_PLAN=free
```

- `.env` is ignored by Git - never commit it. Studio only ever shows the last
  four characters of a key.
- Restart Studio after editing `.env`.
- A Windows environment variable with the same name overrides `.env`.

### Groq (recommended cloud AI)

1. Create a free key at <https://console.groq.com/keys> and put it in
   `GROQ_API_KEY`.
2. Optional but recommended: turn on **Zero Data Retention** in Groq Console
   (Settings > Data Controls), then confirm it in Studio's **AI** panel - real
   recordings are only sent to Groq after that confirmation.
3. In Studio's **AI** panel, enable Groq and choose the routes, for example
   transcription `whisper-large-v3-turbo` and text `openai/gpt-oss-20b`.
   New projects use these routes.

Groq's free plan has daily limits (for `gpt-oss-20b`, 200,000 tokens per
day; a story pass over a 30-minute video uses about 40,000). When a limit is
reached, Chopify pauses that work and tells you; it never switches to a paid
service.

### Ollama (local AI, optional)

```powershell
ollama pull qwen2.5:3b
```

Chopify talks to Ollama at `http://127.0.0.1:11434` (override with
`CHOPIFY_OLLAMA_HOST`). Choose the local route in the **AI** panel, or keep it
as an explicit fallback for outages (`CHOPIFY_AI_LOCAL_FALLBACK=true`).
Local Whisper models are downloaded by faster-whisper on first use.

## YouTube setup (Data API v3)

Publishing and discovery need your own Google Cloud OAuth client (Chopify
does not ship one).

1. Open <https://console.cloud.google.com/> and create a project.
2. **APIs & Services → Library**: enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen**: choose *External*, fill in the
   app name and your email, and add yourself under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   application type **Desktop app**. Download the JSON file
   (`client_secret_....json`). Keep it private - it is ignored by Git.
5. In Studio, open a project's **YouTube** panel, enter the path of that file
   and press **Add client file**, then **Connect YouTube channel** and sign in
   with the account that owns the channel.

Or from the command line:

```powershell
.venv\Scripts\python youtube_auth.py client path\to\client_secret_XXXX.json
.venv\Scripts\python youtube_auth.py connect
.venv\Scripts\python youtube_auth.py status
```

Chopify requests the `youtube.upload` and `youtube.readonly` scopes. The OAuth
client and tokens are stored encrypted (Windows DPAPI) in
`%APPDATA%\Chopify`, never in the repository. While the consent screen is in
*Testing* mode, Google expires the sign-in after 7 days and Studio asks you to
connect again. See [docs/YOUTUBE.md](docs/YOUTUBE.md) for quotas and limits.

## Using Chopify

### Start Studio

```powershell
.venv\Scripts\python dashboard.py
```

Studio opens at <http://127.0.0.1:8765> (`--port` to change it,
`--no-browser` to skip opening a browser). Stop it with **Ctrl+C**.

### Clip a video

1. Paste one or more local file paths or links (YouTube watch, `youtu.be`,
   Shorts or a direct media URL) into the source box - one per line - and
   press **Find clips**. **Check source** tells you first whether a link is a
   normal video, a live stream or an upcoming stream.
2. Chopify transcribes the video and finds story candidates. Review them:
   watch each one, edit titles, times and captions, or pick a different
   caption style.
3. Press **Export** to render the selected clips as Shorts (MP4 + SRT +
   poster) into the project's `clips` folder.

Project settings > **How clips are found** switches between **Story-based**
(default for new projects) and **Automatic score**.

### Run live sessions

1. In OBS, record to **MKV** (or FLV/TS) into a folder of its own; use one
   folder per OBS instance.
2. Studio > **Dashboard > Live sessions**: choose **OBS output folder** and the
   folder (or **SRT** with `srt://0.0.0.0:9000?mode=listener`, or a **direct
   media URL / YouTube live link**), give it a name and press **Start live
   session**. Up to five sources run at once.
3. Candidates and their state (waiting for payoff, AI queued, rendering, done)
   appear in the panel; clips land in the session's `clips` folder.

### Discover gaming videos

Studio > **Dashboard > Discover gaming videos**: type a game (for example
"GTA 6"), optionally tick **Live now**, choose the order and your region, and
press **Search YouTube** - or **Popular gaming** for YouTube's chart. **Add to
list** saves the link to your watch list.

### Publish to YouTube

- **One clip at a time**: on an exported clip press **Publish…**, review the
  drafted title and description, then **Upload to YouTube…** and confirm.
- **Automatically**: in a project, press **Auto-publish…**, make every choice
  (channel, visibility, made for kids, region, templates, daily limit,
  minimum score), preview the templates and tick the consent box. Finished
  clips of that project or live session are then uploaded with those
  settings. **Turn off auto-publish** withdraws anything not yet uploading.

### Manage projects and storage

Use the **⋯** menu next to a project to rename it, open its folder, view its
storage or delete it. The **Storage** panel on the Dashboard shows disk use
and cleans up supporting files of projects whose clips are uploaded and
verified - final clips stay.

## Command line

```powershell
# clip a local file (or a YouTube link) into ./clips
.venv\Scripts\python chopify.py path\to\video.mp4 --aspect 9:16

# all options
.venv\Scripts\python chopify.py --help
```

See [docs/CLI.md](docs/CLI.md) for every flag.

## Testing

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

The suite includes unit, integration and real-browser tests (Edge or
Chrome, headless). `tools/` contains longer harnesses for crash recovery,
five-source stress testing and Studio start-up checks.

## Documentation

| Document | About |
|---|---|
| [docs/WORKFLOWS.md](docs/WORKFLOWS.md) | Day-to-day workflows in Studio |
| [docs/AI.md](docs/AI.md) | AI Gateway, providers, privacy and spending controls |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Architecture of the pipeline, scheduler and live system |
| [docs/YOUTUBE.md](docs/YOUTUBE.md) | YouTube connection, publishing, quotas |
| [docs/COMPLIANCE.md](docs/COMPLIANCE.md) | YouTube access and policy notes |
| [docs/STORAGE.md](docs/STORAGE.md) | Storage and cleanup rules |
| [docs/CLI.md](docs/CLI.md) | Command-line reference |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Common problems and fixes |

**Responsible use:** only clip and publish videos you own or are allowed to
use. Downloading from YouTube is subject to YouTube's Terms of Service; see
[docs/COMPLIANCE.md](docs/COMPLIANCE.md).

## Credits and license

Chopify was originally created by [mehbul](https://github.com/mehbul) as
[`mehbul/chopify`](https://github.com/mehbul/chopify). This version builds on
that project and extends it substantially - the AI Gateway, story-based
clipping, live ingestion and live clipping, the five-source scheduler,
discovery, consent-based publishing and project management.

Licensed under the terms in [LICENSE](LICENSE).
