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

    def fake_run(self, path, action, preview, pick):
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
    browser.wait("document.querySelector('#status').textContent.startsWith('review')", timeout=20)
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
