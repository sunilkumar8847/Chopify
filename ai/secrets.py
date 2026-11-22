"""API keys: Windows DPAPI-encrypted file (same scheme as the YouTube token) or
environment variables. Keys never leave this module except to the provider
request that needs them; the UI only ever sees "configured" and the last 4.
"""
import os
import threading

import youtube_auth

KEYS_FILE = "ai_keys.bin"
ENV = {"groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY",
       "gemini": "GEMINI_API_KEY", "nvidia": "NVIDIA_API_KEY"}
_lock = threading.Lock()


def _path():
    return youtube_auth.config_dir() / KEYS_FILE


def _stored():
    try:
        data = youtube_auth._read_secret(_path())
    except Exception:              # noqa: BLE001 - unreadable = no stored keys
        return {}
    return data if isinstance(data, dict) else {}


def get(provider):
    """The key for a provider, or None. Environment variables win."""
    env = ENV.get(provider)
    if env and os.environ.get(env, "").strip():
        return os.environ[env].strip()
    value = _stored().get(provider)
    return value if isinstance(value, str) and value else None


def set(provider, key):
    if provider not in ENV:
        raise ValueError("Unknown provider.")
    key = str(key or "").strip()
    if not 8 <= len(key) <= 400 or any(c.isspace() for c in key):
        raise ValueError("That does not look like an API key.")
    with _lock:
        data = _stored()
        data[provider] = key
        youtube_auth._write_secret(_path(), data)
    return status(provider)


def remove(provider):
    with _lock:
        data = _stored()
        data.pop(provider, None)
        if data:
            youtube_auth._write_secret(_path(), data)
        else:
            try:
                _path().unlink()
            except OSError:
                pass
    return status(provider)


def status(provider):
    """Safe summary: never the key itself."""
    env = ENV.get(provider)
    if env and os.environ.get(env, "").strip():
        return {"configured": True, "source": "environment", "env": env,
                "last4": os.environ[env].strip()[-4:]}
    value = _stored().get(provider)
    if value:
        return {"configured": True, "source": "stored", "env": env, "last4": value[-4:]}
    return {"configured": False, "source": None, "env": env, "last4": None}


def all_keys():
    """Every configured key value (for log redaction only)."""
    keys = [v for v in _stored().values() if isinstance(v, str)]
    keys += [os.environ[e] for e in ENV.values() if os.environ.get(e)]
    return [k for k in keys if len(k) >= 8]
