"""Speed profiles, device choice, subtitle-first transcripts and refinement.

Measured on a 10-minute Hindi sample (RTX 2050, docs/PERFORMANCE.md): every
faster Whisper setting lost speech (small model, beam 1, batched decoding),
so Whisper always runs medium, beam 5, VAD. Profiles differ in how much of
the video Whisper has to hear:

* accurate - Whisper over the whole video (the original behaviour).
* balanced - checked YouTube captions find the clips; Whisper transcribes
  only the clips that are exported, for exact word timing.
* fast     - checked automatic captions with YouTube's own word timing are
  used for discovery and for the clip captions; Whisper only runs the short
  caption check (and the clips when the track has no word timing).

Any project falls back to whole-video Whisper when captions are missing or
refused, and says why. ``subtitles: off`` forces Whisper for every profile.
"""
import time
from pathlib import Path

import applog
import download_and_transcribe as dt
import subtitles

PROFILES = {
    "accurate": {"model": "medium", **dt.LEGACY_DECODE, "captions": "off",
                 "label": "Accurate",
                 "speed": "Whisper on the whole video"},
    "balanced": {"model": "medium", **dt.LEGACY_DECODE, "captions": "discover",
                 "label": "Balanced",
                 "speed": "YouTube captions find clips, Whisper transcribes exported clips"},
    "fast": {"model": "medium", **dt.LEGACY_DECODE, "captions": "all",
             "label": "Fast",
             "speed": "YouTube captions and word timing, Whisper only checks them"},
}
LEGACY_PROFILE = "accurate"          # saved projects without a profile
SUBTITLE_MODES = {"auto", "off"}
REFINE_PAD = 1.0                     # extra audio around a refined range
REFINE_MERGE = 3.0                   # join ranges closer than this

# Measured processing seconds per audio second for whole-video Whisper
# (medium, beam 5, VAD): RTX 2050 float16 idle 0.78 on the Hindi sample,
# i5-12450H int8 1.09 on the 62-minute English project. The caption check
# transcribes PROBE_WINDOWS x PROBE_SECONDS only.
WHISPER_RTF = {"cuda": 0.78, "cpu": 1.09}


def plan(options):
    """Effective transcription settings for a project's options."""
    profile = options.get("profile") or LEGACY_PROFILE
    spec = PROFILES[profile]
    model = options.get("whisper_model") or spec["model"]
    decode = {k: spec[k] for k in ("beam", "vad", "batch")}
    captions = spec["captions"] if (options.get("subtitles") or "off") == "auto" else "off"
    return {"profile": profile, "model": model, "decode": decode,
            "device": options.get("device") or "auto", "captions": captions}


def cache_key_parts(settings):
    """Extra transcript cache-key items; none for the legacy settings, so
    projects made before profiles keep their cached transcripts."""
    extra = []
    if settings["decode"] != dt.LEGACY_DECODE:
        extra.append(["decode", settings["decode"]])
    if settings["captions"] != "off":
        # discover and all share one caption transcript; they differ only
        # in whether exported clips are refined.
        extra.append(["subtitles", "auto", subtitles.SUBTITLE_VERSION])
    return extra


def estimate(settings, duration, gpu_ok, url=True):
    """Human estimate of how this project will be transcribed."""
    on_gpu = gpu_ok and settings["device"] in {"auto", "cuda"}
    where = "GPU" if on_gpu else "CPU"
    whole = None
    if duration:
        whole = max(1, round(float(duration) * WHISPER_RTF["cuda" if on_gpu else "cpu"] / 60))
    check = max(1, round(subtitles.PROBE_WINDOWS * subtitles.PROBE_SECONDS
                         * WHISPER_RTF["cuda" if on_gpu else "cpu"] / 60))
    label = PROFILES[settings["profile"]]["label"]
    if settings["captions"] != "off" and url:
        return {"mode": "captions", "device": where, "whisper_minutes": check,
                "fallback_minutes": whole,
                "text": f"{label}: YouTube captions when they pass checks "
                        f"(about {check} min of Whisper on {where}); otherwise "
                        f"Whisper {settings['model']} on the whole video"
                        + (f" (about {whole} min)" if whole else "")}
    return {"mode": "whisper", "device": where, "whisper_minutes": whole,
            "text": f"{label}: Whisper {settings['model']} on the whole video on {where}"
                    + (f" (about {whole} min)" if whole else "")}


def should_refine(data, settings):
    """Whisper for exported clips: caption transcripts, except Fast with
    YouTube's own word timing (automatic tracks)."""
    if not needs_refine(data):
        return False
    kind = (data.get("captions") or {}).get("kind")
    return not (settings["captions"] == "all" and kind == "automatic")


class Runtime:
    """Loads the Whisper model once, on the resolved device."""

    def __init__(self, settings, log=print):
        self.settings = settings
        self.log = log
        self.device, self.compute, self.warning = dt.resolve_device(settings["device"])
        if self.warning:
            log(f"WARNING: {self.warning}")
            applog.warning("transcribe.device", self.warning,
                           requested=settings["device"])
        self._model = None

    @property
    def model(self):
        if self._model is None:
            self._model = dt._load_model(self.settings["model"], self.device, self.compute)
        return self._model

    def release(self):
        """Free the model (and its VRAM) before Ollama needs the GPU."""
        if self._model is not None:
            self._model = None
            import gc
            gc.collect()

    def info(self):
        return {"profile": self.settings["profile"], "model": self.settings["model"],
                "device": self.device, "compute": self.compute,
                "beam": self.settings["decode"]["beam"],
                "requested_device": self.settings["device"],
                "warning": self.warning}

    def fingerprint(self):
        return dt.fingerprint(self.settings["model"], self.compute,
                              self.settings["decode"])


# --------------------------------------------------------------- captions

def caption_transcript(path, url, media, duration, tracks, runtime, log=print,
                       runner=None):
    """transcript dict from the URL's captions, or (None, reason).

    Never raises for a bad track: every refusal comes back as a reason so
    the caller can fall back to Whisper and say why.
    """
    key, kind = subtitles.choose_track(tracks)
    if key is None:
        return None, f"no usable caption track: {kind}"
    try:
        with applog.step("captions.fetch", f"Download {kind} captions ({key})"):
            file = subtitles.fetch(url, path, key, kind, runner=runner)
        words, markers = subtitles.parse_file(file)
        stats = subtitles.check(words, duration, tracks.get("language"), markers)
        windows = subtitles.probe_windows(words, duration)

        def heard(start, end):
            _, w, lang = dt.transcribe_range(
                runtime.model, media, start, end, path,
                decode=runtime.settings["decode"], name="caption-check")
            return w, lang
        with applog.step("captions.verify", f"Check captions in {len(windows)} windows"):
            verification = subtitles.verify(words, windows, heard, tracks.get("language"))
    except subtitles.SubtitleError as exc:
        return None, f"YouTube {kind} captions ({key}) refused: {exc}"
    except Exception as exc:                       # network, parse, ffmpeg
        applog.warning("captions.fetch", "caption track unusable", exc=exc)
        return None, f"YouTube {kind} captions ({key}) could not be read: {exc}"
    if not verification.get("verified"):
        return None, (f"YouTube {kind} captions ({key}) could not be checked: "
                      f"{verification.get('note')}")
    data = subtitles.build(words, tracks, key, kind, tracks.get("language"),
                           duration, stats, verification)
    log(f"Using YouTube {kind} captions ({key}): {stats['words']} words, "
        f"agreement with Whisper {verification['similarity']:.0%}")
    return data, None


# ------------------------------------------------------------- refinement

def needs_refine(data):
    return str(data.get("transcript_source") or "").startswith("youtube")


def merge_ranges(ranges, gap=REFINE_MERGE):
    out = []
    for start, end in sorted((float(a), float(b)) for a, b in ranges if b > a):
        if out and start <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


def missing_ranges(data, ranges, fingerprint):
    """Parts of ``ranges`` not yet refined with these settings or edited."""
    done = [(float(r["start"]), float(r["end"])) for r in data.get("refined") or []
            if r.get("by") == "edit" or r.get("settings") == fingerprint]
    todo = []
    for start, end in merge_ranges(ranges):
        pieces = [[start, end]]
        for a, b in done:
            nxt = []
            for lo, hi in pieces:
                if b <= lo or a >= hi:
                    nxt.append([lo, hi])
                    continue
                if lo < a:
                    nxt.append([lo, a])
                if b < hi:
                    nxt.append([b, hi])
            pieces = nxt
        todo.extend(p for p in pieces if p[1] - p[0] >= 0.5)
    return todo


def splice(data, start, end, segments, words, fingerprint):
    """Replace caption words in [start, end) with Whisper's words."""
    keep = [w for w in data.get("words") or [] if not start <= float(w["start"]) < end]
    new = [w for w in words if start <= float(w["start"]) < end]
    data["words"] = sorted(keep + new, key=lambda w: float(w["start"]))
    seg_keep = [s for s in data.get("segments") or []
                if not start <= float(s["start"]) < end]
    seg_new = [s for s in segments if start <= float(s["start"]) < end]
    data["segments"] = sorted(seg_keep + seg_new, key=lambda s: float(s["start"]))
    data.setdefault("refined", []).append(
        {"start": round(start, 3), "end": round(end, 3), "settings": fingerprint,
         "words": len(new), "at": time.time()})
    return data


def refine(path, data, ranges, runtime, save, log=print, progress=None):
    """Whisper word timing for every range that will be rendered.

    Caption transcripts only; Whisper transcripts are returned unchanged.
    ``save(data)`` is called after each range so an interruption keeps the
    finished ones.
    """
    if not needs_refine(data):
        return data, 0
    media = data.get("video")
    if not media or not Path(media).is_file():
        raise ValueError("The discovery audio is missing; Find clips again before rendering.")
    duration = float(data.get("duration") or 0)
    todo = missing_ranges(data, ranges, runtime.fingerprint())
    for number, (start, end) in enumerate(todo, 1):
        if progress:
            progress(number - 1, len(todo))
        lo, hi = max(0.0, start - REFINE_PAD), min(duration or end + REFINE_PAD, end + REFINE_PAD)
        with applog.step("transcribe.refine", f"Whisper for {start:.0f}-{end:.0f}s"):
            segments, words, _ = dt.transcribe_range(
                runtime.model, media, lo, hi, path,
                decode=runtime.settings["decode"], name="refine")
        splice(data, start, end, segments, words, runtime.fingerprint())
        save(data)
        log(f"Refined captions {start:.0f}-{end:.0f}s with Whisper "
            f"({sum(1 for w in words if start <= w['start'] < end)} words)")
    return data, len(todo)


def lock_edits(old, new):
    """Mark the range a manual transcript edit changed, so refinement keeps it."""
    if not needs_refine(new):
        return new
    a, b = old.get("words") or [], new.get("words") or []
    key = lambda w: (str(w.get("word", "")).strip(), round(float(w["start"]), 2),
                     round(float(w["end"]), 2), bool(w.get("hidden")), bool(w.get("emphasis")))
    first = 0
    while first < min(len(a), len(b)) and key(a[first]) == key(b[first]):
        first += 1
    if first == len(a) == len(b):
        return new
    last_a, last_b = len(a) - 1, len(b) - 1
    while last_a >= first and last_b >= first and key(a[last_a]) == key(b[last_b]):
        last_a, last_b = last_a - 1, last_b - 1
    times = [float(w["start"]) for w in a[first:last_a + 1] + b[first:last_b + 1]]
    times += [float(w["end"]) for w in a[first:last_a + 1] + b[first:last_b + 1]]
    if not times:                                   # pure deletion at an edge
        edge = b[first - 1] if first and first - 1 < len(b) else (b[0] if b else None)
        if edge is None:
            return new
        times = [float(edge["start"]), float(edge["end"])]
    new.setdefault("refined", []).append(
        {"start": round(min(times) - 0.25, 3), "end": round(max(times) + 0.25, 3),
         "by": "edit", "at": time.time()})
    return new
