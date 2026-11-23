"""Continuous live clipping (Phase 6).

For every live session (``live_ingest``) this module:

1. **Detects** possible moments on the newest footage, locally and cheaply:
   audio-energy spikes against the session's rolling baseline (median/MAD,
   robust to loud games), with a cooldown so one moment is one candidate.
   The scene label (Phase 4) is recorded on the candidate; nothing is
   discarded by it (scene filtering is still off).
2. **Pins** generous context in the ring buffer (``PIN_BEFORE`` before and
   ``PIN_AFTER`` after the peak) so setup and payoff survive ring trimming.
3. **Queues AI evaluation** as a scheduler ``ai`` task once the payoff has
   been recorded: transcribe the pinned audio through the AI Gateway (Groq
   Free by default; privacy rules apply), refine the boundaries with
   ``boundaries.refine_segments`` (setup/payoff rules, 120 s Shorts maximum)
   and judge the text with the configured LLM route (``highlights``). If a
   provider is rate-limited or out of quota the task fails *retryably* and
   the scheduler retries later with backoff; the candidate and its pin stay,
   ingestion continues, and nothing switches to a paid provider. Local Qwen
   is used only when the user turned local fallback on.
4. **Renders** accepted clips as scheduler ``render`` tasks with the
   unchanged ``render_clips.render`` (9:16 Shorts, Roman captions) into the
   session project's ``clips`` folder, then releases the pin.

Candidates are stored in ``<project>/live/candidates.json`` so a Studio
restart continues where it stopped (scheduler tasks are deduplicated).
"""
import json
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

import applog
import boundaries
import highlights
import projects
import scenes
import score_clips

DETECT_EVERY = 30.0          # seconds of new footage between detection passes
RMS_STEP = 0.5               # seconds per RMS sample
BASELINE_SECONDS = 600
Z_THRESHOLD = 4.0
MIN_RMS = 0.02
COOLDOWN = 45.0
PRE_CONTEXT = 30.0           # initial clip start before the peak
POST_CONTEXT = 15.0          # initial clip end after the peak
PIN_BEFORE = 75.0            # kept in the ring for setup
PIN_AFTER = 45.0             # kept in the ring for payoff
JUDGE_BEFORE = 45.0          # audio sent for transcription: setup ...
JUDGE_AFTER = 20.0           # ... and payoff (small uploads on slow connections)
MIN_SCORE = 6.5
MAX_CLIPS_PER_HOUR = 30
ASPECT = "9:16"


class Retry(RuntimeError):
    """A temporary condition (quota, footage not yet buffered): try again later."""
    retryable = True


class Final(RuntimeError):
    retryable = False


def rms_series(path, step=RMS_STEP, rate=8000):
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar",
                           str(rate), "-f", "s16le", "-"], capture_output=True)
    pcm = np.frombuffer(proc.stdout[: len(proc.stdout) // 2 * 2], np.int16).astype(np.float32)
    n = int(rate * step)
    count = len(pcm) // n
    if not count:
        return np.zeros(0)
    blocks = pcm[: count * n].reshape(count, n) / 32768.0
    return np.sqrt((blocks ** 2).mean(axis=1))


def find_peaks(values, baseline, z=Z_THRESHOLD, min_rms=MIN_RMS, run=2):
    """Indexes of sustained energy peaks (z-score over a robust baseline)."""
    ref = np.asarray(baseline if len(baseline) >= 40 else values, dtype=float)
    if not len(values) or not len(ref):
        return []
    median = float(np.median(ref))
    mad = float(np.median(np.abs(ref - median))) * 1.4826 + 1e-4
    score = (np.asarray(values) - median) / mad
    hot = (score >= z) & (np.asarray(values) >= min_rms)
    peaks, i = [], 0
    while i < len(hot):
        if hot[i]:
            j = i
            while j < len(hot) and hot[j]:
                j += 1
            if j - i >= run:
                best = i + int(np.argmax(score[i:j]))
                peaks.append((best, float(score[best])))
            i = j
        else:
            i += 1
    return peaks


class SessionClipper:
    """Per-session detection state (runs in its own thread)."""

    def __init__(self, owner, session):
        self.owner = owner
        self.session = session
        self.path = session.path
        self.file = self.path / "live" / "candidates.json"
        self.lock = threading.Lock()
        self.baseline = deque(maxlen=int(BASELINE_SECONDS / RMS_STEP))
        self.analysed_until = 0.0
        self.last_peak = -1e9
        self.inbox = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"clipper-{self.path.name}")
        self.thread.start()

    # ------------------------------------------------------- candidates
    def candidates(self):
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _write(self, items):
        temp = self.file.with_suffix(".tmp")
        temp.write_text(json.dumps(items, indent=1), encoding="utf-8")
        temp.replace(self.file)

    def update(self, cid, **changes):
        with self.lock:
            items = self.candidates()
            for item in items:
                if item["id"] == cid:
                    item.update(changes, updated=time.time())
            self._write(items)
            return next((i for i in items if i["id"] == cid), None)

    def get(self, cid):
        return next((i for i in self.candidates() if i["id"] == cid), None)

    # -------------------------------------------------------- detection
    def _run(self):
        while True:
            item = self.inbox.get()
            if item is None:
                return
            try:
                self._on_segment(item)
            except Exception as exc:           # noqa: BLE001 - detection must not stop ingest
                applog.warning("live.detect", f"Detection failed: {exc}",
                               project_id=self.path.name)

    def _on_segment(self, segment):
        newest = float(segment["end"])
        self._release_waiting(newest)
        if newest - self.analysed_until < self.owner.settings["detect_every"]:
            return
        start = max(self.analysed_until, newest - self.owner.settings["detect_every"] * 2)
        window = self.path / "live" / "work" / "detect.ts"
        window.parent.mkdir(parents=True, exist_ok=True)
        try:
            file, offset = self.session.ring.extract(start, newest, window)
        except Exception:                      # noqa: BLE001 - gap in the buffer
            self.analysed_until = newest
            return
        file_start = start - offset
        values = rms_series(file)
        times = file_start + np.arange(len(values)) * RMS_STEP
        fresh = times >= self.analysed_until
        peaks = find_peaks(values[fresh], list(self.baseline),
                           z=self.owner.settings["z_threshold"])
        label = self._scene_label(file)
        for index, z in peaks:
            peak = float(times[fresh][index])
            if peak - self.last_peak < self.owner.settings["cooldown"]:
                continue
            if self._clips_last_hour() >= self.owner.settings["max_clips_per_hour"]:
                break
            self.last_peak = peak
            self._add_candidate(peak, z, label)
        self.baseline.extend(float(v) for v in values[fresh])
        self.analysed_until = newest
        window.unlink(missing_ok=True)

    def _scene_label(self, file):
        try:
            times, frames = scenes.read_frames(file, keyframes=False)
            feats = scenes.frame_features(frames)
            feats["frames"] = frames
            windows = scenes.classify_windows(times, feats, np.zeros(0), window=10.0)
            labels = [w[2] for w in windows]
            return max(set(labels), key=labels.count) if labels else "unknown"
        except Exception:                      # noqa: BLE001 - label is informational
            return "unknown"

    def _clips_last_hour(self):
        hour = time.time() - 3600
        return sum(1 for c in self.candidates() if c.get("created", 0) >= hour)

    def _add_candidate(self, peak, z, scene):
        st = self.owner.settings
        pin = self.session.ring.pin(peak - st["pin_before"], peak + st["pin_after"],
                                    "live candidate")
        cid = f"c{int(peak * 10):08d}"
        item = {"id": cid, "peak": round(peak, 2), "z": round(z, 2), "scene": scene,
                "start": round(max(0.0, peak - self.owner.settings["pre_context"]), 2),
                "end": round(peak + self.owner.settings["post_context"], 2),
                "pin": pin, "state": "waiting_payoff", "created": time.time(),
                "detected_at": time.time()}
        with self.lock:
            items = self.candidates()
            if any(i["id"] == cid for i in items):
                return
            items.append(item)
            self._write(items)
        applog.info("live.candidate", f"Possible moment at {peak:.0f}s (z {z:.1f})",
                    project_id=self.path.name, candidate=cid, scene=scene)

    def _release_waiting(self, newest):
        """Queue AI evaluation once the payoff (pin end) is in the buffer."""
        for item in self.candidates():
            if item["state"] == "waiting_payoff" and newest >= item["peak"] + self.owner.settings["pin_after"]:
                self.update(item["id"], state="ai_queued")
                self.owner.queue_ai(self, item["id"])


class LiveClipper:
    """Connects live sessions, the scheduler and the AI Gateway."""

    def __init__(self, scheduler, settings=None, transcribe=None, judge=None, render=None,
                 on_clip=None):
        self.scheduler = scheduler
        self.on_clip = on_clip        # (project_id, clip_path, candidate) after each render
        self.settings = {"detect_every": DETECT_EVERY, "z_threshold": Z_THRESHOLD,
                         "cooldown": COOLDOWN, "pre_context": PRE_CONTEXT,
                         "post_context": POST_CONTEXT, "min_score": MIN_SCORE,
                         "max_clips_per_hour": MAX_CLIPS_PER_HOUR, "pin_before": PIN_BEFORE,
                         "pin_after": PIN_AFTER, "judge_before": JUDGE_BEFORE,
                         "judge_after": JUDGE_AFTER, **(settings or {})}
        self.clippers = {}
        self.sessions = {}
        self.transcribe = transcribe or self._transcribe
        self.judge = judge or self._judge
        self.render = render or self._render
        self.lock = threading.Lock()

    # hooks for Studio ------------------------------------------------
    def on_segment(self, session, segment):
        with self.lock:
            clipper = self.clippers.get(session.project_id)
            if clipper is None:
                clipper = self.clippers[session.project_id] = SessionClipper(self, session)
                self.sessions[session.project_id] = session
        clipper.inbox.put(segment)

    def queue_ai(self, clipper, cid):
        self.scheduler.add_task(clipper.path.name, "ai",
                                {"kind": "live:judge", "candidate": cid},
                                max_failures=12, dedupe=f"{clipper.path.name}:judge:{cid}")

    def status(self, project_id):
        clipper = self.clippers.get(project_id)
        return clipper.candidates() if clipper else []

    def shutdown(self):
        for clipper in self.clippers.values():
            clipper.inbox.put(None)

    # scheduler handlers ----------------------------------------------
    def _clipper(self, project_id):
        clipper = self.clippers.get(project_id)
        if clipper is None:
            raise Retry("The live session is not running yet.")
        return clipper

    def handle_judge(self, payload, cancel, project_id):
        clipper = self._clipper(project_id)
        item = clipper.get(payload["candidate"])
        if item is None or item["state"] not in ("ai_queued", "ai_waiting"):
            return {"skipped": True}
        ring = clipper.session.ring
        work = clipper.path / "live" / "work"
        work.mkdir(parents=True, exist_ok=True)
        lo = item["peak"] - min(self.settings["judge_before"], self.settings["pin_before"])
        hi = item["peak"] + min(self.settings["judge_after"], self.settings["pin_after"])
        try:
            file, offset = ring.extract(max(0.0, lo), hi, work / f"{item['id']}-judge.ts")
        except Exception as exc:               # noqa: BLE001
            clipper.update(item["id"], state="failed", error=f"Footage missing: {exc}")
            ring.unpin(item["pin"])
            raise Final(str(exc)) from exc
        file_start = max(0.0, lo) - offset
        try:
            words, language = self.transcribe(file, file_start, project_id)
        except Retry as exc:
            clipper.update(item["id"], state="ai_waiting", error=str(exc))
            raise
        finally:
            file.unlink(missing_ok=True)
        seg = self._refine(item, words)
        text = " ".join(w["word"].strip() for w in words
                        if seg["start"] <= float(w["start"]) < seg["end"])
        try:
            score, judged = self.judge(clipper.path, seg, text, words, language)
        except Retry as exc:
            clipper.update(item["id"], state="ai_waiting", error=str(exc), seg=seg)
            raise
        keep = score is not None and score >= self.settings["min_score"]
        if not judged:
            keep = item["z"] >= Z_THRESHOLD * 1.5     # no AI configured: strong spikes only
        words_file = clipper.path / "live" / "work" / f"{item['id']}.words.json"
        words_file.write_text(json.dumps(words), encoding="utf-8")
        clipper.update(item["id"], state="accepted" if keep else "rejected", seg=seg,
                       score=score, ai_judged=judged, language=language, text=text[:400],
                       judged_at=time.time())
        if keep:
            self.scheduler.add_task(project_id, "render",
                                    {"kind": "live:render", "candidate": item["id"]},
                                    max_failures=2, dedupe=f"{project_id}:render:{item['id']}")
        else:
            ring.unpin(item["pin"])
        return {"keep": keep, "score": score}

    def handle_render(self, payload, cancel, project_id):
        clipper = self._clipper(project_id)
        item = clipper.get(payload["candidate"])
        if item is None or item["state"] not in ("accepted", "rendering"):
            return {"skipped": True}
        clipper.update(item["id"], state="rendering")
        words = json.loads((clipper.path / "live" / "work" / f"{item['id']}.words.json")
                           .read_text(encoding="utf-8"))
        seg = {**item["seg"], "id": f"live-{item['id']}"}
        try:
            out = self.render(clipper, seg, words)
        except Exception as exc:               # noqa: BLE001
            clipper.update(item["id"], state="accepted", error=str(exc)[:300])
            raise
        clipper.session.ring.unpin(item["pin"])
        clipper.update(item["id"], state="done", clip=str(out), rendered_at=time.time(),
                       latency_seconds=round(time.time() - item["detected_at"], 1))
        applog.info("live.clip", f"Live clip ready: {Path(out).name}", project_id=project_id,
                    candidate=item["id"])
        if self.on_clip:
            try:
                self.on_clip(project_id, Path(out), clipper.get(item["id"]))
            except Exception as exc:           # noqa: BLE001 - publishing must not undo a clip
                applog.warning("publish.auto", f"Auto-publish skipped: {exc}",
                               project_id=project_id)
        return {"clip": str(out)}

    # building blocks ---------------------------------------------------
    def _refine(self, item, words):
        seg = {"start": item["start"], "end": item["end"], "score": 7.0}
        lo = item["peak"] - min(self.settings["judge_before"], self.settings["pin_before"])
        hi = item["peak"] + min(self.settings["judge_after"], self.settings["pin_after"])
        sentences = score_clips.sentenceize(words)
        if sentences:
            refined, _ = boundaries.refine_segments(
                [seg], sentences, mode="auto", min_len=15, max_len=boundaries.SHORTS_MAX,
                band=(25, 75), aspect=ASPECT)
            if refined:
                seg = {**seg, **refined[0]}
        start = max(lo, float(seg["start"]))
        end = min(hi, float(seg["end"]))
        if end - start > boundaries.SHORTS_MAX:
            end = start + boundaries.SHORTS_MAX            # hard 120 s Shorts limit
        if end - start < 10:
            start, end = max(lo, item["peak"] - 20), min(hi, item["peak"] + 10)
        hook = " ".join(w["word"].strip() for w in words if start <= float(w["start"]) < end)
        return {"start": round(start, 2), "end": round(end, 2),
                "hook": " ".join(hook.split()[:8]) or "Live moment"}

    def _transcribe(self, file, file_start, project_id):
        from ai import gateway as gateway_mod
        from ai.base import AIError, QuotaExhausted, RateLimited, TemporaryError
        try:
            result = gateway_mod.get().transcribe(str(file), offset=file_start,
                                                  project_id=project_id)
        except (QuotaExhausted, RateLimited, TemporaryError) as exc:
            raise Retry(f"Transcription paused: {exc}") from exc
        except AIError as exc:
            raise Retry(f"Transcription unavailable: {exc}") from exc
        words = [{"word": w.word, "start": round(w.start, 3), "end": round(w.end, 3)}
                 for s in result.segments for w in s.words]
        return words, result.language

    def _judge(self, path, seg, text, words, language):
        """(score, judged). Retry on quota; (None, False) if no AI is set up."""
        import llm
        from ai import gateway as gateway_mod
        from ai.base import GENERATE
        gw = gateway_mod.get()
        settings = gw.settings()
        route = (settings.get("routes") or {}).get(GENERATE) or {}
        if not route.get("provider"):
            return None, False
        provider = (llm.get_provider("ollama", route.get("model")) if route["provider"] == "local"
                    else llm.get_provider("gateway", f"{route['provider']}:{route['model']}"))
        ok, reason = provider.available()
        if not ok:
            if gw.cooldown.get(route["provider"], 0) > time.time() or "rate limit" in reason:
                raise Retry(f"AI judge paused: {reason}")
            return None, False
        score = highlights.judge_text(path, provider, seg, text, words, language or "")
        if score is None and gw.cooldown.get(route["provider"], 0) > time.time():
            raise Retry("AI judge paused: rate limit")
        return score, score is not None

    def _render(self, clipper, seg, words):
        import render_clips
        work = clipper.path / "live" / "work"
        file, offset = clipper.session.ring.extract(max(0.0, seg["start"] - 1), seg["end"] + 1,
                                                    work / f"{seg['id']}-src.ts")
        try:
            # A seekable copy (MPEG-TS joins are not reliably seekable).
            source = work / f"{seg['id']}-src.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(file), "-c", "copy",
                            str(source)], check=True, capture_output=True)
            file_start = max(0.0, seg["start"] - 1) - offset
            W, H = render_clips.ffprobe_dims(source)
            out = render_clips.render(seg, words, source, W, H, work, ASPECT,
                                      out_dir=clipper.path / "clips", source_offset=file_start)
        finally:
            file.unlink(missing_ok=True)
            (work / f"{seg['id']}-src.mp4").unlink(missing_ok=True)
        return out
