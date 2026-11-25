"""Common interface, normalized results and errors for every AI provider."""
import time
import uuid
from dataclasses import dataclass, field

TRANSCRIBE = "transcribe"
GENERATE = "generate"
VISION = "vision"
CAPABILITIES = (TRANSCRIBE, GENERATE, VISION)

# What is being sent. "real" = anything from the user's own recordings or
# projects (audio, frames, transcript text). "synthetic" = test material made
# for evaluation only. Providers whose terms allow content reuse only ever get
# synthetic data.
REAL = "real"
SYNTHETIC = "synthetic"


# ------------------------------------------------------------------ errors

class AIError(RuntimeError):
    """Base class. ``kind`` is a stable, user-facing category."""
    kind = "error"
    retryable = False

    def __init__(self, message, provider=None, status=None):
        super().__init__(message)
        self.provider = provider
        self.status = status


class AuthFailed(AIError):
    kind = "auth"                 # missing/invalid key: never retried


class PermanentError(AIError):
    kind = "invalid_request"      # 400/404/422: never retried


class TemporaryError(AIError):
    kind = "temporary"            # 5xx, timeouts, connection resets
    retryable = True


class RateLimited(AIError):
    kind = "rate_limited"         # 429 (Retry-After honoured)
    retryable = True

    def __init__(self, message, provider=None, retry_after=None, status=429):
        super().__init__(message, provider, status)
        self.retry_after = retry_after


class QuotaExhausted(AIError):
    kind = "quota"                # waiting longer than allowed: reported, not retried


class Unsupported(AIError):
    kind = "unsupported"          # capability/model not offered


class PolicyBlocked(AIError):
    kind = "policy"               # disabled provider, privacy, free-only, budget


class PrivacyBlocked(PolicyBlocked):
    kind = "privacy"


class BudgetExceeded(PolicyBlocked):
    kind = "budget"


class InvalidOutput(AIError):
    kind = "invalid_output"       # malformed JSON / schema mismatch


# ----------------------------------------------------------------- results

@dataclass
class Usage:
    provider: str
    model: str
    capability: str
    latency_ms: int = 0
    audio_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0
    cost_usd: float = 0.0
    free: bool = True
    served_by: str = ""           # e.g. the OpenRouter upstream provider


@dataclass
class Word:
    word: str
    start: float
    end: float
    probability: float = 1.0


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list = field(default_factory=list)


@dataclass
class Transcription:
    segments: list
    language: str
    duration: float
    language_probability: float = 1.0
    usage: Usage = None


@dataclass
class Generation:
    text: str
    data: object = None           # parsed JSON when a schema/JSON was requested
    usage: Usage = None


def request_id():
    return uuid.uuid4().hex[:12]


class Clock:
    """Injectable time source (tests replace sleep)."""
    now = staticmethod(time.time)
    sleep = staticmethod(time.sleep)


# ---------------------------------------------------------------- provider

@dataclass
class ModelInfo:
    id: str
    capabilities: tuple
    # USD; None = unknown (treated as paid/unverified and blocked in free-only)
    price_input_per_mtok: float = None
    price_output_per_mtok: float = None
    price_per_audio_hour: float = None
    min_billed_seconds: float = 0.0
    strict_json: bool = False      # documented strict JSON-schema support
    free: bool = False             # documented $0 model (e.g. OpenRouter ":free")
    source: str = ""               # pricing source URL
    verified: str = ""             # date the price/capability was checked


class Provider:
    """One AI service. Subclasses fill ``models`` and implement capabilities."""
    name = "base"
    title = "Provider"
    key_env = None
    needs_key = True             # False: local or simulated, no API key
    # Privacy rules come from the provider's own terms (see docs/AI.md):
    # "real" data is only sent when real_media_allowed() says so.
    terms_url = ""

    def __init__(self, settings=None, key=None, http=None, clock=Clock):
        self.settings = settings or {}
        self.key = key
        self.http = http
        self.clock = clock
        self.send = None             # set by the gateway (retries, rate limits)
        self.models = self.catalogue()

    def catalogue(self):
        """{model_id: ModelInfo} this adapter knows and has verified."""
        return {}

    @classmethod
    def catalogue_supports(cls, capability, model_id):
        """Settings validation: could this model serve the capability?"""
        return cls().supports(capability, model_id)

    def _request(self, method, url, headers=None, body=None, timeout=120):
        from ai import http as aihttp
        sender = self.send or (lambda *a: aihttp.request(self.name, *a, http=self.http))
        return sender(method, url, self._headers(headers), body, timeout)

    def _headers(self, extra=None):
        return dict(extra or {})

    # -------------------------------------------------------- catalogue
    def model(self, model_id):
        info = self.models.get(model_id)
        if info is None:
            raise Unsupported(f"{self.title} has no model '{model_id}' in Chopify's "
                              "verified catalogue.", self.name)
        return info

    def supports(self, capability, model_id):
        info = self.models.get(model_id)
        return bool(info and capability in info.capabilities)

    # ---------------------------------------------------------- policy
    def real_media_allowed(self, model_id=None):
        """(allowed, reason) for sending the user's real recordings/projects."""
        return False, f"{self.title} is not approved for real recordings."

    def is_free(self, model_id):
        """True when a call costs nothing; None when unknown."""
        return None

    def estimate_cost(self, capability, model_id, audio_seconds=0, input_tokens=0,
                      output_tokens=0, images=0):
        info = self.model(model_id)
        if self.is_free(model_id):
            return 0.0
        if capability == TRANSCRIBE:
            if info.price_per_audio_hour is None:
                return None
            billed = max(float(audio_seconds), info.min_billed_seconds)
            return info.price_per_audio_hour * billed / 3600.0
        if info.price_input_per_mtok is None or info.price_output_per_mtok is None:
            return None
        return (info.price_input_per_mtok * input_tokens
                + info.price_output_per_mtok * output_tokens) / 1e6

    # ----------------------------------------------------- capabilities
    def health(self):
        raise Unsupported(f"{self.title} has no health check.", self.name)

    def transcribe(self, path, model_id, language=None, word_timestamps=True, offset=0.0):
        raise Unsupported(f"{self.title} does not transcribe audio.", self.name)

    def generate(self, model_id, system, user, schema=None, images=None, **hints):
        raise Unsupported(f"{self.title} does not generate text.", self.name)
