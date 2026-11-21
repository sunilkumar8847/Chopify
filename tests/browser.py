"""Tiny Chrome DevTools Protocol client for real-browser Studio tests.

Standard library only (no Selenium/Playwright). Drives an installed
Microsoft Edge or Google Chrome in headless mode: navigate, run JavaScript,
and collect console messages, exceptions and network requests.
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser():
    for path in CANDIDATES:
        if Path(path).is_file():
            return path
    for name in ("msedge", "google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


class _Socket:
    """Minimal WebSocket client (RFC 6455): text frames, client masking."""

    def __init__(self, url):
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed")
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError(response[:200])
        self.buffer = response.split(b"\r\n\r\n", 1)[1]

    def send(self, text):
        data = text.encode()
        header = bytearray([0x81])
        if len(data) < 126:
            header.append(0x80 | len(data))
        elif len(data) < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(data))
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", len(data))
        mask = os.urandom(4)
        self.sock.sendall(bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("browser closed the connection")
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def recv(self):
        message = b""
        while True:
            first, second = self._read(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length)
            opcode = first & 0x0F
            if opcode == 0x8:
                raise ConnectionError("browser closed the connection")
            if opcode in (0x9, 0xA):
                continue
            message += payload
            if first & 0x80:
                return message.decode("utf-8", errors="replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Browser:
    """One headless browser tab with console/network capture."""

    def __init__(self, width=1280, height=900):
        executable = find_browser()
        if executable is None:
            raise RuntimeError("No Edge or Chrome installation found")
        self.profile = tempfile.mkdtemp(prefix="chopify-browser-")
        self.process = subprocess.Popen(
            [executable, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions",
             f"--user-data-dir={self.profile}", "--remote-debugging-port=0",
             f"--window-size={width},{height}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = Path(self.profile) / "DevToolsActivePort"
        deadline = time.time() + 30
        while not (port_file.exists() and port_file.read_text().strip()):
            if time.time() > deadline:
                self.close()
                raise RuntimeError("browser did not start")
            time.sleep(0.1)
        port = int(port_file.read_text().split()[0])
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
        page = next(t for t in targets if t.get("type") == "page")
        self.ws = _Socket(page["webSocketDebuggerUrl"])
        self.next_id = 0
        self.console, self.exceptions, self.requests = [], [], {}
        for method in ("Runtime.enable", "Log.enable", "Network.enable", "Page.enable"):
            self.call(method)

    # ---------------------------------------------------------------- CDP
    def _event(self, message):
        method, params = message.get("method"), message.get("params", {})
        if method == "Runtime.consoleAPICalled":
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in params.get("args", []))
            self.console.append({"level": params.get("type"), "text": text})
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            self.console.append({"level": entry.get("level"), "text": entry.get("text")})
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            self.exceptions.append(details.get("exception", {}).get("description")
                                   or details.get("text"))
        elif method == "Network.requestWillBeSent":
            req = params["request"]
            self.requests[params["requestId"]] = {
                "method": req["method"], "url": req["url"], "status": None,
                "body": req.get("postData"), "failed": None}
        elif method == "Network.responseReceived":
            entry = self.requests.get(params["requestId"])
            if entry is not None:
                entry["status"] = params["response"]["status"]
        elif method == "Network.loadingFailed":
            entry = self.requests.get(params["requestId"])
            if entry is not None:
                entry["failed"] = params.get("errorText")

    def call(self, method, **params):
        self.next_id += 1
        wanted = self.next_id
        self.ws.send(json.dumps({"id": wanted, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == wanted:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
            self._event(message)

    def pump(self, seconds=0.2):
        """Process browser events for a while (console, network)."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.evaluate("1")
            time.sleep(0.05)

    # ------------------------------------------------------------- helpers
    def goto(self, url, wait_for=None, timeout=15):
        self.call("Page.navigate", url=url)
        self.wait(wait_for or "document.readyState === 'complete'", timeout)

    def evaluate(self, expression, await_promise=False):
        result = self.call("Runtime.evaluate", expression=expression,
                           returnByValue=True, awaitPromise=await_promise)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("exception", {}).get(
                "description") or result["exceptionDetails"].get("text"))
        return result.get("result", {}).get("value")

    def wait(self, expression, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(f"!!({expression})"):
                    return True
            except RuntimeError:
                pass
            time.sleep(0.1)
        raise TimeoutError(f"timed out waiting for: {expression}")

    def api_requests(self, path):
        return [r for r in self.requests.values() if path in r["url"]]

    def close(self):
        try:
            self.ws.close()
        except AttributeError:
            pass
        if getattr(self, "process", None) is not None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        shutil.rmtree(getattr(self, "profile", ""), ignore_errors=True)
