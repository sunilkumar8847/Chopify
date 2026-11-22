"""HTTP for AI providers: one place that maps responses to AIError kinds.

Uses the same stdlib client as the YouTube code (``youtube_auth.http_request``),
so no new dependency. Error messages never contain request headers (keys).
"""
import json
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
    value = (headers or {}).get("retry-after")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


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
