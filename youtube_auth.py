"""Phase 5: connect a YouTube channel with Google OAuth 2.0 (desktop app flow).

Uses Google's documented endpoints directly (no extra packages):

* Sign-in: the OAuth 2.0 authorization-code flow for installed apps, with a
  loopback redirect to 127.0.0.1 on a random port, PKCE (S256) and a random
  ``state`` value, so the code is only usable by this process.
* Scopes: ``youtube.upload`` (upload videos) and ``youtube.readonly`` (show
  which channel is connected). Nothing else.
* Tokens: saved encrypted with Windows DPAPI (only this Windows user can read
  them) in the Chopify config folder outside the Git repository; refreshed
  automatically; revoked and deleted on disconnect. Tokens are never logged
  and never returned by ``status()``.

Credentials are never hard-coded: you create a "Desktop app" OAuth client in
your own Google Cloud project and give Chopify its client_secret JSON once.

Config folder: $CHOPIFY_CONFIG, else %APPDATA%\\Chopify (Windows) or
~/.config/chopify.
"""
import base64
import hashlib
import http.client
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/youtube/v3"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly")
CLIENT_FILE = "youtube_client.bin"
TOKEN_FILE = "youtube_token.bin"
FLOW_TIMEOUT = 300          # seconds to finish signing in
REFRESH_MARGIN = 120        # refresh this many seconds before expiry
_ENTROPY = b"chopify-youtube-v1"


class AuthError(RuntimeError):
    """Sign-in or token problem the user has to act on."""


# ------------------------------------------------------------- config folder

def config_dir():
    if os.environ.get("CHOPIFY_CONFIG"):
        return Path(os.environ["CHOPIFY_CONFIG"])
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "Chopify"
    return Path.home() / ".config" / "chopify"


# ------------------------------------------------------------- secret storage

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data):
        buffer = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    def _dpapi(data, protect):
        crypt = ctypes.windll.crypt32
        source, _keep = _blob(data)
        entropy, _keep2 = _blob(_ENTROPY)
        out = _Blob()
        function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
        ok = function(ctypes.byref(source), None, ctypes.byref(entropy), None, None,
                      0x01, ctypes.byref(out))          # CRYPTPROTECT_UI_FORBIDDEN
        if not ok:
            raise AuthError("Windows could not protect or read the saved YouTube "
                            "sign-in. Connect the channel again.")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    if sys.platform == "win32":
        return b"DPAPI1" + _dpapi(data, True)
    return b"PLAIN1" + data


def unprotect(data: bytes) -> bytes:
    if data.startswith(b"DPAPI1"):
        if sys.platform != "win32":
            raise AuthError("This sign-in was saved on Windows; connect again.")
        return _dpapi(data[6:], False)
    if data.startswith(b"PLAIN1"):
        return data[6:]
    raise AuthError("The saved YouTube sign-in is unreadable; connect again.")


def _write_secret(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{secrets.token_hex(4)}.tmp")
    temp.write_bytes(protect(json.dumps(value).encode("utf-8")))
    if sys.platform != "win32":
        os.chmod(temp, 0o600)
    os.replace(temp, path)


def _read_secret(path):
    if not path.is_file():
        return None
    return json.loads(unprotect(path.read_bytes()).decode("utf-8"))


# ------------------------------------------------------------- HTTP

def http_request(method, url, headers=None, body=None, timeout=60):
    """(status, headers{lowercase: value}, body bytes); never raises on HTTP errors.

    http.client is used directly so a 308 "Resume Incomplete" from the upload
    API is returned as-is instead of being treated as a redirect.
    """
    parts = urlsplit(url)
    connection_class = (http.client.HTTPSConnection if parts.scheme == "https"
                        else http.client.HTTPConnection)
    connection = connection_class(parts.netloc, timeout=timeout)
    target = parts.path + ("?" + parts.query if parts.query else "")
    try:
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return (response.status,
                {k.lower(): v for k, v in response.getheaders()}, data)
    finally:
        connection.close()


def _json(body):
    try:
        return json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}


# ------------------------------------------------------------- client secret

def parse_client(data):
    """Validate a Google "Desktop app" client_secret JSON (dict)."""
    section = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        if isinstance(data, dict) and "web" in data:
            raise AuthError("This is a 'Web application' client. Create a "
                            "'Desktop app' OAuth client instead.")
        raise AuthError("Not a Google OAuth client file (client_secret*.json).")
    client_id = section.get("client_id")
    client_secret = section.get("client_secret")
    if not (isinstance(client_id, str) and client_id.endswith(".apps.googleusercontent.com")):
        raise AuthError("The client file has no valid client_id.")
    if not isinstance(client_secret, str) or not client_secret:
        raise AuthError("The client file has no client_secret.")
    return {"client_id": client_id, "client_secret": client_secret}


class YouTubeAccount:
    """The connected channel: client config, tokens, sign-in flow, refresh."""

    def __init__(self, directory=None, http=None, clock=time.time):
        self.dir = Path(directory or config_dir())
        self.http = http or http_request
        self.clock = clock
        self.lock = threading.RLock()
        self.flow = None

    # ----------------------------------------------------------- client
    @property
    def client_path(self):
        return self.dir / CLIENT_FILE

    @property
    def token_path(self):
        return self.dir / TOKEN_FILE

    def import_client(self, source):
        """Store the client_secret JSON (encrypted) in the config folder."""
        source = Path(str(source).strip().strip('"'))
        if not source.is_file():
            raise AuthError(f"Client file not found: {source}")
        try:
            data = json.loads(source.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise AuthError("The client file is not valid JSON.") from exc
        client = parse_client(data)
        _write_secret(self.client_path, client)
        return {"client_id": _mask(client["client_id"])}

    def client(self):
        client = _read_secret(self.client_path)
        if not client:
            raise AuthError("Add your Google OAuth client file first "
                            "(Google Cloud → Credentials → Desktop app).")
        return client

    # ----------------------------------------------------------- status
    def status(self):
        """Safe summary for the UI. Never includes a token or the secret."""
        with self.lock:
            info = {"client_configured": self.client_path.is_file(),
                    "connected": False, "channel": None, "scopes": [],
                    "config_dir": str(self.dir)}
            try:
                if info["client_configured"]:
                    info["client_id"] = _mask(self.client()["client_id"])
                token = _read_secret(self.token_path)
            except AuthError as exc:
                info["error"] = str(exc)
                token = None
            if token and token.get("refresh_token"):
                info.update(connected=True, channel=token.get("channel"),
                            scopes=token.get("scopes", []))
            if self.flow:
                info["signin"] = {k: self.flow[k] for k in ("state", "message")}
            return info

    # ----------------------------------------------------------- sign-in
    def begin_signin(self, open_browser=None):
        """Start the browser sign-in; returns the URL. Finishes in the background."""
        client = self.client()
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        result = {}
        done = threading.Event()

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query)
                if "code" not in query and "error" not in query:
                    self.send_response(404)
                    self.end_headers()
                    return
                if query.get("state", [""])[0] != state:
                    result["error"] = "Sign-in response did not match (state). Try again."
                elif "error" in query:
                    result["error"] = f"Google sign-in was cancelled ({query['error'][0]})."
                else:
                    result["code"] = query["code"][0]
                ok = "code" in result
                page = ("<h2>Chopify is connected. You can close this tab.</h2>" if ok else
                        "<h2>Chopify could not connect. Return to Chopify and try again.</h2>")
                body = ("<!doctype html><meta charset=utf-8><title>Chopify</title>"
                        f"<body style='font-family:sans-serif'>{page}</body>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                done.set()

        server = HTTPServer(("127.0.0.1", 0), Callback)
        server.timeout = 1.0
        redirect = f"http://127.0.0.1:{server.server_port}"
        url = AUTH_URI + "?" + urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect,
            "response_type": "code", "scope": " ".join(SCOPES), "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent"})
        with self.lock:
            self.flow = {"state": "waiting", "message": "Finish signing in in your browser.",
                         "url": url}

        def serve():
            deadline = self.clock() + FLOW_TIMEOUT
            try:
                while not done.is_set() and self.clock() < deadline:
                    server.handle_request()
            finally:
                server.server_close()
            try:
                if "code" not in result:
                    raise AuthError(result.get("error") or "Sign-in timed out. Try again.")
                self.finish_signin(result["code"], verifier, redirect)
                with self.lock:
                    self.flow = {"state": "connected", "message": "YouTube connected."}
            except AuthError as exc:
                with self.lock:
                    self.flow = {"state": "error", "message": str(exc)}

        threading.Thread(target=serve, name="chopify-oauth", daemon=True).start()
        if open_browser:
            open_browser(url)
        return url

    def finish_signin(self, code, verifier, redirect):
        client = self.client()
        status, _, body = self.http("POST", TOKEN_URI, {
            "Content-Type": "application/x-www-form-urlencoded"}, urlencode({
                "code": code, "client_id": client["client_id"],
                "client_secret": client["client_secret"], "redirect_uri": redirect,
                "grant_type": "authorization_code", "code_verifier": verifier}).encode())
        data = _json(body)
        if status != 200 or "access_token" not in data:
            raise AuthError(f"Google refused the sign-in: "
                            f"{data.get('error_description') or data.get('error') or status}")
        if not data.get("refresh_token"):
            raise AuthError("Google did not return a refresh token. Remove Chopify "
                            "from your Google account's third-party access and try again.")
        granted = data.get("scope", "").split()
        if SCOPES[0] not in granted:
            raise AuthError("Upload permission was not granted. Tick the YouTube "
                            "upload permission when signing in.")
        token = {"access_token": data["access_token"],
                 "refresh_token": data["refresh_token"],
                 "expires_at": self.clock() + float(data.get("expires_in", 3600)),
                 "scopes": granted, "channel": None}
        with self.lock:
            _write_secret(self.token_path, token)
        token["channel"] = self.fetch_channel()
        with self.lock:
            _write_secret(self.token_path, token)
        return token["channel"]

    # ----------------------------------------------------------- tokens
    def access_token(self, force_refresh=False):
        with self.lock:
            token = _read_secret(self.token_path)
            if not token or not token.get("refresh_token"):
                raise AuthError("Connect a YouTube channel first.")
            if (not force_refresh and token.get("access_token")
                    and token.get("expires_at", 0) - REFRESH_MARGIN > self.clock()):
                return token["access_token"]
            client = self.client()
            status, _, body = self.http("POST", TOKEN_URI, {
                "Content-Type": "application/x-www-form-urlencoded"}, urlencode({
                    "client_id": client["client_id"],
                    "client_secret": client["client_secret"],
                    "refresh_token": token["refresh_token"],
                    "grant_type": "refresh_token"}).encode())
            data = _json(body)
            if status == 200 and data.get("access_token"):
                token["access_token"] = data["access_token"]
                token["expires_at"] = self.clock() + float(data.get("expires_in", 3600))
                _write_secret(self.token_path, token)
                return token["access_token"]
            if data.get("error") == "invalid_grant":
                # Revoked, expired (7 days in "Testing" mode) or password change.
                token["refresh_token"] = None
                _write_secret(self.token_path, token)
                raise AuthError("YouTube access expired or was revoked. "
                                "Connect the channel again.")
            raise AuthError(f"Could not refresh YouTube access (HTTP {status}).")

    def authorized(self, method, url, headers=None, body=None, timeout=120):
        """API call with a fresh token; refreshes once on 401."""
        for attempt in range(2):
            token = self.access_token(force_refresh=attempt == 1)
            all_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
            status, response_headers, data = self.http(method, url, all_headers, body, timeout)
            if status != 401:
                return status, response_headers, data
        return status, response_headers, data

    def fetch_channel(self):
        status, _, body = self.authorized(
            "GET", f"{API}/channels?part=snippet&mine=true")
        items = _json(body).get("items") or []
        if status != 200 or not items:
            raise AuthError("This Google account has no YouTube channel.")
        return {"id": items[0]["id"], "title": items[0]["snippet"].get("title", "")}

    # ----------------------------------------------------------- disconnect
    def disconnect(self):
        """Revoke access at Google (best effort) and delete the saved tokens."""
        with self.lock:
            try:
                token = _read_secret(self.token_path)
            except AuthError:
                token = None
            revoked = False
            secret = (token or {}).get("refresh_token") or (token or {}).get("access_token")
            if secret:
                try:
                    status, _, _ = self.http("POST", REVOKE_URI, {
                        "Content-Type": "application/x-www-form-urlencoded"},
                        urlencode({"token": secret}).encode(), 30)
                    revoked = status == 200
                except OSError:
                    revoked = False
            try:
                self.token_path.unlink()
            except FileNotFoundError:
                pass
            self.flow = None
            return {"disconnected": True, "revoked": revoked}


def _mask(client_id):
    head = client_id.split("-")[0]
    return head[:4] + "…" + ".apps.googleusercontent.com"


def main():
    """python youtube_auth.py client <client_secret.json> | connect | status | disconnect"""
    import webbrowser
    account = YouTubeAccount()
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if command == "client":
            print(account.import_client(sys.argv[2]))
        elif command == "connect":
            url = account.begin_signin(open_browser=webbrowser.open)
            print("Opening Google sign-in. If no browser opens, visit:\n" + url)
            while account.status().get("signin", {}).get("state") == "waiting":
                time.sleep(1)
            print(account.status()["signin"]["message"])
        elif command == "disconnect":
            print(account.disconnect())
        else:
            print(json.dumps(account.status(), indent=2))
    except AuthError as exc:
        raise SystemExit(f"YouTube: {exc}")


if __name__ == "__main__":
    main()
