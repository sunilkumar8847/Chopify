"""Local models behind the gateway interface: faster-whisper and Ollama.

Nothing new is installed or loaded here: this wraps the existing code paths
(download_and_transcribe._load_model / _transcribe_media and llm.OllamaProvider)
so a local route behaves exactly like Chopify did before Phase 1. Local models
are optional; they are used when a route points at them, or as a fallback only
when the user turns local fallback on.
"""
import time

from ai.base import (GENERATE, TRANSCRIBE, Generation, InvalidOutput, ModelInfo, Provider,
                     Segment, TemporaryError, Transcription, Usage, Word)

WHISPER_SIZES = ("tiny", "base", "small", "medium", "large-v3")


class LocalProvider(Provider):
    name = "local"
    title = "Local (this computer)"
    needs_key = False

    @classmethod
    def catalogue_supports(cls, capability, model_id):
        if capability == TRANSCRIBE:
            return model_id in WHISPER_SIZES
        return capability == GENERATE and bool(model_id)

    def catalogue(self):
        return {m: ModelInfo(m, (TRANSCRIBE,), price_per_audio_hour=0.0, free=True)
                for m in WHISPER_SIZES}

    def supports(self, capability, model_id):
        return self.catalogue_supports(capability, model_id)

    def model(self, model_id):
        return self.models.get(model_id) or ModelInfo(model_id, (GENERATE,), 0.0, 0.0, free=True)

    def is_free(self, model_id):
        return True

    def real_media_allowed(self, model_id=None):
        return True, ""

    def health(self):
        import llm
        ok, reason = llm.OllamaProvider(self.settings.get("text_model") or "qwen2.5:3b").available()
        return {"ok": ok, "detail": reason or "Ollama reachable; Whisper runs on demand"}

    def transcribe(self, path, model_id, language=None, word_timestamps=True, offset=0.0):
        import download_and_transcribe as dt
        started = time.time()
        device, compute, _warning = dt.resolve_device(self.settings.get("device") or "auto")
        model = dt._load_model(model_id, device, compute)
        segs, words, detected, duration = dt._transcribe_media(model, path, offset=offset)
        segments = []
        for seg in segs:
            inside = [Word(w["word"], w["start"], w["end"]) for w in words
                      if seg["start"] - 0.01 <= w["start"] < seg["end"] + 0.01]
            segments.append(Segment(seg["start"], seg["end"], seg["text"], inside))
        usage = Usage(self.name, model_id, TRANSCRIBE, int((time.time() - started) * 1000),
                      audio_seconds=duration, free=True)
        return Transcription(segments, detected, duration, 1.0, usage)

    def generate(self, model_id, system, user, schema=None, images=None, **hints):
        import llm
        if images:
            from ai.base import Unsupported
            raise Unsupported("The local text model cannot read images.", self.name)
        provider = llm.OllamaProvider(model_id)
        ok, reason = provider.available()
        if not ok:
            raise TemporaryError(reason, self.name)
        started = time.time()
        try:
            data = provider.chat_json(system, user, schema)
        except llm.LLMError as exc:
            raise InvalidOutput(str(exc), self.name) from exc
        import json
        usage = Usage(self.name, model_id, GENERATE, int((time.time() - started) * 1000),
                      free=True)
        return Generation(json.dumps(data, ensure_ascii=False), data, usage)
