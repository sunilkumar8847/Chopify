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

Try `--whisper-model small` or `tiny` and `--device cpu`. faster-whisper GPU
acceleration requires compatible NVIDIA/CUDA; AMD systems can use CPU. Studio
executes jobs serially to avoid loading multiple speech models at once.

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

Chopify finds these DLLs by itself (only inside its own processes). Then set
**Device** to `cuda` in the project settings. With 4 GB of VRAM the `medium`
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
