"""Gateway-backed stand-ins for the objects existing Chopify code expects.

``CloudWhisperModel`` answers ``transcribe(path, **options)`` exactly like a
faster-whisper ``WhisperModel`` (an iterator of segments with ``words`` and an
info object), so chunking, checkpoints, caption checks and clip refinement in
download_and_transcribe.py / transcription.py work unchanged with a cloud
route. Decoding options (beam size, VAD) are local-only and ignored here.
"""
from types import SimpleNamespace

from ai.base import REAL, AIError


class CloudTranscriptionError(RuntimeError):
    """A cloud transcription failed or was blocked; the message says why."""


class CloudWhisperModel:
    def __init__(self, route, gateway=None):
        self.route = route                    # "groq:whisper-large-v3-turbo"
        self._gateway = gateway

    @property
    def gateway(self):
        if self._gateway is None:
            from ai import gateway
            self._gateway = gateway.get()
        return self._gateway

    def transcribe(self, path, language=None, word_timestamps=True, **_ignored):
        try:
            result = self.gateway.transcribe(str(path), language_hint=language,
                                             word_timestamps=word_timestamps, data_kind=REAL,
                                             route=self.route)
        except AIError as exc:
            raise CloudTranscriptionError(f"Cloud transcription ({self.route}) stopped: "
                                          f"{exc}") from exc
        info = SimpleNamespace(language=result.language,
                               language_probability=result.language_probability,
                               duration=result.duration)
        return iter(result.segments), info


def is_cloud_route(value):
    provider, sep, model = str(value or "").partition(":")
    return bool(sep and provider and provider != "local" and model)
