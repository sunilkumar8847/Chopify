"""Feature flags for new subsystems (Phase 2 onwards).

A flag is on when its automated acceptance tests pass (``DEFAULTS``) and can
be switched per machine:

* environment: ``CHOPIFY_FEATURE_<NAME>=0|1`` (wins), or
* ``features.json`` in the Chopify config folder: ``{"<name>": false}``.

The YouTube download switch is separate (``compliance.py``) and stays off.
"""
import json
import os

DEFAULTS = {
    "scheduler": True,          # Phase 2: five-source persistent scheduler
    "scene_detection": True,    # Phase 4: classify scenes (observation only)
    # Phase 4: drop non-gameplay sections from clip selection. Off until a
    # labelled evaluation of real recordings meets the targets (scene_eval.py).
    "scene_filtering": False,
}


def _file():
    import youtube_auth
    try:
        data = json.loads((youtube_auth.config_dir() / "features.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def enabled(name):
    if name not in DEFAULTS:
        raise KeyError(f"Unknown feature {name}")
    env = os.environ.get(f"CHOPIFY_FEATURE_{name.upper()}", "").strip().lower()
    if env in {"1", "true", "on", "yes"}:
        return True
    if env in {"0", "false", "off", "no"}:
        return False
    value = _file().get(name)
    return value if isinstance(value, bool) else DEFAULTS[name]


def status():
    return {name: enabled(name) for name in DEFAULTS}
