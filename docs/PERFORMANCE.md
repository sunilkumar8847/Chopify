# Transcription performance

Measured on the development machine: NVIDIA RTX 2050 (4 GB), Intel
i5-12450H, 16 GB RAM, Windows 11, faster-whisper 1.2.1 / CTranslate2 4.9.0.

## Why it was slow

* New Studio projects were transcribed on the **CPU**: the Find clips form
  never sent a device, and the default was `cpu` (int8). A 62-minute video
  took about 68 minutes of speech-to-text (1.09 x its length).
* Even on the GPU, Whisper medium (beam 5, VAD, word timestamps) needs
  0.53-0.78 x the video length: a 2-hour video is 65-95 minutes.

## Whisper settings (10-minute Hindi sample)

Sample: minutes 79-89 of a 2-hour Hindi gaming video. Reference: YouTube's
own automatic Hindi captions for the same audio (1,747 words), checked by
reading the passages where the systems disagree. "Lost windows" counts
10-second windows where the reference has speech but the transcript has
less than a quarter of it.

| Config (GPU float16) | Time | x length | Peak VRAM | Peak RAM | Words | Lost windows | Loops |
|---|---|---|---|---|---|---|---|
| A medium, beam 5, VAD (current), idle | 468 s | 0.78 | 3.4 GB | 1.6 GB | 1164 | 11 | no |
| A same, while a CPU job ran | 585 s | 0.98 | 3.5 GB | - | 1391 | 4 | no |
| B small, beam 1 | 451 s* | 0.75* | 1.4 GB | - | 1158 | 12 | no |
| C medium, beam 1 | 348 s* | 0.58* | 3.3 GB | 1.6 GB | 650 | 36 | yes |
| C2 medium, beam 1, no previous-text conditioning | 537 s | 0.90 | 3.0 GB | 1.6 GB | 1333 | 11 | yes |
| D medium, beam 5, no VAD | 321 s | 0.53 | 3.5 GB | 1.6 GB | 1209 | 9 | no |
| E2 medium, beam 5, batched x2 | 79 s | 0.13 | 2.9 GB | 1.6 GB | 952 | 18 | yes |
| E4 medium, beam 5, batched x4 | 58 s | 0.10 | 3.7 GB | 1.6 GB | 940 | 18 | yes |

\* measured while another transcription used the CPU.

Findings:

* Every faster setting loses speech. Batched decoding drops the start of
  every 30-second batch (lost windows every 30 s); beam 1 falls into
  repetition loops ("अपने परस्थानलिटी नहीं जाने बाला यार" repeated for
  two minutes). Small mis-spells and drops whole sentences.
* Whisper Hindi output is not repeatable: two runs of the same config
  differ by ~50% of words and lose different passages.
* Word timing is equally good everywhere (median 0.06 s from YouTube's).
* int8_float16 is not used (it dropped Hindi words in an earlier test).

So Whisper always runs **medium, beam 5, VAD, float16 on the GPU**.

## YouTube captions

Both test videos have automatic captions in the spoken language (`hi-orig`,
`en-orig`) with per-word timing. Against Whisper in 30-second windows:
median agreement 0.81 (Hindi) and 0.90 (English); a deliberately wrong
timeline scores 0.22. The Hindi captions contained sentences that every
Whisper run missed.

| Validation (real audio, real captions) | Result | Time |
|---|---|---|
| English, 62 min | accepted: 93% agreement, offsets <= 0.06 s | 30 s |
| Hindi, first 64 min | accepted: 77% agreement, offsets <= 0.08 s | 96 s |
| Hindi, whole video, audio downloaded before the creator trimmed it | **refused**: timeline off at 5815 s | 125 s |

## Results

| 62-minute English video | Speech-to-text | Find clips total |
|---|---|---|
| Before (CPU int8, whole video) | ~68 min | ~70 min |
| Whisper medium on GPU, whole video | ~49 min (estimate, 0.78 x) | - |
| Balanced (checked captions) | **40.5 s** | **3 min 13 s** |

Exporting a clip adds Whisper for that clip only (90 s of audio for an
84-second clip).

## Profiles

| Profile | Finding clips | Exported clip captions |
|---|---|---|
| Accurate | Whisper on the whole video | from that transcript |
| Balanced (default) | checked YouTube captions | Whisper on the clip's range |
| Fast | checked YouTube captions | YouTube word timing (Whisper for manual tracks) |

All profiles fall back to whole-video Whisper (medium, beam 5) when captions
are missing or refused, and show why. `YouTube captions: Off` forces
Whisper. Compute `Auto` uses the GPU and shows a warning when it has to use
the CPU; `GPU only` fails instead of silently running on the CPU.

## Caption checks

A track is used only when all of these pass:

1. It is in the spoken language (manual first, then YouTube's `-orig`
   automatic track); machine translations are never used.
2. At least 80 words and 8 words per minute; not mostly `[Music]` markers;
   no phrase repeated throughout; expected script (Latin for English,
   Devanagari/Latin for Hindi); does not run past the end of the audio.
3. Three 30-second speech-dense windows (start, middle, end) are
   transcribed with Whisper: median agreement at least 45%, no window
   shifted by more than 1.5 s, and the speech is in the track's language.

## Caching and resume

* Caption transcripts, Whisper chunks and refined clip ranges are cached.
* A chunk is reused only with the same model and decoding settings; CPU
  int8 and GPU float16 chunks are never mixed.
* Inside a 30-minute chunk a checkpoint is saved every 2 minutes of audio.
* Caption style, metadata and clip selection changes never transcribe again;
  refining clips does not re-run discovery. Manual transcript edits are
  never overwritten by refinement.
* Projects created before profiles keep their settings and cache keys.
