"""The AI Gateway: one entry point for transcription, text and vision.

    gateway.transcribe(path, language_hint=None, word_timestamps=True)
    gateway.generate(system_prompt, user_prompt, schema=None)
    gateway.analyze_images(images, prompt, schema=None)

Each call: route (settings) -> adapter -> policy.check (enabled, allow-list,
capability, privacy for the data kind, free-only, paid approval, budgets) ->
quota cooldown check -> request with retries (exponential backoff, Retry-After
for 429, never for auth/permission/invalid requests) -> usage ledger.

Fallback: only to local models, only when the user turned local fallback on,
and only for outages (temporary errors, rate limits, quota). There is no
automatic switch to another cloud provider and never to a paid one.
"""
import contextlib
import functools
import random
import threading
import time
from pathlib import Path

import applog
from ai import http as aihttp
from ai import policy, secrets
from ai.base import (GENERATE, REAL, TRANSCRIBE, VISION, AIError, AuthFailed, Clock,
                     QuotaExhausted, RateLimited, TemporaryError, request_id)
from ai.registry import REGISTRY
from ai.usage import Ledger

PER_PROVIDER_CONCURRENCY = 2
OUTAGES = (TemporaryError, RateLimited, QuotaExhausted)


class Cancelled(AIError):
    kind = "cancelled"


class Gateway:
    def __init__(self, ledger=None, http=None, clock=Clock, keys=None, settings=None):
        self._ledger = ledger
        self.http = http
        self.clock = clock
        self.keys = keys or secrets.get
        self._settings = settings                   # tests inject settings
        self.cooldown = {}                          # provider -> retry not before (epoch)
        self.limits = {}                            # provider -> last x-ratelimit-* values
        self.slots = {name: threading.BoundedSemaphore(PER_PROVIDER_CONCURRENCY)
                      for name in REGISTRY}
        self.cancelled = threading.Event()

    @property
    def ledger(self):
        if self._ledger is None:
            self._ledger = Ledger()
        return self._ledger

    def settings(self):
        return self._settings if self._settings is not None else policy.load()

    # --------------------------------------------------------------- setup
    def adapter(self, name, settings=None):
        settings = settings or self.settings()
        if name not in REGISTRY:
            raise policy.PolicyBlocked(f"Unknown AI provider '{name}'.")
        conf = dict((settings.get("providers") or {}).get(name) or {})
        adapter = REGISTRY[name](conf, None if not REGISTRY[name].needs_key else self.keys(name),
                                 self.http, self.clock)
        adapter.send = functools.partial(self._send, adapter, settings)
        return adapter

    def route(self, capability, route=None, settings=None):
        settings = settings or self.settings()
        if route:
            provider, _, model = str(route).partition(":")
            return provider, model
        chosen = (settings.get("routes") or {}).get(capability) or {}
        if not chosen.get("provider"):
            raise policy.PolicyBlocked(f"No AI provider is configured for {capability}.")
        return chosen["provider"], chosen.get("model")

    # ---------------------------------------------------------- the calls
    def transcribe(self, audio, language_hint=None, word_timestamps=True, data_kind=REAL,
                   route=None, project_id=None, offset=0.0):
        from ai import media
        try:
            seconds = media.probe_duration(audio)
        except Exception:                           # noqa: BLE001 - estimate only
            seconds = 0.0

        def call(adapter, model):
            return adapter.transcribe(audio, model, language=language_hint,
                                      word_timestamps=word_timestamps, offset=offset)
        return self._run(TRANSCRIBE, route, data_kind, call, {"audio_seconds": seconds},
                         project_id)

    def generate(self, system_prompt, user_prompt, schema=None, data_kind=REAL, route=None,
                 project_id=None):
        tokens_in = (len(system_prompt) + len(user_prompt)) // 3 + 50

        def call(adapter, model):
            return adapter.generate(model, system_prompt, user_prompt, schema=schema)
        return self._run(GENERATE, route, data_kind, call,
                         {"input_tokens": tokens_in, "output_tokens": 1024}, project_id)

    def analyze_images(self, images, prompt, schema=None, system_prompt="You describe video "
                       "frames precisely.", data_kind=REAL, route=None, project_id=None):
        from ai import media
        frames = [img if isinstance(img, (bytes, bytearray)) else media.jpeg_frame(img)
                  for img in images]

        def call(adapter, model):
            return adapter.generate(model, system_prompt, prompt, schema=schema, images=frames)
        return self._run(VISION, route, data_kind, call,
                         {"input_tokens": 1500 * len(frames) + len(prompt) // 3,
                          "output_tokens": 1024, "images": len(frames)}, project_id)

    def precheck(self, capability, route=None, data_kind=REAL):
        """(ok, reason) without sending anything (used for 'available')."""
        try:
            settings = self.settings()
            name, model = self.route(capability, route, settings)
            adapter = self.adapter(name, settings)
            if adapter.needs_key and not adapter.key:
                return False, f"No API key is configured for {adapter.title}."
            policy.check(settings, adapter, capability, model, data_kind,
                         adapter.estimate_cost(capability, model, input_tokens=2000,
                                               output_tokens=1024) if capability != TRANSCRIBE
                         else adapter.estimate_cost(capability, model, audio_seconds=60),
                         self.ledger)
            return True, ""
        except AIError as exc:
            return False, str(exc)

    def health(self, name):
        adapter = self.adapter(name)
        if adapter.needs_key and not adapter.key:
            result = {"ok": False, "detail": "No API key configured."}
        else:
            try:
                result = adapter.health()
            except AIError as exc:
                result = {"ok": False, "detail": str(exc)}
        self.ledger.set_health(name, result["ok"], result["detail"])
        return result

    def cancel(self):
        self.cancelled.set()

    # ------------------------------------------------------------ engine
    def _run(self, capability, route, data_kind, call, sizes, project_id):
        settings = self.settings()
        name, model = self.route(capability, route, settings)
        rid = request_id()
        adapter = self.adapter(name, settings)
        try:
            if adapter.needs_key and not adapter.key:
                raise AuthFailed(f"No API key is configured for {adapter.title}.", name)
            estimate = adapter.estimate_cost(capability, model, **sizes) \
                if adapter.supports(capability, model) else None
            policy.check(settings, adapter, capability, model, data_kind, estimate, self.ledger)
            wait = self.cooldown.get(name, 0) - self.clock.now()
            if wait > 0:
                raise QuotaExhausted(f"{adapter.title}{_plan(adapter)} rate limit: wait about "
                                     f"{wait:.0f}s more. The request was not sent.", name)
        except AIError as exc:
            self.ledger.record(provider=name, model=model or "", capability=capability,
                               status=_status(exc, "blocked"), data_kind=data_kind, error=exc,
                               request_id=rid, project_id=project_id)
            applog.warning("ai.blocked", str(exc), provider=name, model=model,
                           capability=capability, request_id=rid)
            return self._fallback(exc, capability, data_kind, call, sizes, project_id, settings,
                                  name)
        try:
            with applog.step("ai.request", f"{capability} via {name}:{model}",
                             provider=name, model=model, ai_request=rid):
                result = call(adapter, model)
        except AIError as exc:
            self.ledger.record(provider=name, model=model, capability=capability,
                               status=_status(exc, "error"), data_kind=data_kind, error=exc,
                               request_id=rid, project_id=project_id)
            return self._fallback(exc, capability, data_kind, call, sizes, project_id, settings,
                                  name)
        self.ledger.record(result.usage, status="ok", data_kind=data_kind, request_id=rid,
                           project_id=project_id)
        return result

    def _fallback(self, exc, capability, data_kind, call, sizes, project_id, settings, name):
        if (name == "local" or not settings.get("local_fallback")
                or not isinstance(exc, OUTAGES) or capability == VISION):
            raise exc
        local = "medium" if capability == TRANSCRIBE else (
            (settings.get("routes") or {}).get(GENERATE, {}).get("model")
            if (settings.get("routes") or {}).get(GENERATE, {}).get("provider") == "local"
            else "qwen2.5:3b")
        applog.warning("ai.fallback", f"{name} unavailable ({exc.kind}); using the local "
                       f"{capability} model because local fallback is on.", provider=name)
        return self._run(capability, f"local:{local}", data_kind, call, sizes, project_id)

    def _send(self, adapter, settings, method, url, headers, body, timeout):
        """One HTTP request with retries; used by every adapter."""
        retries = int(settings.get("max_retries", 3))
        max_wait = float(settings.get("max_rate_limit_wait_seconds", 60))
        timeout = min(timeout, float(settings.get("timeout_seconds", 120)) * 3)
        last = None
        for attempt in range(retries + 1):
            if self.cancelled.is_set():
                raise Cancelled("The AI request was cancelled.", adapter.name)
            try:
                with self.slots[adapter.name], self._machine_slot(adapter, settings):
                    response = aihttp.request(adapter.name, method, url, headers, body,
                                              timeout, http=self.http)
                self._note_limits(adapter, response[1])
                return response
            except RateLimited as exc:
                last = exc
                wait = exc.retry_after if exc.retry_after is not None else 2 ** attempt
                if wait > max_wait or attempt == retries:
                    self.cooldown[adapter.name] = self.clock.now() + max(wait, 1)
                    raise QuotaExhausted(f"{adapter.title}{_plan(adapter)} rate limit reached: "
                                         f"retry in about {wait:.0f}s. The work is paused; "
                                         "nothing was sent to another provider.",
                                         adapter.name) from exc
                self.clock.sleep(wait)
            except TemporaryError as exc:
                last = exc
                if attempt == retries:
                    raise
                self.clock.sleep(min(30.0, 2 ** attempt + random.random()))
        raise last

    def _machine_slot(self, adapter, settings):
        """One of N request slots per provider for the whole computer (all of
        Studio's processes share the upload link). Tests with a fake HTTP
        client skip it."""
        if self.http is not None:
            return contextlib.nullcontext()
        from ai import slots
        return slots.slot(adapter.name, settings.get("max_concurrent_requests", 2),
                          wait=float(settings.get("timeout_seconds", 120)) * 5,
                          cancelled=self.cancelled)

    def _note_limits(self, adapter, headers):
        """Respect the account's live limits: pause before a limit is hit."""
        limits = aihttp.rate_limits(headers)
        if not limits:
            return
        self.limits[adapter.name] = {**limits, "checked": self.clock.now()}
        resets = [limits.get(f"reset_{k}_s") or 1.0 for k in ("requests", "tokens")
                  if limits.get(f"remaining_{k}") is not None and limits[f"remaining_{k}"] <= 0]
        if resets:
            self.cooldown[adapter.name] = max(self.cooldown.get(adapter.name, 0),
                                              self.clock.now() + max(resets))


def _plan(adapter):
    return " free-plan" if adapter.name == "groq" and adapter.is_free(None) else ""


def _status(exc, default):
    """Ledger status: rate/quota limits are shown separately from failures."""
    return "limited" if getattr(exc, "kind", "") in ("quota", "rate_limited") else default


_gateway = None
_gateway_lock = threading.Lock()


def get():
    """The shared gateway for this process."""
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            _gateway = Gateway()
        return _gateway


def reset():
    """Tests: forget the shared gateway."""
    global _gateway
    with _gateway_lock:
        _gateway = None
