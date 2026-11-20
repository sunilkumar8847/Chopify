"""Exercise actual HTTP routes, mutation protection and saved project editing."""
import json
import threading
import urllib.error
import urllib.request

import pytest


@pytest.fixture
def server(tmp_path):
    import dashboard
    httpd = dashboard.make_server(tmp_path, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()
    httpd.app.close()
    thread.join()


def request(base, path, data=None, token=None, origin=None):
    headers = {}
    if token:
        headers["X-Chopify-Token"] = token
    if origin:
        headers["Origin"] = origin
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path,
        data=json.dumps(data).encode() if data is not None else None, headers=headers)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def test_http_rejects_cross_origin_and_unauthenticated_mutations(server):
    httpd, base = server
    for token, origin in [(None, base), (httpd.app.token, "https://evil.example")]:
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(base, "/api/projects", {"source": "test.mp4", "options": {}}, token, origin)
        assert exc.value.code == 403


def test_create_edit_and_read_project_over_http(server):
    httpd, base = server
    project = request(base, "/api/projects", {"source": "recording.mp4", "options": {}}, httpd.app.token)
    assert len(request(base, "/api/projects")["projects"]) == 1
    project_id = project["id"]
    request(base, f"/api/projects/{project_id}/settings", {"aspect": "9:16", "layout": "split"}, httpd.app.token)
    detail = request(base, f"/api/projects/{project_id}")
    assert detail["project"]["options"]["layout"] == "split"
    assert detail["candidates"] == []
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(base, f"/api/projects/{project_id}/settings", {"min_len": -1}, httpd.app.token)
    assert exc.value.code == 400


def test_config_exposes_caption_presets_and_defaults(server):
    httpd, base = server
    config = request(base, "/api/config")
    assert set(config["caption_presets"]) == {
        "default", "clean", "minimal", "bold", "boxed", "karaoke", "pop",
        "neon", "typewriter", "retro", "hormozi", "mrbeast", "podcast", "shorts"}
    assert config["caption_presets"]["bold"]["animation"] == "pop"
    assert config["caption_defaults"]["words_per_cue"] == 3
    # Phase 4: smaller Shorts font, captions lifted above the Shorts UI
    assert config["caption_aspects"]["9:16"]["font_size"] == 88
    assert "money" in config["caption_power_words"] or "secret" in config["caption_power_words"]
    assert any(rule["emoji"] == "💰" for rule in config["caption_emoji_rules"])


def test_caption_style_saves_to_project_and_rejects_invalid(server):
    httpd, base = server
    project = request(base, "/api/projects", {"source": "recording.mp4", "options": {}}, httpd.app.token)
    prefix = f"/api/projects/{project['id']}"
    caption = {"words_per_cue": 2, "uppercase": True, "position": "top",
               "animation": "bounce", "background": "box",
               "background_color": "#101010", "background_opacity": 60,
               "highlight_mode": "karaoke", "emoji": True, "emphasis": True}
    request(base, prefix + "/settings", {"caption": caption}, httpd.app.token)
    detail = request(base, prefix)
    assert detail["project"]["options"]["caption"] == caption
    for bad in ({"words_per_cue": 99}, {"color": "red"}, {"animation": "spin"}):
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(base, prefix + "/settings", {"caption": bad}, httpd.app.token)
        assert exc.value.code == 400


def test_media_cannot_escape_project_and_supports_ranges(server):
    import projects
    httpd, base = server
    state = projects.create_project(httpd.app.root, "test.mp4", {})
    path = projects.project_path(httpd.app.root, state["id"])
    (path / "clips").mkdir()
    (path / "clips" / "test.mp4").write_bytes(b"0123456789")
    req = urllib.request.Request(base + f"/media/{state['id']}/clips/test.mp4", headers={"Range": "bytes=2-5"})
    with urllib.request.urlopen(req) as r:
        assert r.status == 206
        assert r.read() == b"2345"
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + f"/media/{state['id']}/clips/%2e%2e/project.json")
    assert exc.value.code in (400, 403, 404)


def test_queued_projects_reject_duplicate_jobs_and_edits(server, monkeypatch):
    httpd, base = server
    gate = threading.Event()
    monkeypatch.setattr(httpd.app, "_run", lambda *args: gate.wait(10))
    project = request(base, "/api/projects", {"source": "test.mp4", "options": {}}, httpd.app.token)
    prefix = f"/api/projects/{project['id']}"
    try:
        request(base, prefix + "/run", {"action": "prepare"}, httpd.app.token)
        assert request(base, prefix)["busy"]
        for action, body in [("run", {"action": "prepare"}),
                             ("settings", {"style": "podcast"}),
                             ("settings", {"caption": {"words_per_cue": 2}})]:
            with pytest.raises(urllib.error.HTTPError) as exc:
                request(base, prefix + "/" + action, body, httpd.app.token)
            assert exc.value.code == 400
    finally:
        gate.set()


# Stand-in for chopify.py: records how Studio launched it, then tries to read
# input exactly like the real scoring prompt does.
FAKE_CHOPIFY = r'''
import json, sys, time
from pathlib import Path
project = Path(sys.argv[sys.argv.index("--project") + 1])
report = {"argv": sys.argv[1:], "start": time.time()}
try:
    report["input"] = input("Minimum clip score (0-10, Enter = 8.0): ")
except EOFError:
    report["input"] = "EOF"
report["end"] = time.time()
(project / "launch.json").write_text(json.dumps(report), encoding="utf-8")
'''


@pytest.fixture
def fake_studio(tmp_path, monkeypatch):
    import dashboard
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "chopify.py").write_text(FAKE_CHOPIFY, encoding="utf-8")
    monkeypatch.setattr(dashboard, "HERE", bin_dir)
    studio = dashboard.Studio(tmp_path / "projects")
    yield studio
    studio.close()


def test_studio_job_never_waits_for_terminal_input(fake_studio):
    import projects
    state = projects.create_project(fake_studio.root, "recording.mp4", {})
    fake_studio.enqueue(state["id"], "prepare")
    # A job blocked on input() would never finish; fail fast instead of hanging.
    fake_studio.futures[state["id"]].result(timeout=60)
    path = projects.project_path(fake_studio.root, state["id"])
    report = json.loads((path / "launch.json").read_text(encoding="utf-8"))
    # --no-input disables the prompt. stdin must also not be the Studio
    # terminal: reading it hits EOF at once. (isatty() is not asserted:
    # Windows reports True for the NUL device even though it never blocks.)
    assert "--no-input" in report["argv"]
    assert report["input"] == "EOF"
    assert report["end"] - report["start"] < 30


def test_studio_queue_runs_next_job_after_previous_finishes(fake_studio):
    import projects
    ids = [projects.create_project(fake_studio.root, f"video{i}.mp4", {})["id"]
           for i in range(3)]
    for project_id in ids:
        fake_studio.enqueue(project_id, "prepare")
    for project_id in ids:
        fake_studio.futures[project_id].result(timeout=60)
    reports = [json.loads((projects.project_path(fake_studio.root, i) / "launch.json")
                          .read_text(encoding="utf-8")) for i in ids]
    # The single worker runs jobs one after another, in queue order.
    for earlier, later in zip(reports, reports[1:]):
        assert earlier["end"] <= later["start"]


def test_studio_launches_jobs_detached_from_terminal(tmp_path, monkeypatch):
    # The behavioural test above cannot tell inherited stdin from DEVNULL when
    # pytest itself has no terminal, so pin both launch settings directly.
    import subprocess
    import dashboard
    import projects
    launched = {}

    class FakeChild:
        def wait(self):
            return 0

    def fake_popen(cmd, **kwargs):
        launched.update(cmd=cmd, **kwargs)
        return FakeChild()

    monkeypatch.setattr(dashboard.subprocess, "Popen", fake_popen)
    studio = dashboard.Studio(tmp_path / "projects")
    try:
        state = projects.create_project(studio.root, "recording.mp4", {})
        studio.enqueue(state["id"], "prepare")
        studio.futures[state["id"]].result(timeout=60)
    finally:
        studio.close()
    assert "--no-input" in launched["cmd"]
    assert launched["stdin"] is subprocess.DEVNULL


# --- Browser disconnects (ConnectionAbortedError, WinError 10053) ---------

class _DeadSocket:
    """A wfile whose peer has gone: Windows raises WinError 10053 on write."""
    def __init__(self, ok_writes=0, error=ConnectionAbortedError):
        self.ok_writes, self.error, self.data = ok_writes, error, b""

    def write(self, data):
        if self.ok_writes <= 0:
            raise self.error(10053, "An established connection was aborted by the software in your host machine")
        self.ok_writes -= 1
        self.data += data
        return len(data)

    def flush(self):
        pass


def _bare_handler(wfile, headers=None):
    import dashboard
    handler = dashboard.Handler.__new__(dashboard.Handler)
    handler.wfile = wfile
    handler.request_version = "HTTP/1.1"
    handler.requestline = "GET /media HTTP/1.1"
    handler.command = "GET"
    handler.close_connection = False
    handler.headers = headers or {}
    return handler


@pytest.mark.parametrize("error", [ConnectionAbortedError, ConnectionResetError, BrokenPipeError])
def test_send_json_to_a_disconnected_browser_does_not_raise(error):
    handler = _bare_handler(_DeadSocket(error=error))
    handler.send_json({"ok": True})                   # must not raise
    assert handler.client_gone and handler.close_connection
    handler.wfile = _DeadSocket(ok_writes=99)
    handler.send_json({"error": "Project or file not found."}, 404)
    assert handler.wfile.data == b"", "nothing is written after the client left"


def test_send_file_stops_quietly_when_the_player_cancels(tmp_path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"x" * 300_000)
    handler = _bare_handler(_DeadSocket(ok_writes=2), {"Range": "bytes=0-"})
    handler.send_file(media)                          # headers + 1 chunk, then abort
    assert handler.client_gone


def test_missing_media_is_a_clean_error_not_half_a_response(tmp_path):
    handler = _bare_handler(_DeadSocket(ok_writes=99))
    with pytest.raises(FileNotFoundError):
        handler.send_file(tmp_path / "gone.mp4")
    assert handler.wfile.data == b"" and not handler.client_gone


def test_server_hides_only_disconnect_tracebacks(capsys):
    import dashboard
    server = dashboard.StudioServer.__new__(dashboard.StudioServer)
    for error, shown in ((ConnectionAbortedError(10053, "aborted"), False),
                         (ConnectionResetError(10054, "reset"), False),
                         (RuntimeError("genuine bug"), True)):
        try:
            raise error
        except Exception:
            server.handle_error(None, ("127.0.0.1", 1))
        err = capsys.readouterr().err
        assert ("Traceback" in err) == shown, (error, err)


def test_aborted_media_request_leaves_studio_working(server, capsys):
    import socket
    import struct
    import projects
    httpd, base = server
    state = projects.create_project(httpd.app.root, "test.mp4", {})
    path = projects.project_path(httpd.app.root, state["id"])
    (path / "clips").mkdir()
    (path / "clips" / "big.mp4").write_bytes(b"\0" * 16_000_000)
    for _ in range(3):
        sock = socket.create_connection(("127.0.0.1", httpd.server_port))
        sock.sendall(f"GET /media/{state['id']}/clips/big.mp4 HTTP/1.1\r\n"
                     f"Host: 127.0.0.1:{httpd.server_port}\r\nRange: bytes=0-\r\n\r\n".encode())
        sock.recv(1024)
        # RST like a browser cancelling the request (seek / project switch)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()
    assert len(request(base, "/api/projects")["projects"]) == 1
    import time
    time.sleep(0.5)
    assert "Traceback" not in capsys.readouterr().err


def test_application_connection_errors_are_still_reported(server, monkeypatch):
    # A network failure *inside* Chopify (e.g. talking to Google) is a real
    # error and must reach the user, not be mistaken for a closed tab.
    httpd, base = server

    def google_dropped():
        raise ConnectionResetError(10054, "Google closed the connection")
    monkeypatch.setattr(httpd.app.youtube, "disconnect", google_dropped)
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(base, "/api/youtube/disconnect", {}, httpd.app.token)
    assert exc.value.code == 400
    assert "Google closed the connection" in json.load(exc.value)["error"]
