# Automation dashboard

Open Studio (`python dashboard.py`) and press **▤ Dashboard** in the left
rail, or go straight to <http://127.0.0.1:8765/#dashboard>. The badge on the
button counts projects that need attention.

## What it shows

* **Cards:** running and queued jobs, projects that failed, uploads (done,
  approved, drafts), uploads left today and new sources found by monitoring.
* **Pipeline:** one row per project with a dot per stage — Source,
  Transcribe, Discover, Select, Render, Upload — filled when done, pulsing
  while running (with a progress bar for transcription chunks and clip
  renders) and red where it failed.
* **Failed rows** say which stage failed, why, and what to do, for example
  "The GPU libraries for Whisper could not be loaded — install the CUDA
  libraries in the virtual environment or set Device to cpu, then Retry."
* **Upload queue, jobs, channel monitoring, automation settings** and the
  **activity log** (what Studio did and when; kept in
  `<projects>/.studio/events.jsonl`).

## Controls

| Control | What it does | Safe because |
|---|---|---|
| **Pause processing** | The running job finishes; queued jobs wait (shown as "paused"). | Nothing is interrupted mid-write. |
| **Pause uploads** | No new upload starts; one already uploading finishes. | Uploads are resumable anyway. |
| **Retry** (failed / interrupted / cancelled rows) | Runs the project's last action again. | Finished stages and clips are reused. |
| **Cancel** (busy rows) | Stops the job; recordings stop and are kept. | Partial work stays on disk. |

Pause states survive a Studio restart.

## Batch processing and duplicates

Paste several sources (one per line) under **New project** and press **Find
clips**. Each becomes its own project and they run one at a time. A source
that already has a project — the same YouTube video in any URL form
(`youtu.be/ID`, `watch?v=ID`, `/live/ID`, `/shorts/ID`) or the same local file
— is skipped and Studio tells you which project already has it. The API
accepts `"allow_duplicates": true` when you really want a second copy.

Uploads are de-duplicated separately: the same clip file is never uploaded
twice (Phase 5).

## Channel monitoring (optional)

1. Add a channel (`https://www.youtube.com/@name` or `/channel/UC…`).
2. Tick **Monitor channels** and save. Studio checks every N minutes
   (default 30; **Check channels now** runs one pass immediately).

The first check only records the videos that already exist, so adding a
channel never processes its back catalogue. After that:

* a **new video** becomes a project and is processed with the chosen action
  (Find clips for review, Find clips and export, or only create the project);
* a **live stream** is recorded (if *Record live streams* is on) and
  processed when the recording ends;
* an **upcoming stream** is checked again until it goes live.

At most *New sources per day* items are taken per day; the rest wait for the
next day. **Monitoring never publishes anything:** clips still need your
approval, one by one, and upload as private by default.

Monitoring uses `yt-dlp` to list a channel's newest uploads; only process
channels you own or have permission to use.

## After a restart

Jobs that were running or queued when Studio stopped are marked
*interrupted*. With **Resume interrupted jobs when Studio starts** (default
on) they are queued again automatically; otherwise press Retry. Recordings
are not restarted automatically — use *Resume recording* or *Finalize
recording* in the project. Approved uploads resume by themselves (Phase 5).

## Logs

* `<project>/job.log` — the full output of every job, appended per run with a
  `===== date action =====` header (rotated to `job.log.1` at 2 MB).
* `<projects>/.studio/events.jsonl` — the activity log (rotated at 1 MB).
