# Chopify setup and troubleshooting

## Preflight fails

Check `ffmpeg -version` and `ffprobe -version`. The ffmpeg build needs libx264,
AAC, and the `subtitles` filter (libass). Install a full build, reopen the terminal,
and use the same interpreter to install dependencies and run Chopify:

```powershell
winget install Gyan.FFmpeg
.venv\Scripts\python -m pip install -r requirements.txt
```

`--no-preflight` bypasses checks; it does not install missing dependencies.
For Studio, open the Job log for subprocess errors.

## Diagnostic logs

Studio and every processing job write structured logs (one JSON object per
line) to `logs/chopify-YYYY-MM-DD.jsonl` in the Chopify folder (or
`$CHOPIFY_LOG_DIR`). Each line has the time, level, project ID, job ID,
request ID, stage, operation, message and, for unexpected errors, the full
traceback. Files roll over at 10 MB and are kept for 14 days. Tokens, client
secrets and authorization codes are removed before anything is written.

Read them without opening files:

```
python applog.py --errors            # recent warnings and errors
python applog.py --project ID -n 200 # everything for one project
python applog.py --request ID --traceback
```

An error shown in Studio that ends with "(request abc123...)" can be looked
up with `--request`. Each project's `job.log` still holds the full output of
its jobs.

## A YouTube link fails to download

Find clips downloads YouTube links (videos, youtu.be, Shorts, live) with
yt-dlp. Private, members-only, age-restricted or region-blocked videos cannot
be downloaded without signing in. YouTube changes often: update yt-dlp with
`.venv\Scripts\python -m pip install -U yt-dlp`. A network failure during
the download is retried automatically (three tries). See
[COMPLIANCE.md](COMPLIANCE.md).

## Find clips does nothing

Fixed: a hidden live-recording field had an invalid default and silently
blocked the form. If clicking **Find clips** ever shows no "Submitting..."
message, reload the page (Ctrl+F5) so the browser loads the current Studio
files, and check `python applog.py --errors`.

## Studio says connection lost

Keep `python dashboard.py` running. Use its exact `http://127.0.0.1:PORT` address;
`localhost` and network IPs are intentionally rejected. If the port is busy,
use `--port 8766`. Reload after restarting because the session token changes.

## Local file is not found

Use an absolute path to a supported video on this computer. Relative CLI paths
resolve from the shell's current directory. Keep recordings in place. If a source
was replaced, prepare again before rendering so timestamps stay valid.

## Model or YouTube download fails

The first Whisper run downloads a model. Establish internet access and retry;
completed downloads/transcripts are reused. URL input also needs internet.
Supported sites change independently of Chopify. Update with
`python -m pip install --upgrade yt-dlp`. YouTube may require Deno:
`winget install DenoLand.Deno`. Source access depends on the site's rules;
Chopify does not bypass restrictions.

## Semantic search cannot connect

Start Ollama and run `ollama pull nomic-embed-text`. Check `ollama list`; a chat
model alone is not an embedding model. Requests use
`http://127.0.0.1:11434/api/embed`. Custom models must support that endpoint.
Changing model/text refreshes document vectors. There is no cloud fallback.
Use `--search` for keyword matching without Ollama.

## Transcription is slow or CUDA fails

First check the project's **TRANSCRIPTION** line in Studio: it shows the
device really used (GPU or CPU), the profile and whether YouTube captions
were used. A yellow warning means it fell back to the CPU, or that the
captions were refused (with the reason).

* Projects made before the speed profiles keep their saved Compute setting;
  older Studio versions saved `cpu`. Set **Compute** to Auto and **Speed
  profile** to Balanced in the project settings, then Find clips again.
* Balanced (the default) uses a URL's own YouTube captions after checking
  them and only transcribes exported clips: about a minute instead of most of
  an hour. See [PERFORMANCE.md](PERFORMANCE.md) for measurements.
* Smaller models and beam 1 are not offered as speed-ups: they lost Hindi
  speech in testing.

faster-whisper GPU acceleration requires compatible NVIDIA/CUDA; AMD systems
use the CPU. Studio executes jobs serially to avoid loading multiple speech
models at once.

Hindi is much slower than English on a CPU (Devanagari needs several times
more decoding work per word): a 2-hour Hindi video took about 10× longer than
real time with `medium`. On an NVIDIA GPU it ran at about 3× faster than real
time.

**Using an NVIDIA GPU.** faster-whisper needs CUDA 12 cuBLAS and cuDNN 9. If a
GPU job fails with `cublas64_12.dll is not found`, install them into the
project's virtual environment only (no system changes; about 1 GB):

```
.venv\Scripts\python -m pip install "nvidia-cublas-cu12>=12.4,<13" "nvidia-cudnn-cu12>=9.1,<10"
```

Chopify finds these DLLs by itself (only inside its own processes). New projects use
the GPU automatically (Compute: Auto). With 4 GB of VRAM the `medium`
model uses about 3 GB at float16 (Chopify's default on GPU). `int8_float16`
was tested and dropped words on Hindi speech, so it is not used.

## Captions have missing glyphs or wrong timing

Choose an installed font with the language's glyphs. Review word times in Studio
and save before exporting. Times must be finite, ordered, and within the source.
SRT uses clip-relative time; tightening remaps both subtitle forms. Recognition
and punctuation are imperfect, so review final clips.

## Split-screen crops the wrong person

Choose `9:16`, layout `split`, and tune centers from 0 to 1. First center: top;
second: bottom. Preview first. This is fixed framing, not speaker recognition.

## A render fails midway

Check disk space, ffmpeg, source availability, and logo paths. Retry with
`--project PATH --action render`. Matching completed clips are reused. Preview
and final exports have separate caches. Batch jobs continue after failures and
report a failure exit code. Avoid two writers using one project directory.
