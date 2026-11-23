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

``.env`` (via ``envconfig``) sets the spending defaults AND their ceilings:
CHOPIFY_AI_FREE_ONLY, CHOPIFY_AI_ALLOW_PAID, CHOPIFY_AI_DAILY_BUDGET_USD,
CHOPIFY_AI_MONTHLY_BUDGET_USD, CHOPIFY_AI_LOCAL_FALLBACK (default only) and
CHOPIFY_GROQ_PLAN. Studio may be stricter than ``.env`` but never looser, so a
paid call needs both ``CHOPIFY_AI_ALLOW_PAID=true`` in ``.env`` and the Studio
switch. Invalid values fall back to the strict default. The Groq plan comes
only from ``.env`` (default "free"; anything unrecognised counts as unknown,
which is treated as paid and blocked in free-only mode).
"""
import copy
import json
import os
import threading
import time

import envconfig
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
    "max_concurrent_requests": 2,     # per provider, across all Chopify processes
    "routes": {
        TRANSCRIBE: {"provider": "local", "model": "medium"},
        GENERATE: {"provider": "local", "model": "qwen2.5:3b"},
        VISION: {"provider": None, "model": None},
    },
    "providers": {
        "local": {"enabled": True, "models": []},
        "groq": {"enabled": False, "models": ["whisper-large-v3-turbo", "whisper-large-v3",
                                              "openai/gpt-oss-20b", "openai/gpt-oss-120b"],
                 # From CHOPIFY_GROQ_PLAN in .env (default free): "free_plan" = Groq's
                 # free plan (no charges); "paid" = developer plan; "unknown" = paid.
                 "billing": "free_plan",
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

ENV_VARS = {"free_only": "CHOPIFY_AI_FREE_ONLY", "paid_calls": "CHOPIFY_AI_ALLOW_PAID",
            "daily_paid_budget_usd": "CHOPIFY_AI_DAILY_BUDGET_USD",
            "monthly_paid_budget_usd": "CHOPIFY_AI_MONTHLY_BUDGET_USD",
            "local_fallback": "CHOPIFY_AI_LOCAL_FALLBACK", "groq_plan": "CHOPIFY_GROQ_PLAN"}
GROQ_PLANS = {"free": "free_plan", "free_plan": "free_plan", "developer": "paid",
              "paid": "paid", "unknown": "unknown"}
BUDGETS = ("daily_paid_budget_usd", "monthly_paid_budget_usd")


def env_limits():
    """Spending defaults/ceilings from .env or the OS environment (strict if invalid)."""
    plan = envconfig.get(ENV_VARS["groq_plan"])
    return {
        "free_only": envconfig.flag(ENV_VARS["free_only"], True),
        "paid_calls": envconfig.flag(ENV_VARS["paid_calls"], False),
        "daily_paid_budget_usd": envconfig.number(ENV_VARS["daily_paid_budget_usd"], 0.0, 0, 10000),
        "monthly_paid_budget_usd": envconfig.number(ENV_VARS["monthly_paid_budget_usd"], 0.0,
                                                    0, 10000),
        "local_fallback": envconfig.flag(ENV_VARS["local_fallback"], False),
        "groq_plan": GROQ_PLANS.get(plan.lower(), "unknown") if plan else "free_plan",
        "sources": {name: envconfig.source(var) for name, var in ENV_VARS.items()},
    }


def apply_env(settings, stored=None):
    """Effective settings: Studio's values limited by the .env ceilings."""
    stored = settings if stored is None else stored
    limits = env_limits()
    out = copy.deepcopy(settings)
    out["free_only"] = limits["free_only"] or bool(stored.get("free_only", limits["free_only"]))
    out["paid_calls"] = limits["paid_calls"] and stored.get("paid_calls") is True
    for name in BUDGETS:
        value = stored.get(name, limits[name])
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        out[name] = min(max(value, 0.0), limits[name])
    if "local_fallback" not in stored:
        out["local_fallback"] = limits["local_fallback"]
    out.setdefault("providers", {}).setdefault("groq", {})["billing"] = limits["groq_plan"]
    return out


def check_env(changes):
    """Refuse Studio changes that would go past the .env ceilings."""
    limits = env_limits()
    var = ENV_VARS
    if changes.get("free_only") is False and limits["free_only"]:
        raise ValueError(f"Free-only mode is required by {var['free_only']} in .env. Change "
                         ".env and restart Studio to turn it off.")
    if changes.get("paid_calls") is True and not limits["paid_calls"]:
        raise ValueError(f"Paid AI calls are disabled by {var['paid_calls']} in .env. Change "
                         ".env and restart Studio first.")
    for name in BUDGETS:
        value = changes.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool)                 and value > limits[name] + 1e-9:
            raise ValueError(f"The {'daily' if name.startswith('daily') else 'monthly'} paid "
                             f"budget cannot exceed ${limits[name]:.2f} ({var[name]} in .env).")
    groq = ((changes.get("providers") or {}).get("groq") or {})
    if "billing" in groq:
        raise ValueError(f"The Groq plan comes from {var['groq_plan']} in .env.")


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


def load_stored():
    """What Studio saved (no .env limits applied)."""
    try:
        stored = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    return stored if isinstance(stored, dict) else {}


def load():
    """Effective settings: defaults + Studio's saved values + .env ceilings."""
    stored = load_stored()
    return apply_env(_merge(DEFAULTS, stored), stored)


def save(settings):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, indent=1), encoding="utf-8")
    os.replace(temp, path)


def update(changes, registry):
    """Validate and store user changes; returns the new settings."""
    with _lock:
        check_env(changes or {})
        settings = load()
        new = _merge(settings, changes or {})
        validate(new, registry)
        save(new)
        return apply_env(new)


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
                            ("max_rate_limit_wait_seconds", 0, 600),
                            ("max_concurrent_requests", 1, 4)):
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
        if provider == "nvidia":
            raise ValueError("The NVIDIA trial is for internal evaluation with synthetic data "
                             "only, so it cannot be a route for your projects.")
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
    if adapter.name == "nvidia" and model not in allowed:
        raise PolicyBlocked(f"NVIDIA trial model '{model}' is not approved: read its model card "
                            "terms on build.nvidia.com, then add it to NVIDIA's allowed models. "
                            "Nothing was sent.", adapter.name)
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
