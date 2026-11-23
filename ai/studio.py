"""What Studio's AI Settings / AI Usage panels read and change.

Nothing here ever returns a key: only "configured", where it comes from and
its last four characters. Keys are configured only in ``.env`` (see
``envconfig``); Studio cannot write them.
"""
import envconfig
from ai import gateway as gateway_mod
from ai import policy, secrets
from ai.base import CAPABILITIES, TRANSCRIBE
from ai.registry import REGISTRY, ROLE


def _privacy(name, conf):
    adapter = REGISTRY[name](conf)
    if name == "openrouter":
        return {"real_media": "per model",
                "detail": "Each request requires zdr: true and data_collection: deny with "
                          "no provider fallback; real data only for models with a listed Zero "
                          "Data Retention endpoint."}
    ok, reason = adapter.real_media_allowed()
    return {"real_media": "allowed" if ok else "blocked",
            "detail": "Real recordings may be sent." if ok else reason}


def _models(name, conf):
    adapter = REGISTRY[name](conf)
    out = []
    for model_id, info in adapter.models.items():
        free = adapter.is_free(model_id)
        out.append({"id": model_id, "capabilities": list(info.capabilities),
                    "pricing": ("free" if free else "paid" if free is False else "unknown"),
                    "price_per_audio_hour": info.price_per_audio_hour,
                    "price_input_per_mtok": info.price_input_per_mtok,
                    "price_output_per_mtok": info.price_output_per_mtok,
                    "source": info.source, "verified": info.verified})
    return out


def overview(gw=None):
    gw = gw or gateway_mod.get()
    settings = gw.settings()
    health = gw.ledger.health()
    limited = gw.ledger.last_limited()
    providers = []
    for name, cls in REGISTRY.items():
        conf = (settings.get("providers") or {}).get(name) or {}
        providers.append({
            "name": name, "title": cls.title, "role": ROLE.get(name, ""),
            "enabled": bool(conf.get("enabled")), "settings": conf,
            "key": ({"configured": True, "source": "not needed", "last4": None}
                    if not cls.needs_key else secrets.status(name)),
            "privacy": _privacy(name, conf), "terms_url": cls.terms_url,
            "models": _models(name, conf), "health": health.get(name),
            "rate_limits": gw.limits.get(name), "last_limited": limited.get(name)})
    limits = policy.env_limits()
    env = envconfig.status()
    return {"settings": {k: v for k, v in settings.items() if k != "providers"},
            "env": {"path": env["path"], "exists": env["exists"], "error": env["error"],
                    "bad_lines": env["bad_lines"], "vars": policy.ENV_VARS,
                    "limits": {k: v for k, v in limits.items() if k != "sources"},
                    "sources": limits["sources"]},
            "legacy_keys": secrets.legacy_store(),
            "providers": providers, "capabilities": list(CAPABILITIES),
            "spent": {"day": gw.ledger.spent("day"), "month": gw.ledger.spent("month")},
            "usage": gw.ledger.summary()}


def apply(body):
    """Settings changes from Studio (validated; keys are separate)."""
    allowed = {"free_only", "paid_calls", "daily_paid_budget_usd", "monthly_paid_budget_usd",
               "local_fallback", "max_retries", "timeout_seconds",
               "max_rate_limit_wait_seconds", "routes", "providers"}
    unknown = set(body) - allowed
    if unknown:
        raise ValueError(f"Unknown AI settings: {', '.join(sorted(unknown))}")
    policy.check_env(body)          # .env ceilings and the .env-only Groq plan
    for name, conf in (body.get("providers") or {}).items():
        bad = set(conf) - {"enabled", "models", "tier"}
        if bad:
            raise ValueError(f"{name}: {', '.join(sorted(bad))} cannot be set here.")
    for capability, route in (body.get("routes") or {}).items():
        if route.get("provider") == "":
            route["provider"] = None
    settings = policy.update(body, REGISTRY)
    return settings


def confirm(provider, what, revoke=False):
    """Explicit confirmations: Groq ZDR, Gemini paid real-media approval."""
    if revoke:
        return policy.revoke(policy.load(), provider, what, REGISTRY)
    return policy.confirm(policy.load(), provider, what, REGISTRY)


KEYS_IN_ENV = ("API keys are set in the .env file in the Chopify folder, not in Studio. "
               "Edit .env, then restart Studio.")


def route_label(settings, capability=TRANSCRIBE):
    route = (settings.get("routes") or {}).get(capability) or {}
    return f"{route.get('provider')}:{route.get('model')}" if route.get("provider") else None
