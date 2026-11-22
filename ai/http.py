"""HTTP for AI providers: one place that maps responses to AIError kinds.

Uses the same stdlib client as the YouTube code (``youtube_auth.http_request``),
so no new dependency. Error messages never contain request headers (keys).
"""
import json
import re
import secrets
import socket

import applog
import youtube_auth
from ai.base import (AuthFailed, InvalidOutput, PermanentError, RateLimited,
                     TemporaryError)

DEFAULT_TIMEOUT = 120


def _message(body):
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError):
        text = (body or b"")[:300].decode("utf-8", errors="replace")
        return text.strip()
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")[:300]
    if isinstance(error, str):
        return error[:300]
    return str(data.get("message") or "")[:300] if isinstance(data, dict) else ""


def _retry_after(headers):
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    value = headers.get("retry-after")
    try:
        if value is not None:
            return max(0.0, float(value))
    except ValueError:
        pass
    # No Retry-After: wait for whichever exhausted limit resets
    limits = rate_limits(headers)
    resets = [limits[f"reset_{k}_s"] for k in ("requests", "tokens")
              if f"reset_{k}_s" in limits and limits.get(f"remaining_{k}", 1) <= 0]
    return max(resets) if resets else None


_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def parse_reset(value):
    """Groq/OpenAI reset durations ("7.66s", "2m59.56s", "1h2m", "250ms") -> seconds."""
    if value is None:
        return None
    text = str(value).strip().lower()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    parts = _DURATION.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text.replace(" ", ""):
        return None
    scale = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    return sum(float(n) * scale[u] for n, u in parts)


def rate_limits(headers):
    """The account's live limits from x-ratelimit-* response headers (or {})."""
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    out = {}
    for kind in ("requests", "tokens"):
        for field in ("limit", "remaining"):
            value = headers.get(f"x-ratelimit-{field}-{kind}")
            try:
                if value is not None:
                    out[f"{field}_{kind}"] = int(float(value))
            except ValueError:
                pass
        reset = parse_reset(headers.get(f"x-ratelimit-reset-{kind}"))
        if reset is not None:
            out[f"reset_{kind}_s"] = reset
    return out


def request(provider, method, url, headers=None, body=None, timeout=DEFAULT_TIMEOUT,
            http=None):
    """(status, headers, body) for 2xx; raises the matching AIError otherwise."""
    send = http or youtube_auth.http_request
    try:
        status, response_headers, data = send(method, url, headers or {}, body, timeout)
    except (socket.timeout, TimeoutError) as exc:
        raise TemporaryError(f"{provider}: no answer within {timeout}s.", provider) from exc
    except OSError as exc:
        raise TemporaryError(f"{provider}: connection failed ({type(exc).__name__}).",
                             provider) from exc
    if 200 <= status < 300:
        return status, response_headers, data
    detail = applog.redact(_message(data))
    if status in (401, 403):
        raise AuthFailed(f"{provider} rejected the API key or permission "
                         f"(HTTP {status}): {detail}".strip(), provider, status)
    if status == 429:
        raise RateLimited(f"{provider} rate limit reached: {detail}".strip(), provider,
                          retry_after=_retry_after(response_headers))
    if status >= 500 or status == 408:
        raise TemporaryError(f"{provider} is temporarily unavailable (HTTP {status}): "
                             f"{detail}".strip(), provider, status)
    raise PermanentError(f"{provider} refused the request (HTTP {status}): {detail}".strip(),
                         provider, status)


def json_body(provider, data):
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, AttributeError) as exc:
        raise InvalidOutput(f"{provider} returned a response that is not JSON.", provider) from exc


def multipart(fields, files):
    """(content_type, body) for fields {name: str|list} and files {name: (filename, bytes, mime)}."""
    boundary = "chopify" + secrets.token_hex(12)
    parts = []
    for name, value in fields.items():
        for item in (value if isinstance(value, (list, tuple)) else [value]):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                         f"\r\n\r\n{item}\r\n".encode("utf-8"))
    for name, (filename, content, mime) in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                     f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode("utf-8"))
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)
