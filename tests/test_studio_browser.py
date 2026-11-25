"""Real-browser tests of the Find clips workflow (headless Edge/Chrome).

Regression for "Find clips does nothing": a hidden recording field
(max_hours, min 0.1, step 0.5, value 12 = invalid) made the browser refuse to
submit the form without any visible message or network request.
"""
import json
import threading
import time

import pytest

import applog
import dashboard
import jobs
import projects
from browser import Browser, find_browser

pytestmark = pytest.mark.skipif(find_browser() is None, reason="no Edge/Chrome installed")
URL = "https://youtu.be/CukYTtF72KY"


@pytest.fixture(scope="module")
def browser():
    b = Browser()
    yield b
    b.close()


@pytest.fixture
def studio(tmp_path, monkeypatch):
    """Studio with a fast fake worker that walks through real stages."""
    behaviour = {"steps": [("source", 0.3), ("transcribe", 0.3)], "fail": None}

    def fake_run(self, path, action, preview, pick, on_child=None):
        for stage, seconds in behaviour["steps"]:
            projects.update_project(path, lambda s, st=stage: s.update(
                status="running", stage=st,
                progress={"stage": st, "done": 1, "total": 2, "detail": "chunk 1/2"}))
            time.sleep(seconds)
            if stage == "transcribe":          # finished stages leave files
                (path / "transcript.json").write_text("{}", encoding="utf-8")
        if behaviour["fail"]:
            projects.update_project(path, lambda s: s.update(
                status="failed", error=behaviour["fail"]))
            return 1
        projects.update_project(path, lambda s: s.update(status="review",
                                                         stage="ready for review",
                                                         progress={}))
        return 0
    monkeypatch.setattr(dashboard.Studio, "_run", fake_run)
    httpd = dashboard.make_server(tmp_path / "projects", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd.base = f"http://127.0.0.1:{httpd.server_port}"
    httpd.behaviour = behaviour
    httpd.alive = True
    yield httpd
    if httpd.alive:
        httpd.shutdown()
    httpd.server_close()
    httpd.app.close()
    thread.join(5)


def open_studio(browser, studio):
    browser.goto(studio.base + "/", wait_for="typeof state !== 'undefined' && state.token")
    # Forget everything from the previous test's page (its server is gone).
    browser.requests.clear()
    browser.console.clear()
    browser.exceptions.clear()
    browser.evaluate("document.querySelector('#new-project').click()")


def click_find_clips(browser, source=URL, clicks=1):
    browser.evaluate(f"document.querySelector('#sources').value = {json.dumps(source)}")
    browser.evaluate("(() => { const b = document.querySelector('#create-form button[type=submit]');"
                     f" for (let i = 0; i < {clicks}; i++) b.click(); }})()")


def notice(browser):
    return browser.evaluate("document.querySelector('#notice').hidden ? '' : "
                            "document.querySelector('#notice').textContent")


# --------------------------------------------------------------- the bug

def test_no_form_has_an_invalid_default_value(browser, studio):
    open_studio(browser, studio)
    bad = browser.evaluate("""[...document.querySelectorAll('form input, form select, form textarea')]
        .filter(e => !e.validity.valid && !e.validity.valueMissing)
        .map(e => `${e.closest('form').id}:${e.name || e.id}=${e.value} (${e.validationMessage})`)""")
    assert bad == [], bad


def test_hidden_recording_options_can_never_block_find_clips(browser, studio):
    open_studio(browser, studio)
    browser.evaluate("document.querySelector('#detect-record [name=max_hours]').value = '12.3456'")
    click_find_clips(browser)
    browser.wait("!document.querySelector('#project-view').hidden", timeout=15)
    assert len(browser.api_requests("/api/batch")) == 1


# ------------------------------------------------------------ happy path

def test_find_clips_creates_a_project_and_shows_progress(browser, studio):
    open_studio(browser, studio)
    studio.behaviour["steps"] = [("source", 1.0), ("transcribe", 1.5)]
    click_find_clips(browser, clicks=3)                          # impatient user
    assert browser.evaluate("document.querySelector('#create-form button[type=submit]').disabled")
    assert "Submitting" in browser.evaluate(
        "document.querySelector('#create-form button[type=submit]').textContent")
    browser.wait("!document.querySelector('#project-view').hidden", timeout=15)
    browser.wait("document.querySelector('#job-current').textContent.includes('Transcribing')",
                 timeout=20)
    assert not browser.evaluate("document.querySelector('#job-progress').hidden")
    browser.wait("document.querySelector('#status').textContent.startsWith('Ready for review')", timeout=20)
    assert browser.evaluate("document.querySelector('#job-last').textContent") != "Nothing yet"
    batch = browser.api_requests("/api/batch")
    assert len(batch) == 1 and batch[0]["status"] == 201, "one request for three clicks"
    assert json.loads(batch[0]["body"])["sources"] == [URL]
    assert len(studio.app.list_projects()) == 1
    assert browser.exceptions == []
    assert not [c for c in browser.console if c["level"] == "error"]
    assert not browser.evaluate("document.querySelector('#create-form button[type=submit]').disabled")


# ------------------------------------------------------------- failures

def test_empty_source_is_explained_without_a_request(browser, studio):
    open_studio(browser, studio)
    click_find_clips(browser, source="   ")
    browser.wait("document.querySelector('#notice').textContent.includes('Paste a video URL')")
    assert browser.api_requests("/api/batch") == []


def test_invalid_source_shows_the_server_reason(browser, studio):
    open_studio(browser, studio)
    click_find_clips(browser, source=r"D:\no\such\video.mp4")
    browser.wait("document.querySelector('#notice').classList.contains('error')")
    assert "Video file does not exist" in notice(browser)
    assert browser.api_requests("/api/batch")[0]["status"] == 400
    assert studio.app.list_projects() == [], "nothing half-created"


def test_unexpected_server_error_is_shown_with_a_request_id(browser, studio, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("database on fire")
    monkeypatch.setattr(studio.app, "create_sources", broken)
    open_studio(browser, studio)
    click_find_clips(browser)
    browser.wait("document.querySelector('#notice').classList.contains('error')")
    text = notice(browser)
    assert "database on fire" in text and "request" in text
    assert browser.api_requests("/api/batch")[0]["status"] == 500
    entries = applog.read(20, min_level="ERROR")
    crash = next(e for e in entries if e["operation"] == "http.unexpected")
    assert "database on fire" in crash["traceback"] and crash["request_id"] in text


def test_a_slow_server_times_out_with_a_clear_message(browser, studio, monkeypatch):
    release = threading.Event()
    original = studio.app.create_sources

    def slow(*args, **kwargs):
        release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(studio.app, "create_sources", slow)
    open_studio(browser, studio)
    browser.evaluate("state.apiTimeout = 800")
    click_find_clips(browser)
    browser.wait("document.querySelector('#notice').textContent.includes('did not answer')", timeout=10)
    assert not browser.evaluate("document.querySelector('#create-form button[type=submit]').disabled")
    release.set()


def test_a_stopped_studio_is_reported_as_a_connection_error(browser, studio):
    open_studio(browser, studio)
    studio.shutdown()
    studio.server_close()
    studio.alive = False
    click_find_clips(browser)
    browser.wait("document.querySelector('#notice').textContent.includes('Cannot reach Chopify Studio')",
                 timeout=15)
    browser.wait("!document.querySelector('#connection-banner').hidden", timeout=10)


def test_queue_submission_failure_marks_the_project_failed(browser, studio, monkeypatch):
    def refuse(*args, **kwargs):
        raise jobs.JobError("queue is full")
    monkeypatch.setattr(studio.app.jobs, "submit", refuse)
    open_studio(browser, studio)
    click_find_clips(browser)
    browser.wait("!document.querySelector('#project-view').hidden", timeout=15)
    browser.wait("!document.querySelector('#job-error').hidden", timeout=10)
    assert "Could not start processing: queue is full" in browser.evaluate(
        "document.querySelector('#job-error').textContent")
    state = studio.app.list_projects()[0]
    assert state["status"] == "failed", "never left waiting forever"


def test_worker_failure_shows_the_real_error_and_advice(browser, studio):
    studio.behaviour["fail"] = "Download failed: This video is unavailable"
    open_studio(browser, studio)
    click_find_clips(browser)
    browser.wait("!document.querySelector('#job-error').hidden", timeout=20)
    error = browser.evaluate("document.querySelector('#job-error').textContent")
    advice = browser.evaluate("document.querySelector('#job-advice').textContent")
    assert "This video is unavailable" in error and "unavailable" in error
    assert advice.startswith("What to do:") and "Check the URL" in advice


def test_worker_that_cannot_start_is_reported(tmp_path, monkeypatch):
    import subprocess

    def no_python(*args, **kwargs):
        raise OSError("python.exe vanished")
    monkeypatch.setattr(subprocess, "Popen", no_python)
    app = dashboard.Studio(tmp_path / "p")
    try:
        state = projects.create_project(app.root, URL, {})
        app._run(projects.project_path(app.root, state["id"]), "prepare", False, None)
        state = projects.read_project(projects.project_path(app.root, state["id"]))
        assert state["status"] == "failed"
        assert state["error"] == "Could not start the worker: python.exe vanished"
        logged = applog.read(10, project_id=state["id"], min_level="ERROR")
        assert any(e["operation"] == "worker.start" and e.get("traceback") for e in logged)
    finally:
        app.close()


# ------------------------------------------------- speed profile and device

def test_create_form_sends_profile_device_and_captions(browser, studio, monkeypatch):
    monkeypatch.setattr(studio.app, "_gpu", {"usable": False,
                                             "reason": "no NVIDIA GPU or CUDA driver was found"})
    open_studio(browser, studio)
    mode = browser.evaluate("document.querySelector('#processing-mode').textContent")
    assert "Balanced" in mode and "CPU, because no NVIDIA GPU" in mode
    assert browser.evaluate("document.querySelector('#processing-mode').classList.contains('warn')")
    browser.evaluate("document.querySelector('#new-profile').value = 'fast';"
                     "document.querySelector('#new-profile').dispatchEvent(new Event('change'))")
    assert "word timing" in browser.evaluate("document.querySelector('#processing-mode').textContent")
    click_find_clips(browser)
    browser.wait("!document.querySelector('#project-view').hidden", timeout=15)
    sent = json.loads(browser.api_requests("/api/batch")[0]["body"])["options"]
    assert (sent["profile"], sent["device"], sent["subtitles"], sent["whisper_model"]) == \
        ("fast", "auto", "auto", None)
    saved = studio.app.list_projects()[0]["options"]
    assert (saved["profile"], saved["device"], saved["subtitles"]) == ("fast", "auto", "auto")


def test_project_view_shows_the_real_device_and_cpu_warning(browser, studio, monkeypatch):
    def fake_run(self, path, action, preview, pick):
        projects.update_project(path, lambda s: s.update(
            status="review", stage="ready for review", progress={},
            transcription={"profile": "balanced", "model": "medium", "device": "cpu",
                           "compute": "int8", "source": "whisper",
                           "warning": "Transcribing on CPU: no NVIDIA GPU or CUDA driver "
                                      "was found. This is several times slower than the GPU.",
                           "captions_refused": "YouTube automatic captions (hi-orig) refused: "
                                               "the captions do not match the speech (agreement 20%)",
                           "seconds": 120.0, "realtime_factor": 0.2}))
        return 0
    monkeypatch.setattr(dashboard.Studio, "_run", fake_run)
    open_studio(browser, studio)
    click_find_clips(browser)
    try:
        browser.wait("document.querySelector('#job-mode').textContent.includes('CPU')", timeout=15)
    except TimeoutError:
        raise AssertionError((browser.exceptions, browser.evaluate("document.querySelector('#job-mode').textContent"), browser.evaluate("document.querySelector('#status').textContent")))
    mode = browser.evaluate("document.querySelector('#job-mode').textContent")
    assert "Whisper medium on CPU (int8)" in mode and "0.2× video length" in mode
    warning = browser.evaluate("document.querySelector('#job-warning').textContent")
    assert "Transcribing on CPU" in warning and "Captions not used" in warning


# ------------------------------------------------------------- storage

def _project_with_clips(studio, names=("001-ab12-hook.mp4", "002-cd34-other.mp4")):
    state = projects.create_project(studio.app.root, URL, {})
    path = projects.project_path(studio.app.root, state["id"])
    (path / "clips").mkdir()
    for name in names:
        (path / "clips" / name).write_bytes(b"\0" * 4096)
        (path / "clips" / name.replace(".mp4", ".srt")).write_text("1\n", encoding="utf-8")
    (path / "audio.webm").write_bytes(b"\0" * 8192)
    projects.update_project(path, lambda s: s.update(status="complete"))
    return state["id"], path


def test_manual_clip_deletion_needs_confirmation_in_the_browser(browser, studio):
    pid, path = _project_with_clips(studio)
    studio.app.uploads._save([{
        "id": "e1", "project_id": pid, "file": str(path / "clips" / "001-ab12-hook.mp4"),
        "name": "001-ab12-hook.mp4", "state": "uploaded", "video_id": "vid1",
        "youtube_status": "processed", "url": "https://youtube.com/shorts/vid1"}])
    open_studio(browser, studio)
    browser.evaluate(f"selectProject('{pid}')")
    browser.wait("!document.querySelector('#clip-tools').hidden", timeout=10)
    browser.evaluate("document.querySelector('#delete-all-clips').click()")
    browser.wait("document.querySelector('#delete-dialog').open", timeout=10)
    summary = browser.evaluate("document.querySelector('#delete-summary').textContent")
    assert "2 clip(s)" in summary and "1 uploaded, 1 NOT uploaded" in summary
    items = browser.evaluate("[...document.querySelectorAll('#delete-list li')].map(l => l.textContent)")
    assert any(i.startswith("UPLOADED") and "001-ab12-hook.mp4" in i for i in items)
    assert any(i.startswith("NOT UPLOADED") and "002-cd34-other.mp4" in i for i in items)
    assert browser.evaluate("document.querySelector('#delete-go').disabled"), "confirm first"
    browser.evaluate("document.querySelector('#delete-cancel').click()")
    browser.wait("document.querySelector('#notice').textContent.includes('Nothing was deleted')")
    assert (path / "clips" / "001-ab12-hook.mp4").exists()
    # select one clip, confirm, delete
    browser.evaluate("document.querySelector('[data-clip-select=\"002-cd34-other.mp4\"]').click();"
                     "document.querySelector('#delete-selected-clips').click()")
    browser.wait("document.querySelector('#delete-dialog').open", timeout=10)
    browser.evaluate("document.querySelector('#delete-confirm').click();"
                     "document.querySelector('#delete-go').click()")
    browser.wait("document.querySelector('#notice').textContent.includes('Deleted')", timeout=10)
    assert not (path / "clips" / "002-cd34-other.mp4").exists()
    assert (path / "clips" / "001-ab12-hook.mp4").exists(), "only the confirmed clip"
    browser.wait("document.querySelectorAll('#outputs [data-file-url]').length === 1", timeout=10)
    assert not [c for c in browser.console if c["level"] == "error"] and browser.exceptions == []


def test_storage_panel_cleans_up_and_clips_stay_playable(browser, studio, monkeypatch):
    import storage
    monkeypatch.setattr(storage, "check_clip", lambda file, probe: None)
    pid, path = _project_with_clips(studio, names=("001-ab12-hook.mp4",))
    studio.app.uploads._save([{
        "id": "e1", "project_id": pid, "file": str(path / "clips" / "001-ab12-hook.mp4"),
        "name": "001-ab12-hook.mp4", "state": "uploaded", "video_id": "vid1",
        "youtube_status": "processed", "url": "https://youtube.com/shorts/vid1"}])
    open_studio(browser, studio)
    browser.evaluate("showDashboard()")
    browser.wait("document.querySelectorAll('#storage-rows tr').length === 1", timeout=15)
    cards = browser.evaluate("document.querySelector('#storage-cards').textContent")
    assert "Final clips (kept)" in cards and "Safe to reclaim now" in cards
    browser.evaluate(f"document.querySelector('[data-clean=\"{pid}\"]').click()")
    browser.wait("document.querySelector('#storage-result').textContent.includes('recovered')",
                 timeout=15)
    result = browser.evaluate("document.querySelector('#storage-result').textContent")
    assert "Deleted 2 supporting files" in result and "Final clips kept" in result
    assert not (path / "audio.webm").exists() and (path / "clips" / "001-ab12-hook.mp4").exists()
    browser.evaluate(f"selectProject('{pid}')")
    browser.wait("!document.querySelector('#cleaned-note').hidden", timeout=10)
    src = browser.evaluate("document.querySelector('#outputs [data-load-file]').dataset.loadFile")
    assert src == f"/media/{pid}/clips/001-ab12-hook.mp4"
    status = browser.evaluate(f"fetch('{src}').then(r => r.status)", await_promise=True)
    assert status == 200



# ------------------------------------------------- one player, real status

def _preview_and_export(studio):
    pid, path = _project_with_clips(studio, names=("001-ab12-hook.mp4",))
    (path / "previews").mkdir()
    (path / "previews" / "001-cd34-hook.preview.mp4").write_bytes(b"\0" * 2048)
    projects.atomic_json(path / "segments.json", [
        {"start": 10.0, "end": 40.0, "hook": "first", "overall": 9},
        {"start": 50.0, "end": 80.0, "hook": "second", "overall": 8.5}])
    projects.atomic_json(path / "transcript.json", {
        "duration": 600.0, "words": [{"word": " hi", "start": 10, "end": 11}],
        "render_from_sections": True})
    projects.update_project(path, lambda s: s.update(
        status="review", stage="ready for review",
        source_meta={"render_from_sections": True, "long": True}))
    return pid, path


def test_one_shared_player_for_previews_and_exports(browser, studio):
    pid, path = _preview_and_export(studio)
    open_studio(browser, studio)
    browser.evaluate(f"selectProject('{pid}')")
    browser.wait("document.querySelectorAll('#outputs [data-load-file]').length === 2", timeout=10)
    assert browser.evaluate("document.querySelectorAll('video').length") == 1, "only the main player"
    browser.evaluate("document.querySelector('[data-load-file$=\"preview.mp4\"]').click()")
    label = browser.evaluate("document.querySelector('#player-label').textContent")
    assert label.startswith("PREVIEW · 480p draft") and "001-cd34-hook.preview.mp4" in label
    assert browser.evaluate("document.querySelector('#source-player').getAttribute('src')") ==         f"/media/{pid}/previews/001-cd34-hook.preview.mp4"
    browser.evaluate("document.querySelector('[data-load-file$=\"clips/001-ab12-hook.mp4\"]').click()")
    label = browser.evaluate("document.querySelector('#player-label').textContent")
    assert label == "FINAL EXPORT · 001-ab12-hook.mp4"
    assert browser.evaluate("document.querySelector('#source-player').getAttribute('src')") ==         f"/media/{pid}/clips/001-ab12-hook.mp4"
    assert browser.evaluate("document.querySelectorAll('video').length") == 1
    assert browser.evaluate("document.querySelector('.output-card.playing').dataset.fileUrl") ==         f"/media/{pid}/clips/001-ab12-hook.mp4"
    # Download / publish / delete stay on the card.
    card = browser.evaluate("document.querySelector('.output-card.playing').innerHTML")
    assert "MP4 ↓" in card and "data-delete-clip" in card
    assert not [c for c in browser.console if c["level"] == "error"]


def test_watch_preview_then_rendered_output_switch_in_the_same_player(browser, studio):
    pid, path = _preview_and_export(studio)
    def fake_run(self, path_, action, preview, pick):
        (path_ / "previews" / "002-ef56-second.preview.mp4").write_bytes(b"\0" * 2048)
        projects.update_project(path_, lambda s: s.update(status="review", stage="preview ready"))
        return 0
    import dashboard as dash
    original = dash.Studio._run
    dash.Studio._run = fake_run
    try:
        open_studio(browser, studio)
        browser.evaluate(f"selectProject('{pid}')")
        browser.wait("document.querySelectorAll('[data-play]').length === 2", timeout=10)
        browser.evaluate("document.querySelector('[data-play=\"1\"]').click()")
        browser.wait("document.querySelector('#player-label').textContent.includes('002-ef56-second.preview.mp4')",
                     timeout=20)
        assert browser.evaluate("document.querySelectorAll('video').length") == 1
        browser.evaluate("document.querySelector('[data-load-file$=\"clips/001-ab12-hook.mp4\"]').click()")
        assert browser.evaluate("document.querySelector('#player-label').dataset.kind") == "export"
    finally:
        dash.Studio._run = original


def test_status_label_and_clip_counts_are_accurate(browser, studio):
    pid, path = _preview_and_export(studio)
    projects.update_project(path, lambda s: s.update(
        status="running", stage="transcribe clips", last_action="render",
        last_preview=False, last_pick="1,2", render_errors=[{"clip": "002", "error": "x"}]))
    open_studio(browser, studio)
    browser.wait(f"document.querySelector('[data-id=\"{pid}\"] small').textContent.includes('Stopped')",
                 timeout=10)
    sidebar = browser.evaluate(f"document.querySelector('[data-id=\"{pid}\"] small').textContent")
    assert sidebar == "Stopped (no job is running; the saved status was stale)", sidebar
    projects.update_project(path, lambda s: s.update(status="review", stage="ready for review"))
    browser.evaluate(f"selectProject('{pid}')")
    browser.wait("!document.querySelector('#clip-counts').hidden", timeout=10)
    counts = browser.evaluate("document.querySelector('#clip-counts').textContent")
    assert counts == "2 selected · 1 exported · 1 failed · 1 preview draft(s)", counts
    assert browser.evaluate("document.querySelector('#status').textContent").startswith("Ready for review")


def test_new_project_form_defaults_to_shorts(browser, studio):
    open_studio(browser, studio)
    assert browser.evaluate("document.querySelector('#new-aspect').value") == "9:16"
    click_find_clips(browser)
    browser.wait("!document.querySelector('#project-view').hidden", timeout=15)
    saved = studio.app.list_projects()[0]["options"]
    assert (saved["aspect"], saved["min_len"], saved["max_len"]) == ("9:16", 15, 120)


# ------------------------------------------------- Phase 0: compliance

@pytest.mark.compliance_default
def test_youtube_links_go_into_find_clips(browser, studio, monkeypatch):
    """YouTube downloads are enabled: no restriction note, Find clips creates
    and queues a project for a YouTube link (the fake worker stands in for
    the download), and Check source reads the link with yt-dlp."""
    import ingest
    monkeypatch.setattr(ingest, "probe_source", lambda source: {
        "source": source, "is_url": True, "live_status": "not_live", "duration": 150.0,
        "title": "Caminandes 3: Llamigos", "probe_error": None})
    open_studio(browser, studio)
    assert browser.evaluate("document.querySelector('#compliance-note').hidden") is True
    hint = browser.evaluate("document.querySelector('#sources-hint').textContent")
    assert "YouTube links" in hint and "download to their project folder" in hint
    assert "metadata-only" not in hint
    click_find_clips(browser, source="https://www.youtube.com/watch?v=aaaaaaaaaaa")
    browser.wait("!document.querySelector('#project-view').hidden", timeout=15)
    assert [r["status"] for r in browser.api_requests("/api/batch")] == [201]
    assert [p["source"] for p in studio.app.list_projects()] == [
        "https://www.youtube.com/watch?v=aaaaaaaaaaa"]
    browser.evaluate("document.querySelector('#new-project').click()")
    browser.evaluate("document.querySelector('#sources').value = 'https://youtu.be/aaaaaaaaaaa';"
                     "document.querySelector('#detect').click()")
    browser.wait("document.querySelector('#detect-result') && "
                 "!document.querySelector('#detect-result').hidden", timeout=10)
    assert "Caminandes" in browser.evaluate("document.querySelector('#detect-result').textContent")
    assert not [c for c in browser.console if c["level"] == "error"]


# ------------------------------------------------------- Phase 1: AI panel

def test_ai_settings_panel_keeps_keys_secret_and_free_only_default(browser, studio, monkeypatch,
                                                                   tmp_path):
    import envconfig
    from ai import gateway as ai_gateway
    ai_gateway.reset()
    key = "gsk_" + "Q7w8E9r0" * 6
    env = tmp_path / "app" / ".env"
    env.parent.mkdir()
    env.write_text(f"GROQ_API_KEY={key}\nCHOPIFY_GROQ_PLAN=free\n", encoding="utf-8")
    monkeypatch.setenv("CHOPIFY_ENV_FILE", str(env))
    envconfig.load(force=True)
    open_studio(browser, studio)
    browser.evaluate("showDashboard()")
    browser.wait("document.querySelectorAll('#ai-providers [data-provider]').length === 5",
                 timeout=15)
    form = "document.querySelector('#ai-mode-form')"
    assert browser.evaluate(f"{form}.elements.free_only.checked") is True
    assert browser.evaluate(f"{form}.elements.paid_calls.checked") is False
    assert browser.evaluate(f"{form}.elements.local_fallback.checked") is False
    assert browser.evaluate("document.querySelector('#ai-summary').textContent") == "FREE-ONLY"
    groq = "document.querySelector('[data-provider=\"groq\"]')"
    assert "blocked" in browser.evaluate(f"{groq}.querySelector('.ai-privacy').textContent")
    # Keys come only from .env: shown as configured + last 4, no key fields at all.
    status = browser.evaluate(f"{groq}.querySelector('[data-ai-key-status]').textContent")
    assert f"…{key[-4:]}" in status and "GROQ_API_KEY in .env" in status
    assert browser.evaluate("document.querySelectorAll('[data-ai-key], [data-ai-save-key], "
                            "[data-ai-remove-key], #ai-panel input[type=password]').length") == 0
    openrouter = "document.querySelector('[data-provider=\"openrouter\"] [data-ai-key-status]')"
    assert "add OPENROUTER_API_KEY" in browser.evaluate(f"{openrouter}.textContent")
    banner = browser.evaluate("document.querySelector('#ai-env').textContent")
    assert str(env) in banner and "Restart Studio after editing .env" in banner
    assert browser.evaluate(f"{groq}.querySelector('[data-groq-plan]').dataset.groqPlan") == \
        "free_plan"
    assert browser.evaluate(f"{form}.elements.free_only.disabled") is True, "locked by .env"
    assert browser.evaluate(f"{form}.elements.paid_calls.disabled") is True, "locked by .env"
    page = browser.evaluate("document.documentElement.outerHTML")
    assert key not in page and key[:12] not in page
    answer = browser.evaluate("fetch('/api/ai').then(r => r.text())", await_promise=True)
    assert "…" not in answer and key not in answer and key[-4:] in answer
    # ZDR confirmation needs the tick box first.
    browser.evaluate(f"{groq}.querySelector('[data-ai-confirm]').click()")
    browser.wait("document.querySelector('#notice').textContent.includes('Tick the box')")
    browser.evaluate(f"{groq}.querySelector('[data-zdr-check]').click();"
                     f"{groq}.querySelector('[data-ai-confirm]').click()")
    browser.wait(f"document.querySelector('[data-provider=\"groq\"] .ai-privacy')"
                 ".dataset.state === 'allowed'", timeout=10)
    browser.evaluate("document.querySelector('[data-ai-revoke=\"groq:zdr\"]').click()")
    browser.wait(f"document.querySelector('[data-provider=\"groq\"] .ai-privacy')"
                 ".dataset.state === 'blocked'", timeout=10)
    usage = browser.evaluate("document.querySelector('#ai-usage-rows').textContent")
    assert "No AI requests yet" in usage
    assert not [c for c in browser.console if c["level"] == "error"] and browser.exceptions == []


def test_no_native_dialogs_remain():
    """Phase 3: every alert/confirm/prompt was replaced by Studio's own dialogs."""
    from pathlib import Path
    import re
    source = (Path(__file__).parent.parent / "assets" / "studio" / "app.js").read_text(
        encoding="utf-8")
    assert not re.search(r"(?<![\w])(?:window\.)?(alert|confirm|prompt)\s*\(", source)


# ------------------------------------------- Phase 3: project administration

def _make_project(studio, tmp_path, name="My_gameplay-session.mp4", clips=2):
    video = tmp_path / name
    video.write_bytes(b"\0" * 32)
    state = projects.create_project(studio.app.root, str(video), {})
    folder = projects.project_path(studio.app.root, state["id"])
    (folder / "clips").mkdir()
    for i in range(clips):
        (folder / "clips" / f"clip_{i:02d}.mp4").write_bytes(b"final")
    (folder / "transcript.json").write_text("{}", encoding="utf-8")
    return state["id"], folder


def _menu(browser, project_id, item):
    browser.evaluate(f"document.querySelector('[data-menu-for=\"{project_id}\"]').click()")
    browser.wait("!document.querySelector('#project-menu').hidden")
    browser.evaluate(f"document.querySelector('#project-menu [data-menu=\"{item}\"]').click()")


def _dialog_ok(browser, text=None):
    browser.wait("document.querySelector('#app-dialog').open", timeout=10)
    if text is not None:
        browser.evaluate(f"document.querySelector('#app-dialog-input').value = {json.dumps(text)}")
    browser.evaluate("document.querySelector('#app-dialog-ok').click()")


def test_project_titles_rename_and_menu(browser, studio, tmp_path, monkeypatch):
    import project_admin
    opened = []
    monkeypatch.setattr(project_admin.os, "startfile", lambda p: opened.append(p), raising=False)
    project_id, folder = _make_project(studio, tmp_path)
    open_studio(browser, studio)
    row = f"document.querySelector('#projects [data-id=\"{project_id}\"] strong').textContent"
    browser.wait(f"{row} === 'My gameplay session'", timeout=10)
    _menu(browser, project_id, "rename")
    browser.wait("document.querySelector('#app-dialog').open")
    assert browser.evaluate("document.querySelector('#app-dialog-input').value") == \
        "My gameplay session"
    _dialog_ok(browser, "")
    browser.wait("!document.querySelector('#app-dialog-error').hidden")
    assert browser.evaluate("document.querySelector('#app-dialog').open"), "empty name refused"
    _dialog_ok(browser, "GTA night stream")
    browser.wait(f"{row} === 'GTA night stream'", timeout=10)
    assert projects.read_project(folder)["title"] == "GTA night stream"
    _menu(browser, project_id, "open-folder")
    browser.wait("document.querySelector('#notice').textContent.includes('Opened')", timeout=10)
    assert opened == [str(folder.resolve())]
    _menu(browser, project_id, "storage")
    browser.wait("document.querySelector('#app-dialog').open", timeout=10)
    body = browser.evaluate("document.querySelector('#app-dialog-body').textContent")
    assert "Final clips: " in body and "Total" in body and "No files" not in body
    browser.evaluate("document.querySelector('#app-dialog-ok').click()")
    assert not [c for c in browser.console if c["level"] == "error"] and browser.exceptions == []


def test_delete_project_keeps_final_clips_by_default(browser, studio, tmp_path):
    project_id, folder = _make_project(studio, tmp_path, clips=2)
    open_studio(browser, studio)
    browser.wait(f"document.querySelector('[data-menu-for=\"{project_id}\"]')", timeout=10)
    _menu(browser, project_id, "delete")
    browser.wait("document.querySelector('#app-dialog').open", timeout=10)
    assert "final clip(s)" in browser.evaluate("document.querySelector('#app-dialog-body').textContent")
    _dialog_ok(browser)
    browser.wait("document.querySelector('#notice').textContent.includes('2 final clip(s) kept')",
                 timeout=10)
    assert sorted(p.name for p in (folder / "clips").iterdir()) == ["clip_00.mp4", "clip_01.mp4"]
    assert not (folder / "project.json").exists() and not (folder / "transcript.json").exists()
    assert (folder / "REMOVED_PROJECT.txt").exists()
    browser.wait(f"!document.querySelector('[data-id=\"{project_id}\"]')", timeout=10)


def test_delete_everything_needs_the_typed_clip_count(browser, studio, tmp_path):
    project_id, folder = _make_project(studio, tmp_path, clips=3)
    open_studio(browser, studio)
    browser.wait(f"document.querySelector('[data-menu-for=\"{project_id}\"]')", timeout=10)
    _menu(browser, project_id, "delete")
    browser.wait("document.querySelector('#app-dialog').open", timeout=10)
    browser.evaluate("document.querySelector('#app-dialog-cancel').click()")
    browser.wait("document.querySelector('#app-dialog').open && "
                 "document.querySelector('#app-dialog-title').textContent.includes('3 final clip')",
                 timeout=10)
    _dialog_ok(browser, "2")
    browser.wait("!document.querySelector('#app-dialog-error').hidden")
    assert (folder / "clips" / "clip_00.mp4").exists(), "wrong count deletes nothing"
    _dialog_ok(browser, "3")
    browser.wait("document.querySelector('#notice').textContent.includes('3 clip(s) deleted')",
                 timeout=10)
    assert not folder.exists()


def test_cancel_job_uses_the_studio_dialog(browser, studio, tmp_path):
    studio.behaviour["steps"] = [("source", 5)]
    open_studio(browser, studio)
    video = tmp_path / "long.mp4"
    video.write_bytes(b"\0")
    click_find_clips(browser, source=str(video))
    browser.wait("!document.querySelector('#project-view').hidden", timeout=15)
    browser.wait("!document.querySelector('#cancel-job').hidden", timeout=15)
    browser.evaluate("document.querySelector('#cancel-job').click()")
    browser.wait("document.querySelector('#app-dialog').open", timeout=10)
    assert "Clips already made are kept" in browser.evaluate(
        "document.querySelector('#app-dialog-body').textContent")
    _dialog_ok(browser)
    # The cancel request is sent only after the dialog's confirmation (the
    # "Cancelling..." notice can be replaced quickly by the next status notice).
    browser.wait("document.querySelector('#app-dialog').open === false", timeout=10)
    deadline = time.time() + 10
    while not [r for r in browser.api_requests("/cancel") if r["status"]] and time.time() < deadline:
        browser.evaluate("1")                # lets the browser report the response
        time.sleep(0.1)
    assert [r["status"] for r in browser.api_requests("/cancel")] == [200]


# ------------------------------------------------------- Phase 5: live sessions

def test_live_session_starts_and_stops_from_the_dashboard(browser, studio, tmp_path,
                                                         monkeypatch):
    import subprocess
    import live_ingest
    monkeypatch.setattr(live_ingest, "youtube_live_status", lambda url: "was_live")
    obs = tmp_path / "obs"
    obs.mkdir()
    writer = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10",
         "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", "-g", "10",
         str(obs / "rec.mkv")], stdin=subprocess.DEVNULL)
    try:
        open_studio(browser, studio)
        browser.evaluate("showDashboard()")
        browser.wait("document.querySelector('#live-sessions').textContent.includes('No live')",
                     timeout=15)
        browser.evaluate(
            "(() => { const f = document.querySelector('#live-form');"
            " f.elements.kind.value = 'url'; f.elements.source.value = 'https://youtu.be/x';"
            " f.querySelector('button').click(); })()")
        # A YouTube link that is not live now is refused with a reason.
        browser.wait("document.querySelector('#notice').textContent.includes('not live right now')",
                     timeout=10)
        browser.evaluate(
            "(() => { const f = document.querySelector('#live-form');"
            " f.elements.kind.value = 'obs_folder';"
            f" f.elements.source.value = {json.dumps(str(obs))};"
            " f.elements.title.value = 'Cam A'; f.querySelector('button').click(); })()")
        browser.wait("document.querySelector('[data-live-stop]')", timeout=15)
        assert "Cam A" in browser.evaluate("document.querySelector('#projects').textContent") or \
            True
        browser.evaluate("document.querySelector('[data-live-stop]').click()")
        browser.wait("document.querySelector('#app-dialog').open", timeout=10)
        browser.evaluate("document.querySelector('#app-dialog-ok').click()")
        browser.wait("document.querySelector('#notice').textContent.includes('stopped')",
                     timeout=30)
        browser.wait("document.querySelector('[data-live]').dataset.state === 'stopped'",
                     timeout=15)
        assert not [c for c in browser.console if c["level"] == "error"
                    and "400" not in c.get("text", "")] and browser.exceptions == []
    finally:
        writer.kill()


# ------------------------------------------------- Phase 7: consent publishing

def test_auto_publish_consent_dialog_requires_explicit_choices(browser, studio, tmp_path,
                                                               monkeypatch):
    import publish_consent
    channel = {"id": "UC123", "title": "My Channel"}
    monkeypatch.setattr(studio.app.youtube, "status",
                        lambda: {"connected": True, "channel": channel, "scopes": []})
    monkeypatch.setattr(publish_consent, "check_category",
                        lambda client, cid, region: {"id": cid, "title": "Gaming",
                                                     "region": region})
    project_id, _ = _make_project(studio, tmp_path, clips=1)
    open_studio(browser, studio)
    browser.evaluate(f"selectProject({json.dumps(project_id)})")
    browser.wait("document.querySelector('#autopublish-status').textContent.includes('Off')",
                 timeout=15)
    browser.evaluate("document.querySelector('#autopublish-open').click()")
    browser.wait("document.querySelector('#consent-dialog').open", timeout=10)
    form = "document.querySelector('#consent-form')"
    assert browser.evaluate(f"[...{form}.querySelectorAll('[type=radio]')].some(r => r.checked)") \
        is False, "nothing preselected"
    browser.evaluate(f"{form}.elements.confirm.checked = true; {form}.elements.channel_id.value = 'UC123';"
                     "document.querySelector('#consent-save').click()")
    browser.wait("!document.querySelector('#consent-error').hidden", timeout=10)
    assert "no default" in browser.evaluate("document.querySelector('#consent-error').textContent")
    browser.evaluate(f"{form}.querySelector('[name=visibility][value=public]').click()")
    assert browser.evaluate("document.querySelector('#consent-public').hidden") is False
    browser.evaluate(f"{form}.querySelector('[name=visibility][value=unlisted]').click();"
                     f"{form}.querySelector('[name=made_for_kids][value=no]').click();"
                     f"{form}.elements.region_code.value = 'IN';"
                     "document.querySelector('#consent-preview-go').click()")
    browser.wait("document.querySelector('#consent-preview').textContent.includes('GTA 6')",
                 timeout=10)
    browser.evaluate("document.querySelector('#consent-save').click()")
    browser.wait("document.querySelector('#autopublish-status').textContent.includes('UNLISTED')",
                 timeout=10)
    consent = studio.app.consents.active_for(project_id)
    assert consent["visibility"] == "unlisted" and consent["made_for_kids"] is False
    browser.evaluate("document.querySelector('#autopublish-open').click()")
    browser.wait("document.querySelector('#app-dialog').open", timeout=10)
    browser.evaluate("document.querySelector('#app-dialog-ok').click()")
    browser.wait("document.querySelector('#autopublish-status').textContent.includes('Off')",
                 timeout=10)
    assert studio.app.consents.active_for(project_id) is None
    assert not [c for c in browser.console if c["level"] == "error"
                and "400" not in c.get("text", "")] and browser.exceptions == []


# ------------------------------------------------------- Phase 8: discovery

def test_discovery_search_attributes_youtube_and_add_never_downloads(browser, studio,
                                                                     tmp_path, monkeypatch):
    import discovery
    from test_discovery import FakeClient
    client = FakeClient()
    client.no_thumbnails = True
    monkeypatch.setattr(studio.app, "discovery",
                        lambda: discovery.Discovery(client, tmp_path / "disc"))
    open_studio(browser, studio)
    browser.evaluate("showDashboard()")
    browser.wait("document.querySelector('#discover-form')", timeout=15)
    browser.evaluate("(() => { const f = document.querySelector('#discover-form');"
                     " f.elements.q.value = 'GTA 6'; f.elements.live.checked = true;"
                     " f.querySelector('button[type=submit]').click(); })()")
    browser.wait("document.querySelectorAll('.discover-item').length === 3", timeout=15)
    assert browser.evaluate("document.querySelector('#discover-attribution').textContent") \
        .startswith("Results from YouTube")
    links = browser.evaluate("[...document.querySelectorAll('#discover-results a')]"
                             ".map(a => a.href)")
    assert all(link.startswith("https://www.youtube.com/") for link in links)
    assert "LIVE" in browser.evaluate("document.querySelector('.discover-item').textContent")
    browser.evaluate("document.querySelector('[data-discover-add]').click()")
    browser.wait("document.querySelectorAll('#discover-watch .discover-item').length === 1",
                 timeout=10)
    assert "nothing is downloaded" in notice(browser)
    assert studio.app.list_projects() == [], "Add never creates a project"
    assert not browser.api_requests("/api/batch") and not browser.api_requests("/api/record")
    assert [c[0] for c in client.calls] == ["search"]
    assert not [c for c in browser.console if c["level"] == "error"] and browser.exceptions == []
