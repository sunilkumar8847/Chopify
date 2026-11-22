"""Provider API keys: read from ``.env`` (via ``envconfig``) or the OS environment.

``.env`` in the Chopify folder is the single source of truth for provider
keys. An operating-system environment variable with the same name wins over
``.env``. Chopify never writes keys anywhere: to add, change or remove one,
edit ``.env`` and restart Studio.

Keys never leave this module except to the provider request that needs them;
the UI only ever sees "configured", the source and the last four characters.

Phase 1 kept keys in a DPAPI file (``ai_keys.bin``). That file is no longer
read. If it exists, ``legacy_store()`` reports it (and which providers it
holds, never values) so the user can move them to ``.env``; it is never
deleted or overwritten here.
"""
import envconfig
import youtube_auth

LEGACY_KEYS_FILE = "ai_keys.bin"
ENV = {"groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY",
       "gemini": "GEMINI_API_KEY", "nvidia": "NVIDIA_API_KEY"}
YOUTUBE_API_KEY_ENV = "CHOPIFY_YOUTUBE_API_KEY"


def get(provider):
    """The key for a provider, or None."""
    env = ENV.get(provider)
    return envconfig.get(env) if env else None


def status(provider):
    """Safe summary: never the key itself."""
    env = ENV.get(provider)
    value = envconfig.get(env) if env else None
    if value:
        return {"configured": True, "source": envconfig.source(env), "env": env,
                "last4": value[-4:]}
    return {"configured": False, "source": None, "env": env, "last4": None}


def legacy_store():
    """Is the old Phase 1 DPAPI key file present? Names only, never values."""
    path = youtube_auth.config_dir() / LEGACY_KEYS_FILE
    if not path.exists():
        return {"exists": False, "path": str(path), "providers": []}
    try:
        data = youtube_auth._read_secret(path)
        names = sorted(k for k, v in data.items() if isinstance(v, str) and v) \
            if isinstance(data, dict) else []
    except Exception:              # noqa: BLE001 - unreadable: just report it exists
        names = []
    return {"exists": True, "path": str(path), "providers": names}


def all_keys():
    """Every configured key value (for log redaction only)."""
    return [v for v in (get(p) for p in ENV) if v and len(v) >= 8]
