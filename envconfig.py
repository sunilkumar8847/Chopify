"""The one place Chopify reads its ``.env`` file.

* Location: ``.env`` next to this file (the Chopify application folder), never
  the terminal's current directory. ``CHOPIFY_ENV_FILE`` may point elsewhere
  (the test suite uses it so tests never see your real keys).
* Precedence: a non-empty operating-system environment variable always wins
  over the same name in ``.env``; ``.env`` never overwrites it.
* Values from ``.env`` are kept inside this module and are NOT copied into
  ``os.environ``, so child processes (ffmpeg, yt-dlp, Ollama, workers) do not
  inherit API keys. Workers read ``.env`` themselves through this module.
* Parsing is python-dotenv's: comments, blank values, single/double quotes,
  ``export KEY=...`` and Windows paths. No ``${VAR}`` expansion.
* A missing file is fine (nothing configured). An unreadable or partly
  malformed file never raises: readable lines are used and the line numbers
  of the others are reported by ``status()``. Values are never logged here.

Studio and workers read the file once per process: restart Studio after
editing ``.env``.
"""
import os
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
OVERRIDE = "CHOPIFY_ENV_FILE"
SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD")

_lock = threading.Lock()
_state = {"loaded": False, "path": None, "values": {}, "exists": False,
          "error": None, "bad_lines": []}


def path():
    override = os.environ.get(OVERRIDE, "").strip()
    return Path(override) if override else ENV_FILE


def _parse(file_path):
    from dotenv.parser import parse_stream
    values, bad = {}, []
    with open(file_path, "r", encoding="utf-8-sig") as stream:
        for binding in parse_stream(stream):
            if binding.error:
                bad.append(binding.original.line)
            elif binding.key:
                values[binding.key] = binding.value if binding.value is not None else ""
    return values, bad


def load(force=False):
    """Read the .env file once (or again with force=True). Never raises."""
    with _lock:
        target = path()
        if _state["loaded"] and not force and _state["path"] == target:
            return dict(_state["values"])
        values, bad, error, exists = {}, [], None, False
        try:
            exists = target.is_file()
            if exists:
                values, bad = _parse(target)
            elif target.exists():
                error = "The .env path is not a file."
        except UnicodeDecodeError:
            values, bad, error = {}, [], "The .env file is not UTF-8 text; it was ignored."
        except OSError as exc:
            values, bad, error = {}, [], f"The .env file could not be read ({type(exc).__name__})."
        except Exception as exc:  # noqa: BLE001 - a broken file must not stop Chopify
            values, bad, error = {}, [], f"The .env file could not be parsed ({type(exc).__name__})."
        _state.update(loaded=True, path=target, values=values, exists=exists, error=error,
                      bad_lines=bad)
        return dict(values)


def get(name, default=None):
    """OS environment (non-empty) first, then .env (non-empty), else default."""
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value.strip()
    value = load().get(name)
    if value is not None and str(value).strip():
        return str(value).strip()
    return default


def source(name):
    """'environment', '.env' or None - where get(name) comes from."""
    value = os.environ.get(name)
    if value is not None and value.strip():
        return "environment"
    value = load().get(name)
    return ".env" if value is not None and str(value).strip() else None


def flag(name, default):
    """A boolean setting; anything unrecognised returns ``default``."""
    value = get(name)
    if value is None:
        return default
    text = value.lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def number(name, default, low=None, high=None):
    """A float setting; invalid or out-of-range values return ``default``."""
    value = get(name)
    if value is None:
        return default
    try:
        result = float(value)
    except ValueError:
        return default
    if result != result or (low is not None and result < low) or (high is not None and result > high):
        return default
    return result


def secret_values():
    """Values of secret-looking variables (for log redaction only)."""
    names = set(load()) | {n for n in os.environ if n.upper().endswith(SECRET_SUFFIXES)}
    out = []
    for name in names:
        if name.upper().endswith(SECRET_SUFFIXES):
            value = get(name)
            if value and len(value) >= 8:
                out.append(value)
    return out


def status():
    """Safe description of the .env file: never any value."""
    load()
    return {"path": str(_state["path"]), "exists": _state["exists"], "error": _state["error"],
            "bad_lines": list(_state["bad_lines"]), "names": sorted(_state["values"])}
