"""Deterministic simulated AI provider for load and recovery testing only.

Registered only when ``CHOPIFY_AI_SIMULATE=1`` is set in the environment (the
Phase 9 stress harness does this for its isolated Studio). Nothing leaves the
computer; every call is free. Transcription returns a fixed script spread
over the audio's duration with word timestamps; generation fills the given
JSON schema with scores derived from a hash of the input, so the same clip
always gets the same verdict (about half are kept). ``CHOPIFY_AI_SIMULATE_LATENCY``
(seconds, default 5) makes every call slow, to exercise queueing.
"""
import hashlib
import json
import os
import time

from ai import media
from ai.base import (GENERATE, TRANSCRIBE, Generation, ModelInfo, Provider, Segment,
                     Transcription, Usage, Word)

SCRIPT = ("Okay, we push left now. Wait, wait, watch this jump! Oh my god, that was insane! "
          "Did you see that clutch? Four kills in ten seconds. Let's go again.").split()


def enabled():
    return os.environ.get("CHOPIFY_AI_SIMULATE", "").strip() == "1"


def latency():
    try:
        return max(0.0, float(os.environ.get("CHOPIFY_AI_SIMULATE_LATENCY", "5")))
    except ValueError:
        return 5.0


class SimulatedProvider(Provider):
    name = "simulated"
    title = "Simulated AI (tests)"
    needs_key = False
    terms_url = ""

    def catalogue(self):
        return {"whisper": ModelInfo("whisper", (TRANSCRIBE,), free=True),
                "judge": ModelInfo("judge", (GENERATE,), strict_json=True, free=True)}

    def real_media_allowed(self, model_id=None):
        return True, ""                     # nothing is sent anywhere

    def is_free(self, model_id):
        return True

    def transcribe(self, path, model_id, language=None, word_timestamps=True, offset=0.0):
        started = time.time()
        time.sleep(latency())
        duration = media.probe_duration(path)
        words, t, i = [], 0.3, 0
        while t < duration - 0.4:
            words.append(Word(SCRIPT[i % len(SCRIPT)], offset + t, offset + t + 0.35))
            t += 0.42
            i += 1
            if words[-1].word[-1] in ".!?":
                t += 1.5                    # a pause between sentences, like real speech
        segments, current = [], []
        for word in words:
            current.append(word)
            if len(current) == 14:
                segments.append(Segment(current[0].start, current[-1].end,
                                        " ".join(w.word for w in current), current))
                current = []
        if current:
            segments.append(Segment(current[0].start, current[-1].end,
                                    " ".join(w.word for w in current), current))
        usage = Usage(self.name, model_id, TRANSCRIBE,
                      latency_ms=int((time.time() - started) * 1000),
                      audio_seconds=duration, cost_usd=0.0, free=True)
        return Transcription(segments, "en", duration, 1.0, usage)

    def generate(self, model_id, system, user, schema=None, images=None):
        started = time.time()
        time.sleep(latency())
        digest = int(hashlib.sha256((user or "").encode("utf-8")).hexdigest(), 16)
        keep = digest % 2 == 0

        def fill(node, depth=0):
            kind = (node or {}).get("type")
            if kind == "object":
                return {k: fill(v, depth + 1) for k, v in (node.get("properties") or {}).items()}
            if kind == "array":
                return [fill(node.get("items") or {"type": "string"}, depth + 1)]
            if kind in ("integer", "number"):
                return 8 if keep else 3
            if kind == "boolean":
                return keep
            if node.get("enum"):
                values = node["enum"]
                return ("keep" if keep else "drop") if {"keep", "drop"} <= set(values) \
                    else values[0]
            return "simulated"
        data = fill(schema) if schema else {"text": "simulated"}
        if isinstance(data, dict) and schema and "verdict" in (schema.get("properties") or {}):
            data["verdict"] = "keep" if keep else "drop"
        text = json.dumps(data)
        usage = Usage(self.name, model_id, GENERATE,
                      latency_ms=int((time.time() - started) * 1000), input_tokens=len(user) // 4,
                      output_tokens=len(text) // 4, cost_usd=0.0, free=True)
        return Generation(text, data if schema else None, usage)
