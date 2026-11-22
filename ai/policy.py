"""AI settings and the checks every request passes before it is sent.

Defaults (free and private unless the user says otherwise):

    free_only = True                 unknown pricing counts as paid
    daily_paid_budget_usd = 0
    monthly_paid_budget_usd = 0
    paid_calls = False
    automatic_paid_fallback = False  (never implemented as "on")
    local_fallback = False           explicit opt-in

Routes choose a provider and model per capability. The default routes are
the existing local models (faster-whisper medium, Ollama qwen2.5:3b), so
nothing changes until the user configures a cloud provider.

Order of checks for one request (see ``check``):
1. the provider is enabled; 2. the model is in its allow-list and offers
the capability; 3. the data kind is allowed by the provider's privacy rules;
4. free/paid status against free-only mode and paid approval;
5. the estimated cost fits the daily and monthly budgets.
Quota/rate-limit state is checked by the gateway right before sending.
"""
import copy
import json
import os
import threading
import time

import youtube_auth
from ai.base import (GENERATE, REAL, TRANSCRIBE, VISION, BudgetExceeded, PolicyBlocked,
                     PrivacyBlocked, Unsupported)

SETTINGS_FILE = "ai_settings.json"

DEFAULTS = {
    "version": 1,
    "free_only": True,
    "paid_calls": False,
    "daily_paid_budget_usd": 0.0,
    "monthly_paid_budget_usd": 0.0,
    "automatic_paid_fallback": False,
    "local_fallback": False,
    "max_retries": 3,
    "timeout_seconds": 120,
    "max_rate_limit_wait_seconds": 60,
    "routes": {
        TRANSCRIBE: {"provider": "local", "model": "medium"},
        GENERATE: {"provider": "local", "model": "qwen2.5:3b"},
        VISION: {"provider": None, "model": None},
    },
    "providers": {
        "local": {"enabled": True, "models": []},
        "groq": {"enabled": False, "models": ["whisper-large-v3-turbo", "whisper-large-v3",
                                              "openai/gpt-oss-20b", "openai/gpt-oss-120b"],
                 # "free_plan": Groq's free plan (no charges); "paid": developer plan.
                 "billing": "unknown",
                 # Groq offers ZDR in console Data Controls but no API reports it;
                 # real recordings need the user's explicit confirmation.
                 "zdr_confirmed_at": None},
        "openrouter": {"enabled": False, "models": []},
        "gemini": {"enabled": False, "models": [], "tier": "free",
                   "real_media_approved_at": None},
        "nvidia": {"enabled": False, "models": []},
    },
}
_lock = threading.Lock()


def _path():
    return youtube_auth.config_dir() / SETTINGS_FILE


def _merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load():
    try:
        stored = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    return _merge(DEFAULTS, stored if isinstance(stored, dict) else {})


def save(settings):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, indent=1), encoding="utf-8")
    os.replace(temp, path)


def update(changes, registry):
    """Validate and store user changes; returns the new settings."""
    with _lock:
        settings = load()
        new = _merge(settings, changes or {})
        validate(new, registry)
        save(new)
        return new


def validate(s, registry):
    for flag in ("free_only", "paid_calls", "local_fallback"):
        if not isinstance(s.get(flag), bool):
            raise ValueError(f"{flag} must be true or false.")
    if s.get("automatic_paid_fallback") is not False:
        raise ValueError("Automatic paid fallback cannot be turned on.")
    for name in ("daily_paid_budget_usd", "monthly_paid_budget_usd"):
        value = s.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10000:
            raise ValueError(f"{name} must be between 0 and 10000.")
    for name, low, high in (("max_retries", 0, 6), ("timeout_seconds", 5, 900),
                            ("max_rate_limit_wait_seconds", 0, 600)):
        value = s.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high}.")
    for capability, route in (s.get("routes") or {}).items():
        if capability not in (TRANSCRIBE, GENERATE, VISION):
            raise ValueError(f"Unknown capability {capability}.")
        provider, model = route.get("provider"), route.get("model")
        if provider is None:
            continue
        if provider not in registry:
            raise ValueError(f"Unknown provider {provider}.")
        if not registry[provider].catalogue_supports(capability, model):
            raise ValueError(f"{provider} model '{model}' cannot {capability}.")
    for name, conf in (s.get("providers") or {}).items():
        if name not in registry:
            raise ValueError(f"Unknown provider {name}.")
        if not isinstance(conf.get("enabled"), bool):
            raise ValueError(f"{name}.enabled must be true or false.")
    groq = s["providers"].get("groq", {})
    if groq.get("billing") not in {"unknown", "free_plan", "paid"}:
        raise ValueError("Groq billing must be unknown, free_plan or paid.")
    gemini = s["providers"].get("gemini", {})
    if gemini.get("tier") not in {"free", "paid"}:
        raise ValueError("Gemini tier must be free or paid.")


def confirm(settings, provider, what, registry):
    """Record an explicit user confirmation (Groq ZDR, Gemini paid real media)."""
    field = {("groq", "zdr"): "zdr_confirmed_at",
             ("gemini", "real_media"): "real_media_approved_at"}.get((provider, what))
    if field is None:
        raise ValueError("Nothing to confirm for that provider.")
    return update({"providers": {provider: {field: time.time()}}}, registry)


def revoke(settings, provider, what, registry):
    field = {("groq", "zdr"): "zdr_confirmed_at",
             ("gemini", "real_media"): "real_media_approved_at"}.get((provider, what))
    if field is None:
        raise ValueError("Nothing to revoke for that provider.")
    return update({"providers": {provider: {field: None}}}, registry)


# --------------------------------------------------------------- checks

def check(settings, adapter, capability, model, data_kind, estimate, ledger):
    """Raise a PolicyBlocked/Unsupported error unless the request may be sent.

    Returns (free: bool, estimate_usd: float | None).
    """
    conf = (settings.get("providers") or {}).get(adapter.name) or {}
    if not conf.get("enabled"):
        raise PolicyBlocked(f"{adapter.title} is turned off in AI settings.", adapter.name)
    allowed = conf.get("models") or []
    if adapter.name != "local" and allowed and model not in allowed:
        raise PolicyBlocked(f"{adapter.title} model '{model}' is not in the allowed list.",
                            adapter.name)
    if not adapter.supports(capability, model):
        raise Unsupported(f"{adapter.title} model '{model}' does not offer {capability}.",
                          adapter.name)
    if data_kind == REAL:
        ok, reason = adapter.real_media_allowed(model)
        if not ok:
            raise PrivacyBlocked(reason, adapter.name)
    free = adapter.is_free(model)
    if free:
        return True, 0.0
    if settings.get("free_only", True):
        what = "unknown pricing (treated as paid)" if free is None else "a paid model"
        raise PolicyBlocked(f"Free-only mode is on and {adapter.title} '{model}' is {what}. "
                            "Nothing was sent.", adapter.name)
    if not settings.get("paid_calls"):
        raise PolicyBlocked("Paid AI calls are turned off in AI settings.", adapter.name)
    if estimate is None:
        raise BudgetExceeded(f"{adapter.title} '{model}' has no verified price, so its cost "
                             "cannot be checked against the budget.", adapter.name)
    for period, limit_key in (("day", "daily_paid_budget_usd"),
                              ("month", "monthly_paid_budget_usd")):
        limit = float(settings.get(limit_key) or 0)
        spent = ledger.spent(period) if ledger else 0.0
        if spent + estimate > limit + 1e-9:
            raise BudgetExceeded(
                f"The {'daily' if period == 'day' else 'monthly'} AI budget "
                f"(${limit:.2f}) would be exceeded: spent ${spent:.4f}, this request "
                f"about ${estimate:.4f}.", adapter.name)
    return False, estimate
