"""Gameplay-only scene classification (Phase 4, observation mode).

Labels each stretch of a recording as one of::

    gameplay, starting_soon, intro, menu, loading, brb, outro, static, unknown

from cheap local signals only:

* **Frames**: keyframes decoded at ``SAMPLE_FPS`` and shrunk to 64x36 gray
  (ffmpeg ``-skip_frame nokey``, so a 3-hour file is fast). Motion = mean
  absolute difference between samples; static = nearly no change; loop = a
  frame that closely matches one a few samples earlier while the picture
  keeps changing (animated "starting soon" screens); HUD = edges that stay
  put in the border while the centre moves.
* **Audio**: RMS per second (8 kHz mono PCM through numpy).
* **OCR** (RapidOCR, optional): on a few selected frames per non-gameplay
  stretch, matched against keywords ("starting soon", "be right back",
  "thanks for watching", "loading", menu words...).
* **Cloud vision** (optional): only through the AI Gateway, only for
  ambiguous stretches, one downscaled frame each, at most ``MAX_VISION``
  frames per recording, and only when the vision route's privacy rules allow
  real data. Never whole recordings.

Observation only: ``analyze`` writes ``scenes.json`` and nothing is cut.
Filtering stays behind the ``scene_filtering`` flag (off) until the labelled
evaluation meets the targets (>= 95 % gameplay time kept, >= 90 % of
intro/BRB/outro time identified); see ``scene_eval.py``.
"""
import json
import re
import subprocess
from pathlib import Path

import numpy as np

import applog

LABELS = ("gameplay", "starting_soon", "intro", "menu", "loading", "brb", "outro",
          "static", "unknown")
NON_GAMEPLAY = {"starting_soon", "intro", "menu", "loading", "brb", "outro", "static"}
SAMPLE_FPS = 1.0
WIDTH, HEIGHT = 64, 36
WINDOW = 5.0                 # seconds per decision window
MIN_SCENE = 10.0             # shorter runs merge into neighbours
MAX_OCR = 40                 # OCR frames per recording
MAX_VISION = 10
VERSION = 1

KEYWORDS = [
    ("starting_soon", r"starting\s*soon|stream\s*(will\s*)?start|going\s*live|be\s*live\s*soon"),
    ("brb", r"\bbrb\b|be\s*right\s*back|right\s*back|taking\s*a\s*break|on\s*a\s*break|intermission"),
    ("outro", r"thanks?\s*(you\s*)?for\s*watching|see\s*you\s*(next|soon|tomorrow)|stream\s*(has\s*)?ended|\boutro\b|goodbye|good\s*night"),
    ("intro", r"\bintro\b|welcome\s*(to|back)|\bepisode\s*\d"),
    ("loading", r"loading|please\s*wait|connecting|matchmaking|searching\s*for"),
    ("menu", r"\b(play|settings|options|quit|exit|continue|new\s*game|load\s*game|multiplayer|campaign|store|inventory|controls|resume)\b"),
]


# ---------------------------------------------------------------- signals

def read_frames(video, fps=SAMPLE_FPS, keyframes=True, start=0.0, duration=None):
    """(times, frames[n,H,W] uint8) sampled from the video."""
    cmd = ["ffmpeg", "-v", "error"]
    if keyframes:
        cmd += ["-skip_frame", "nokey"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(video)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-an", "-vf", f"fps={fps},scale={WIDTH}:{HEIGHT}:flags=area,format=gray",
            "-f", "rawvideo", "-"]
    data = subprocess.run(cmd, capture_output=True, check=True).stdout
    size = WIDTH * HEIGHT
    count = len(data) // size
    frames = np.frombuffer(data[:count * size], np.uint8).reshape(count, HEIGHT, WIDTH)
    times = start + np.arange(count) / fps
    return times, frames


def read_audio_rms(video, rate=8000):
    """RMS (0..1) per second, or an empty array when there is no audio."""
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1",
                           "-ar", str(rate), "-f", "s16le", "-"], capture_output=True)
    pcm = np.frombuffer(proc.stdout[: len(proc.stdout) // 2 * 2], np.int16)
    seconds = len(pcm) // rate
    if not seconds:
        return np.zeros(0)
    blocks = pcm[: seconds * rate].reshape(seconds, rate).astype(np.float32) / 32768.0
    return np.sqrt((blocks ** 2).mean(axis=1))


def frame_features(frames):
    """Per-sample motion, edges, HUD stability and loop similarity."""
    n = len(frames)
    f = frames.astype(np.float32)
    motion = np.zeros(n)
    if n > 1:
        motion[1:] = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2)) / 255.0
        motion[0] = motion[1]
    gy = np.abs(np.diff(f, axis=1))[:, :, :-1]
    gx = np.abs(np.diff(f, axis=2))[:, :-1, :]
    edges = (gx + gy) > 40
    loop = np.zeros(n)
    for i in range(n):
        best = 1.0
        for lag in range(2, min(8, i) + 1):
            best = min(best, float(np.abs(f[i] - f[i - lag]).mean() / 255.0))
        loop[i] = best if i >= 2 else 1.0
    brightness = f.mean(axis=(1, 2)) / 255.0
    return {"motion": motion, "edges": edges, "loop": loop, "brightness": brightness}


def hud_score(edges, frames):
    """Share of border edge pixels that stay fixed while the centre changes."""
    if len(edges) < 3:
        return 0.0
    h, w = edges.shape[1:]
    border = np.zeros((h, w), bool)
    border[: h // 5, :] = border[-h // 5:, :] = True
    border[:, : w // 6] = border[:, -w // 6:] = True
    stable = edges.mean(axis=0) > 0.8
    centre = frames[:, h // 4: 3 * h // 4, w // 4: 3 * w // 4].astype(np.float32)
    centre_change = float(np.abs(np.diff(centre, axis=0)).mean() / 255.0)
    if centre_change < 0.02:
        return 0.0
    return float((stable & border).sum() / max(1, border.sum()))


# ------------------------------------------------------------------- OCR

class OCR:
    """RapidOCR wrapper; ``available`` is False when the package is missing."""

    def __init__(self):
        self.engine = None
        try:
            from rapidocr_onnxruntime import RapidOCR
            self.engine = RapidOCR()
        except Exception as exc:            # noqa: BLE001 - OCR is optional
            applog.info("scenes.ocr", f"OCR unavailable: {type(exc).__name__}")

    @property
    def available(self):
        return self.engine is not None

    def text(self, video, at):
        """Text read from one full-resolution-ish frame (640 px wide)."""
        if not self.engine:
            return ""
        proc = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", str(video),
                               "-frames:v", "1", "-vf", "scale=640:-2", "-f", "image2pipe",
                               "-vcodec", "png", "-"], capture_output=True)
        if not proc.stdout:
            return ""
        import cv2
        image = cv2.imdecode(np.frombuffer(proc.stdout, np.uint8), cv2.IMREAD_COLOR)
        result, _ = self.engine(image)
        return " ".join(item[1] for item in (result or []))


def keyword_label(text):
    text = (text or "").lower()
    for label, pattern in KEYWORDS:
        if re.search(pattern, text):
            if label == "menu" and len(re.findall(KEYWORDS[-1][1], text)) < 2:
                continue                       # one stray word is not a menu
            return label
    return None


# -------------------------------------------------------------- classify

def classify_windows(times, feats, rms, window=WINDOW):
    """[(start, end, label, confidence, stats)] before OCR and smoothing."""
    out = []
    if not len(times):
        return out
    end_time = float(times[-1]) + 1.0 / SAMPLE_FPS
    start = float(times[0])
    while start < end_time - 0.01:
        stop = min(end_time, start + window)
        idx = np.where((times >= start) & (times < stop))[0]
        if not len(idx):
            start = stop
            continue
        motion = float(np.median(feats["motion"][idx]))
        still = float((feats["motion"][idx] < 0.004).mean())
        loop = float(np.median(feats["loop"][idx]))
        hud = hud_score(feats["edges"][idx[0]: idx[-1] + 1],
                        feats["frames"][idx[0]: idx[-1] + 1]) if len(idx) >= 3 else 0.0
        audio = float(rms[int(start): int(stop) + 1].mean()) if len(rms) > int(start) else 0.0
        dark = float(np.median(feats["brightness"][idx])) < 0.04
        stats = {"motion": round(motion, 4), "still": round(still, 2), "loop": round(loop, 4),
                 "hud": round(hud, 3), "audio": round(audio, 4)}
        if still >= 0.8 or dark:
            label, conf = "static", 0.8
        elif loop < 0.01 and motion > 0.004:
            label, conf = "static", 0.6        # looping animation
        elif motion >= 0.012 or hud >= 0.05:
            label, conf = "gameplay", min(0.95, 0.6 + motion * 8 + hud)
        else:
            label, conf = "unknown", 0.4
        out.append([start, stop, label, round(conf, 2), stats])
        start = stop
    return out


def smooth(windows, min_scene=MIN_SCENE):
    """Merge runs; absorb runs shorter than min_scene into a neighbour."""
    runs = []
    for start, end, label, conf, stats in windows:
        if runs and runs[-1]["label"] == label:
            runs[-1]["end"] = end
            runs[-1]["conf"].append(conf)
        else:
            runs.append({"start": start, "end": end, "label": label, "conf": [conf]})
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for i, run in enumerate(runs):
            if run["end"] - run["start"] >= min_scene:
                continue
            # gameplay is never absorbed into a non-gameplay neighbour
            if run["label"] == "gameplay" and not any(
                    r["label"] == "gameplay" for r in runs[max(0, i - 1): i + 2] if r is not run):
                continue
            j = i - 1 if i > 0 else i + 1
            if i + 1 < len(runs) and i > 0 and (runs[i + 1]["end"] - runs[i + 1]["start"]
                                                > runs[i - 1]["end"] - runs[i - 1]["start"]):
                j = i + 1
            target = runs[j]
            target["start"] = min(target["start"], run["start"])
            target["end"] = max(target["end"], run["end"])
            target["conf"] += run["conf"]
            runs.pop(i)
            changed = True
            break
        merged = []
        for run in runs:
            if merged and merged[-1]["label"] == run["label"]:
                merged[-1]["end"] = run["end"]
                merged[-1]["conf"] += run["conf"]
            else:
                merged.append(run)
        runs = merged
    return [{"start": round(r["start"], 2), "end": round(r["end"], 2), "label": r["label"],
             "confidence": round(float(np.mean(r["conf"])), 2)} for r in runs]


def refine(scenes, video, ocr=None, vision=None, duration=None):
    """Name non-gameplay scenes with OCR/vision, then by position (intro/outro)."""
    budget = {"ocr": MAX_OCR, "vision": MAX_VISION}
    for scene in scenes:
        scene["evidence"] = []
        if scene["label"] == "gameplay":
            continue
        length = scene["end"] - scene["start"]
        found = None
        if ocr is not None and ocr.available:
            points = [scene["start"] + length * f for f in (0.25, 0.5, 0.75)]
            for at in points[: max(1, min(3, budget["ocr"]))]:
                if budget["ocr"] <= 0:
                    break
                budget["ocr"] -= 1
                text = ocr.text(video, at)
                label = keyword_label(text)
                if text:
                    scene["evidence"].append({"ocr": text[:120], "at": round(at, 1)})
                if label:
                    found = label
                    break
        if found is None and vision is not None and budget["vision"] > 0:
            budget["vision"] -= 1
            label = vision(video, scene["start"] + length / 2)
            if label in LABELS:
                found = label
                scene["evidence"].append({"vision": label})
        if found:
            scene["label"] = found
            scene["confidence"] = max(scene["confidence"], 0.85)
    gameplay = [s for s in scenes if s["label"] == "gameplay"]
    if gameplay:
        first, last = gameplay[0]["start"], gameplay[-1]["end"]
        for scene in scenes:
            if scene["label"] in ("static", "unknown") and not scene["evidence"]:
                if scene["end"] <= first and scene["start"] < 600:
                    scene["label"] = "intro" if scene["label"] == "unknown" else "starting_soon"
                elif scene["start"] >= last and duration and duration - scene["end"] < 900:
                    scene["label"] = "outro"
    return scenes


def summarize(scenes):
    totals = {label: 0.0 for label in LABELS}
    for scene in scenes:
        totals[scene["label"]] += scene["end"] - scene["start"]
    total = sum(totals.values()) or 1.0
    return {"seconds": {k: round(v, 1) for k, v in totals.items() if v},
            "gameplay_share": round(totals["gameplay"] / total, 3)}


def analyze(video, out=None, use_ocr=True, vision=None):
    """Classify a whole recording. Writes ``out`` (scenes.json) when given."""
    times, frames = read_frames(video)
    feats = frame_features(frames)
    feats["frames"] = frames
    rms = read_audio_rms(video)
    duration = float(times[-1]) + 1.0 if len(times) else 0.0
    scenes = smooth(classify_windows(times, feats, rms))
    scenes = refine(scenes, video, OCR() if use_ocr else None, vision, duration)
    result = {"version": VERSION, "mode": "observation", "duration": round(duration, 2),
              "samples": int(len(times)), "scenes": scenes, "summary": summarize(scenes)}
    if out:
        Path(out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def label_at(scenes, t):
    for scene in scenes:
        if scene["start"] <= t < scene["end"]:
            return scene["label"]
    return "unknown"


def gateway_vision(project_id=None):
    """A vision callback through the AI Gateway (privacy rules apply), or None."""
    from ai import gateway
    from ai.base import VISION
    ok, _ = gateway.get().precheck(VISION)
    if not ok:
        return None
    schema = {"type": "object", "additionalProperties": False, "required": ["label"],
              "properties": {"label": {"type": "string", "enum": list(LABELS)}}}

    def ask(video, at):
        proc = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", str(video),
                               "-frames:v", "1", "-vf", "scale=512:-2", "-q:v", "5", "-f",
                               "image2pipe", "-vcodec", "mjpeg", "-"], capture_output=True)
        if not proc.stdout:
            return None
        try:
            result = gateway.get().analyze_images(
                [proc.stdout], "Classify this livestream frame as one of: " + ", ".join(LABELS),
                schema=schema, project_id=project_id)
            return (result.data or {}).get("label")
        except Exception as exc:            # noqa: BLE001 - vision is optional
            applog.info("scenes.vision", f"Vision unavailable: {exc}")
            return None
    return ask
