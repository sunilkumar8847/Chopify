"""Groq (OpenAI-compatible API): Whisper transcription and gpt-oss text.

Verified 2026-11-22 against console.groq.com/docs (speech-to-text, models,
structured-outputs, rate-limits, your-data):
* POST /openai/v1/audio/transcriptions, multipart; response_format=verbose_json
  with timestamp_granularities[]=word,segment for word timestamps.
* Files: 25 MB on the free tier (100 MB developer); FLAC accepted; minimum
  billed length 10 s. whisper-large-v3 $0.111/h, whisper-large-v3-turbo $0.04/h.
* gpt-oss-20b $0.075/$0.30 and gpt-oss-120b $0.15/$0.60 per 1M tokens, strict
  JSON-schema structured outputs. qwen/qwen3.8-27b (preview) accepts images,
  $0.80/$4.00.
* No training on inputs/outputs (Services Agreement 4.2). Zero Data Retention
  is a console setting (Data Controls); no API reports it, so real recordings
  need the user's explicit confirmation that it is on.
"""
import base64
import json
import time

from ai import http as aihttp
from ai import media
from ai.base import (GENERATE, TRANSCRIBE, VISION, Generation, InvalidOutput, ModelInfo,
                     Provider, Segment, Transcription, Usage, Word)

API = "https://api.groq.com/openai/v1"
# Upload limits per file (console.groq.com/docs/speech-to-text): free plan 25 MB,
# developer plan 100 MB. Chopify uses the free-plan limit for every plan.
UPLOAD_LIMIT = media.MAX_PIECE_BYTES
PRICES = "https://console.groq.com/docs/models"
LANGUAGES = {"english": "en", "hindi": "hi", "urdu": "ur", "bengali": "bn", "marathi": "mr",
             "tamil": "ta", "telugu": "te", "punjabi": "pa", "gujarati": "gu", "nepali": "ne",
             "spanish": "es", "french": "fr", "german": "de", "japanese": "ja", "korean": "ko",
             "chinese": "zh", "russian": "ru", "portuguese": "pt", "arabic": "ar"}


class GroqProvider(Provider):
    name = "groq"
    title = "Groq"
    key_env = "GROQ_API_KEY"
    terms_url = "https://console.groq.com/docs/your-data"

    def catalogue(self):
        verified = "2026-11-22"
        return {
            "whisper-large-v3-turbo": ModelInfo("whisper-large-v3-turbo", (TRANSCRIBE,),
                                                price_per_audio_hour=0.04, min_billed_seconds=10,
                                                source=PRICES, verified=verified),
            "whisper-large-v3": ModelInfo("whisper-large-v3", (TRANSCRIBE,),
                                          price_per_audio_hour=0.111, min_billed_seconds=10,
                                          source=PRICES, verified=verified),
            "openai/gpt-oss-20b": ModelInfo("openai/gpt-oss-20b", (GENERATE,), 0.075, 0.30,
                                            strict_json=True, source=PRICES, verified=verified),
            "openai/gpt-oss-120b": ModelInfo("openai/gpt-oss-120b", (GENERATE,), 0.15, 0.60,
                                             strict_json=True, source=PRICES, verified=verified),
            "qwen/qwen3.8-27b": ModelInfo("qwen/qwen3.8-27b", (GENERATE, VISION), 0.80, 4.00,
                                          strict_json=True, source=PRICES, verified=verified),
        }

    # ------------------------------------------------------------ policy
    def real_media_allowed(self, model_id=None):
        if self.settings.get("zdr_confirmed_at"):
            return True, ""
        return False, ("Groq has not been confirmed for real recordings: turn on Zero Data "
                       "Retention in Groq Console > Settings > Data Controls, then confirm it "
                       "in Chopify's AI settings. Groq has no API that reports this setting, "
                       "so Chopify cannot check it for you.")

    def is_free(self, model_id):
        billing = self.settings.get("billing", "unknown")
        return True if billing == "free_plan" else (False if billing == "paid" else None)

    # ------------------------------------------------------------ http
    def _headers(self, extra=None):
        return {"Authorization": f"Bearer {self.key}", **(extra or {})}

    def health(self):
        _, _, body = self._request("GET", f"{API}/models", timeout=20)
        ids = {m.get("id") for m in aihttp.json_body(self.name, body).get("data") or []}
        missing = [m for m in self.models if m not in ids]
        return {"ok": not missing or len(missing) < len(self.models),
                "detail": "all catalogue models listed" if not missing else
                          f"not listed by Groq: {', '.join(missing)}"}

    # ------------------------------------------------------ transcription
    def transcribe(self, path, model_id, language=None, word_timestamps=True, offset=0.0):
        self.model(model_id)
        started = time.time()
        segments, total, billed = [], 0.0, 0.0
        detected, probability = "", 1.0
        limit = UPLOAD_LIMIT
        with media.audio_pieces(path, max_bytes=limit) as pieces:
            for piece_offset, piece, seconds in pieces:
                content = piece.read_bytes()
                if len(content) > limit:        # never assume: check what is sent
                    raise media.PieceTooLarge(f"{piece.name} is {len(content) / 1e6:.1f} MB, "
                                              f"over Groq's upload limit. Nothing was sent.", self.name)
                fields = {"model": model_id, "response_format": "verbose_json",
                          "temperature": "0"}
                if word_timestamps:
                    fields["timestamp_granularities[]"] = ["word", "segment"]
                if language:
                    fields["language"] = language
                content_type, body = aihttp.multipart(
                    fields, {"file": (piece.name, content, "audio/flac")})
                _, _, data = self._request("POST", f"{API}/audio/transcriptions",
                                           {"Content-Type": content_type}, body, timeout=300)
                result = aihttp.json_body(self.name, data)
                segments.extend(normalize_verbose(result, offset + piece_offset))
                detected = detected or LANGUAGES.get(str(result.get("language", "")).lower(),
                                                      str(result.get("language") or "")[:5])
                total += seconds
                billed += max(seconds, self.models[model_id].min_billed_seconds)
        info = self.models[model_id]
        free = self.is_free(model_id)
        usage = Usage(self.name, model_id, TRANSCRIBE,
                      latency_ms=int((time.time() - started) * 1000), audio_seconds=total,
                      cost_usd=0.0 if free else info.price_per_audio_hour * billed / 3600.0,
                      free=bool(free))
        return Transcription(segments, detected, total, probability, usage)

    # --------------------------------------------------------- generation
    def generate(self, model_id, system, user, schema=None, images=None):
        info = self.model(model_id)
        content = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}}
                for img in images[:3]]
        payload = {"model": model_id, "temperature": 0,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": content}]}
        if schema is not None:
            if info.strict_json and strict_compatible(schema):
                payload["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "chopify", "strict": True, "schema": schema}}
            else:
                payload["response_format"] = {"type": "json_object"}
                payload["messages"][0]["content"] = (
                    system + "\nAnswer with one JSON object matching this schema:\n"
                    + json.dumps(schema))
        started = time.time()
        _, _, body = self._request("POST", f"{API}/chat/completions",
                                   {"Content-Type": "application/json"},
                                   json.dumps(payload).encode("utf-8"))
        return chat_result(self, model_id, body, started, schema is not None, len(images or []))


def chat_result(provider, model_id, body, started, want_json, images=0, served_by=""):
    """Normalize an OpenAI-style chat completion."""
    import llm
    data = aihttp.json_body(provider.name, body)
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise InvalidOutput(f"{provider.title} returned no answer.", provider.name) from exc
    usage_data = data.get("usage") or {}
    tokens_in = int(usage_data.get("prompt_tokens") or 0)
    tokens_out = int(usage_data.get("completion_tokens") or 0)
    free = provider.is_free(model_id)
    cost = usage_data.get("cost")
    if cost is None:
        estimate = provider.estimate_cost(GENERATE, model_id, input_tokens=tokens_in,
                                          output_tokens=tokens_out)
        cost = 0.0 if free else (estimate or 0.0)
    usage = Usage(provider.name, model_id, VISION if images else GENERATE,
                  latency_ms=int((time.time() - started) * 1000), input_tokens=tokens_in,
                  output_tokens=tokens_out, images=images, cost_usd=float(cost),
                  free=bool(free), served_by=served_by)
    parsed = None
    if want_json:
        try:
            parsed = llm.parse_json(text)
        except llm.LLMError as exc:
            raise InvalidOutput(f"{provider.title} did not return valid JSON.",
                                provider.name) from exc
    return Generation(text, parsed, usage)


def strict_compatible(schema):
    """Groq strict mode needs every property required and no extra properties."""
    if not isinstance(schema, dict):
        return False
    if schema.get("type") == "object":
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is not False:
            return False
        if set(schema.get("required") or []) != set(props):
            return False
        return all(strict_compatible(v) for v in props.values())
    if schema.get("type") == "array":
        return strict_compatible(schema.get("items") or {"type": "string"})
    return True


def normalize_verbose(result, offset):
    """OpenAI verbose_json -> [Segment] on the source timeline."""
    words = [Word(str(w.get("word", "")), float(w["start"]) + offset, float(w["end"]) + offset)
             for w in result.get("words") or [] if "start" in w and "end" in w]
    segments = []
    for seg in result.get("segments") or []:
        start, end = float(seg.get("start", 0)) + offset, float(seg.get("end", 0)) + offset
        inside = [w for w in words if start - 0.01 <= w.start < end + 0.01]
        segments.append(Segment(start, end, str(seg.get("text", "")), inside))
    if not segments and (result.get("text") or words):
        start = words[0].start if words else offset
        end = words[-1].end if words else offset + float(result.get("duration") or 0)
        segments.append(Segment(start, end, str(result.get("text", "")), words))
    # words that fell between segments still belong to the nearest one
    assigned = {id(w) for s in segments for w in s.words}
    for w in words:
        if id(w) not in assigned and segments:
            nearest = min(segments, key=lambda s: abs(s.start - w.start))
            nearest.words.append(w)
            nearest.words.sort(key=lambda x: x.start)
    return segments
