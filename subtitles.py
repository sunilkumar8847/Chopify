"""Subtitle-first transcripts from YouTube caption tracks.

Whisper over a whole long video is the slowest step in Chopify. YouTube
usually already has a caption track in the spoken language: one the creator
uploaded (manual) or YouTube's own speech recognition (automatic). This
module picks such a track, parses its timing, and checks it before anything
uses it:

* only tracks in the spoken language (never machine translations);
* manual tracks are preferred over automatic ones;
* empty, sparse, repetitive, wrong-script or out-of-range tracks are refused;
* a few short windows are transcribed with Whisper and compared with the
  captions, which catches wrong text and a shifted timeline (for example a
  video the creator trimmed after the audio was downloaded).

A refused track is never used: the caller falls back to Whisper and shows
why. Captions only drive candidate discovery and AI judging; rendered clips
get Whisper word timestamps for their own ranges (``refine``).
"""
import difflib
import json
import re
import statistics
import subprocess
import sys
import unicodedata
from pathlib import Path
import ytdlp

SUBTITLE_VERSION = 1
FILE_PREFIX = "captions"
MIN_WORDS = 80                   # fewer words cannot drive discovery
MIN_WORDS_PER_MINUTE = 8         # over the whole source
MAX_MARKER_SHARE = 0.35          # "[Music]" and friends
MAX_REPEAT_SHARE = 0.2           # one 3-word phrase repeated everywhere
MAX_END_OVERRUN = 5.0            # captions past the end: different timeline
PROBE_WINDOWS = 3
PROBE_SECONDS = 30.0
MIN_PROBE_SIMILARITY = 0.45      # text agreement with Whisper (0..1)
MAX_PROBE_OFFSET = 1.5           # seconds between caption and Whisper words
SEGMENT_GAP = 1.2
SEGMENT_MAX_SECONDS = 15.0

_MARKER = re.compile(r"^[\[(（【♪].*[\])）】♪]$|^♪+$")
_TERMINAL = re.compile(r"[.!?।॥]$")
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


class SubtitleError(Exception):
    """A caption track exists but must not be used (reason in the message)."""


# ---------------------------------------------------------------- choosing

def base_language(code):
    return str(code or "").split("-")[0].split("_")[0].lower()


def track_list(info):
    """Compact caption inventory from a yt-dlp info dict (kept in source_meta)."""
    manual = sorted(k for k in (info.get("subtitles") or {})
                    if k and k != "live_chat")
    automatic = sorted((info.get("automatic_captions") or {}).keys())
    original = [k for k in automatic if k.endswith("-orig")]
    language = info.get("language") or (base_language(original[0]) if original else None)
    keep = set(original)
    if language:
        keep |= {k for k in automatic if base_language(k) == base_language(language)}
    return {"language": language, "manual": manual,
            "automatic": sorted(keep)}


def choose_track(tracks):
    """(key, kind) of the best track in the spoken language, or (None, reason)."""
    tracks = tracks or {}
    language = tracks.get("language")
    automatic = tracks.get("automatic") or []
    if not language:
        return None, "the spoken language is unknown"
    spoken = base_language(language)
    manual = [k for k in tracks.get("manual") or [] if base_language(k) == spoken]
    if manual:
        exact = [k for k in manual if k.lower() == str(language).lower()]
        return (exact or manual)[0], "manual"
    original = [k for k in automatic if k.endswith("-orig") and base_language(k) == spoken]
    if original:
        return original[0], "automatic"
    # Older videos have no "-orig" marker; then the plain code is the
    # recognized speech. When other "-orig" tracks exist, a plain code in a
    # different language is a translation and is never used.
    if not any(k.endswith("-orig") for k in automatic):
        plain = [k for k in automatic if k.lower() == spoken]
        if plain:
            return plain[0], "automatic"
    return None, f"no {spoken} caption track (only translations or none)"


# ----------------------------------------------------------------- fetching

def fetch(url, workdir, key, kind, runner=None):
    """Download one caption track as json3 (VTT/SRT accepted as fallback)."""
    workdir = Path(workdir)
    for old in workdir.glob(f"{FILE_PREFIX}.*"):
        if old.suffix in {".json3", ".vtt", ".srt"}:
            old.unlink(missing_ok=True)
    flag = "--write-subs" if kind == "manual" else "--write-auto-subs"
    cmd = [*ytdlp.base(), "--skip-download", "--no-playlist",
           flag, "--sub-langs", key, "--sub-format", "json3/vtt/srt",
           "-o", str(workdir / f"{FILE_PREFIX}.%(ext)s"), url]
    run = runner or (lambda c: subprocess.run(c, check=True, capture_output=True,
                                              text=True, encoding="utf-8",
                                              errors="replace", timeout=180))
    run(cmd)
    found = sorted(p for p in workdir.glob(f"{FILE_PREFIX}*")
                   if p.suffix in {".json3", ".vtt", ".srt"})
    if not found:
        raise SubtitleError("the caption download produced no file")
    return found[0]


# ------------------------------------------------------------------ parsing

def _clean(text):
    text = _TAG.sub("", str(text or "")).replace("​", "")
    return _SPACE.sub(" ", text).strip()


def _spread(text, start, end):
    """Words of one cue with times spread by character length."""
    tokens = text.split()
    if not tokens:
        return []
    end = max(end, start + 0.05 * len(tokens))
    total = sum(len(t) + 1 for t in tokens)
    out, at = [], start
    for token in tokens:
        span = (end - start) * (len(token) + 1) / total
        out.append({"word": token, "start": at, "end": at + span})
        at += span
    return out


def parse_json3(data):
    """Words from YouTube json3. Automatic tracks carry per-word offsets."""
    words = []
    for event in data.get("events") or []:
        segs = event.get("segs")
        if not segs:
            continue
        start = float(event.get("tStartMs") or 0) / 1000.0
        end = start + float(event.get("dDurationMs") or 0) / 1000.0
        timed = [s for s in segs if _clean(s.get("utf8"))]
        if not timed:
            continue
        if len(timed) > 1 or "tOffsetMs" in timed[0]:
            for i, seg in enumerate(timed):
                w_start = start + float(seg.get("tOffsetMs") or 0) / 1000.0
                nxt = (start + float(timed[i + 1].get("tOffsetMs") or 0) / 1000.0
                       if i + 1 < len(timed) else end)
                for word in _spread(_clean(seg.get("utf8")), w_start,
                                    max(nxt, w_start + 0.05)):
                    words.append(word)
        else:
            words.extend(_spread(_clean(timed[0].get("utf8")), start, end))
    return _finish(words)


_CUE_TIME = re.compile(
    r"(\d+:)?(\d{1,2}):(\d{2})[.,](\d{3})\s*-->\s*(\d+:)?(\d{1,2}):(\d{2})[.,](\d{3})")


def _seconds(h, m, s, ms):
    return int((h or "0:")[:-1] or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_cues(text):
    """Words from WebVTT or SRT text, with times spread inside each cue."""
    words, seen = [], set()
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    for block in blocks:
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            match = _CUE_TIME.search(line)
            if not match:
                continue
            g = match.groups()
            start, end = _seconds(*g[:4]), _seconds(*g[4:])
            body = _clean(" ".join(lines[i + 1:]))
            # Rolling automatic VTT repeats the previous line in each cue.
            fresh = [ln for ln in (_clean(x) for x in lines[i + 1:]) if ln and ln not in seen]
            seen = set(_clean(x) for x in lines[i + 1:])
            body = " ".join(fresh) if fresh else ""
            words.extend(_spread(body, start, end))
            break
    return _finish(words)


# "[ __ ]" is YouTube's mask for a bleeped word, ">>" a speaker change.
_ARTIFACT = re.compile(r"^(?:>+|\[|\]|_+|\[_*\])$")


def _finish(words):
    words = [w for w in words if w["word"] and not _MARKER.match(w["word"])
             and not _ARTIFACT.match(w["word"])]
    words.sort(key=lambda w: w["start"])
    for a, b in zip(words, words[1:]):          # no overlaps between words
        if a["end"] > b["start"]:
            a["end"] = max(a["start"] + 0.01, b["start"])
    return [{"word": " " + w["word"], "start": round(w["start"], 3),
             "end": round(w["end"], 3)} for w in words]


def parse_file(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix == ".json3":
        raw = json.loads(text)
        markers = sum(1 for e in raw.get("events") or [] for s in e.get("segs") or []
                      if _MARKER.match(_clean(s.get("utf8"))))
        return parse_json3(raw), markers
    words = parse_cues(text)
    markers = len(re.findall(r"\[[^\]]+\]", text))
    return words, markers


def segments_from(words, gap=SEGMENT_GAP, longest=SEGMENT_MAX_SECONDS):
    """Sentence-like segments (end on punctuation, a pause or 15 seconds)."""
    segments, current = [], []
    for word in words:
        if current and (word["start"] - current[-1]["end"] > gap
                        or word["end"] - current[0]["start"] > longest):
            segments.append(current)
            current = []
        current.append(word)
        if _TERMINAL.search(word["word"].strip()):
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return [{"start": s[0]["start"], "end": s[-1]["end"],
             "text": "".join(w["word"] for w in s)} for s in segments]


# --------------------------------------------------------------- validation

def _script_share(words, script):
    letters = [c for w in words for c in w["word"] if c.isalpha()]
    if not letters:
        return 0.0
    if script == "latin":
        hit = sum(1 for c in letters if c.isascii())
    else:
        hit = sum(1 for c in letters if "ऀ" <= c <= "ॿ")
    return hit / len(letters)


def check(words, duration, language, markers=0):
    """Raise SubtitleError when the parsed track is unusable; return stats."""
    count = len(words)
    if count < MIN_WORDS:
        raise SubtitleError(f"the track is empty or nearly empty ({count} words)")
    span = float(duration or words[-1]["end"])
    per_minute = count / max(span / 60.0, 1e-6)
    if per_minute < MIN_WORDS_PER_MINUTE:
        raise SubtitleError(
            f"the track covers too little speech ({per_minute:.1f} words per minute)")
    if markers and markers / (markers + count) > MAX_MARKER_SHARE:
        raise SubtitleError("the track is mostly [Music]/[Applause] markers")
    if duration and words[-1]["start"] > float(duration) + MAX_END_OVERRUN:
        raise SubtitleError(
            f"captions run to {words[-1]['start']:.0f}s but the source is "
            f"{float(duration):.0f}s long (different timeline)")
    tokens = [normalize(w["word"]) for w in words]
    grams = [" ".join(tokens[i:i + 3]) for i in range(len(tokens) - 2)]
    if grams:
        top = max(statistics.multimode(grams), key=grams.count)
        share = grams.count(top) * 3 / len(tokens)
        if share > MAX_REPEAT_SHARE:
            raise SubtitleError(f"the track repeats \"{top}\" throughout")
    spoken = base_language(language)
    if spoken == "en" and _script_share(words, "latin") < 0.9:
        raise SubtitleError("the English track is not in Latin script")
    if spoken in {"hi", "mr", "ne"}:
        mixed = _script_share(words, "devanagari") + _script_share(words, "latin")
        if mixed < 0.9:
            raise SubtitleError("the Hindi track is in an unexpected script")
    return {"words": count, "words_per_minute": round(per_minute, 1),
            "markers": markers}


_NUKTA = str.maketrans({"़": None, "ँ": "ं", "।": None,
                        "॥": None})


def normalize(word):
    """Spelling-tolerant form used to compare captions with Whisper."""
    text = unicodedata.normalize("NFC", str(word)).lower().translate(_NUKTA)
    return re.sub(r"[^\w]", "", text)


def compare(caption_words, whisper_words):
    """Text similarity (0..1) and median time offset of matching words."""
    a = [normalize(w["word"]) for w in caption_words]
    b = [normalize(w["word"]) for w in whisper_words]
    a_keep = [(t, w) for t, w in zip(a, caption_words) if t]
    b_keep = [(t, w) for t, w in zip(b, whisper_words) if t]
    if not a_keep or not b_keep:
        return 0.0, None
    char = difflib.SequenceMatcher(None, " ".join(t for t, _ in a_keep),
                                   " ".join(t for t, _ in b_keep), autojunk=False).ratio()
    matcher = difflib.SequenceMatcher(None, [t for t, _ in a_keep],
                                      [t for t, _ in b_keep], autojunk=False)
    offsets = []
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            offsets.append(float(b_keep[block.b + k][1]["start"])
                           - float(a_keep[block.a + k][1]["start"]))
    offset = statistics.median(offsets) if len(offsets) >= 3 else None
    return round(char, 3), (round(offset, 2) if offset is not None else None)


def probe_windows(words, duration, count=PROBE_WINDOWS, seconds=PROBE_SECONDS):
    """Speech-dense windows spread over the source (start, end)."""
    duration = float(duration or words[-1]["end"])
    if duration <= seconds * 1.5:
        return [(0.0, duration)]
    windows = []
    for i in range(count):
        lo = duration * (i + 0.5) / count - duration / (2 * count)
        hi = lo + duration / count - seconds
        best, best_n = lo, -1
        step = max(5.0, (hi - lo) / 40)
        t = lo
        while t <= max(lo, hi):
            n = sum(1 for w in words if t <= w["start"] < t + seconds)
            if n > best_n:
                best, best_n = t, n
            t += step
        windows.append((round(best, 3), round(min(duration, best + seconds), 3)))
    return windows


def verify(words, windows, transcribe_window, language):
    """Compare captions with Whisper in each window; raise when they disagree.

    ``transcribe_window(start, end)`` returns (whisper_words, language).
    """
    results = []
    for start, end in windows:
        heard, detected = transcribe_window(start, end)
        caption = [w for w in words if start <= w["start"] < end]
        inner = [w for w in heard if start + 1.0 <= w["start"] < end - 1.0] or heard
        similarity, offset = compare(caption, inner)
        results.append({"start": start, "end": end, "similarity": similarity,
                        "offset": offset, "caption_words": len(caption),
                        "whisper_words": len(heard), "language": detected})
    spoken = base_language(language)
    heard_any = [r for r in results if r["whisper_words"] >= 5]
    if not heard_any:
        return {"windows": results, "verified": False,
                "note": "Whisper heard no speech in the check windows"}
    wrong = [r for r in heard_any if r["language"] and base_language(r["language"]) != spoken
             and not (spoken == "hi" and base_language(r["language"]) in {"ur", "en"})]
    if len(wrong) * 2 > len(heard_any):
        raise SubtitleError(
            f"the speech is {wrong[0]['language']}, not {spoken}")
    similarity = statistics.median(r["similarity"] for r in heard_any)
    if similarity < MIN_PROBE_SIMILARITY:
        raise SubtitleError(
            f"the captions do not match the speech (agreement {similarity:.0%})")
    shifted = [r for r in heard_any if r["offset"] is not None
               and abs(r["offset"]) > MAX_PROBE_OFFSET]
    if shifted:
        r = shifted[0]
        raise SubtitleError(
            f"the caption timeline is {r['offset']:+.1f}s off at {r['start']:.0f}s "
            "(the video may have been edited after download)")
    return {"windows": results, "verified": True,
            "similarity": round(similarity, 3)}


def build(words, tracks, key, kind, language, duration, stats, verification):
    """transcript.json content for a caption-based transcript."""
    return {
        "language": base_language(language),
        "duration": float(duration),
        "model": None,
        "segments": segments_from(words),
        "words": words,
        "transcript_source": f"youtube_{kind}",
        "captions": {"version": SUBTITLE_VERSION, "track": key, "kind": kind,
                     "stats": stats, "verification": verification},
        "refined": [],
    }
