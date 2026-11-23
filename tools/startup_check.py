"""Phase 9: start Studio with the normal command and check every panel loads.

    python tools/startup_check.py

Runs ``python dashboard.py --no-browser`` exactly as a user would, but on an
isolated projects folder and config (so real projects and settings are not
opened). Then drives a headless Edge/Chrome through the project list, a
project's menu, the Dashboard (AI, live sessions, discovery, storage) and the
auto-publish controls, and fails on any console error.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from harness import (HERE, api, isolated_env, kill_run, real_projects_snapshot,  # noqa: E402
                     run_processes, start_studio)
from browser import Browser  # noqa: E402


def main():
    before = real_projects_snapshot()
    run = HERE.parent / "_phase9" / time.strftime("startup-%Y%m%d-%H%M%S")
    (run / "config").mkdir(parents=True)
    video = run / "My_local-recording.mp4"
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=15:duration=5", "-c:v", "libx264", str(video)],
                   check=True)
    studio, base, token = start_studio(run, 8791, isolated_env(run))
    checks = {}
    browser = Browser()
    try:
        out = (run / "studio.out").read_text(encoding="utf-8", errors="replace")
        checks["studio_started_without_errors"] = "Traceback" not in out
        status, config = api(base, token, "/api/config")
        checks["config_api"] = status == 200
        status, ai = api(base, token, "/api/ai")
        checks["ai_gateway_loaded"] = status == 200 and len(ai.get("providers", [])) >= 5 and \
            ai["settings"]["free_only"] is True and ai["settings"]["paid_calls"] is False
        _, project = api(base, token, "/api/projects", {"source": str(video), "options": {}})
        status, listing = api(base, token, "/api/projects")
        checks["scheduler_loaded"] = (listing.get("scheduler") or {}).get(
            "settings", {}).get("max_sources") == 5
        checks["youtube_downloads_off"] = config.get("compliance", {}).get(
            "youtube_downloads") is False
        browser.goto(base + "/", wait_for="typeof state !== 'undefined' && state.token")
        browser.wait("document.querySelectorAll('#projects [data-id]').length === 1", 15)
        checks["project_list"] = browser.evaluate(
            "document.querySelector('#projects [data-id] strong').textContent") == \
            "My local recording"
        browser.evaluate(f"document.querySelector('[data-menu-for=\"{project['id']}\"]').click()")
        browser.wait("!document.querySelector('#project-menu').hidden", 10)
        checks["project_menu"] = browser.evaluate(
            "[...document.querySelectorAll('#project-menu [data-menu]')].map(b => b.dataset.menu)"
        ) == ["rename", "open-folder", "storage", "delete"]
        browser.evaluate("document.body.click()")
        browser.evaluate(f"selectProject({json.dumps(project['id'])})")
        browser.wait("document.querySelector('#autopublish-status').textContent.length > 0", 15)
        checks["auto_publish_off_by_default"] = "Off" in browser.evaluate(
            "document.querySelector('#autopublish-status').textContent")
        browser.evaluate("showDashboard()")
        browser.wait("document.querySelector('#live-sessions').textContent.includes('No live')", 15)
        checks["live_sessions_panel"] = True
        checks["discovery_panel"] = browser.evaluate(
            "!!document.querySelector('#discover-form') && "
            "document.querySelector('#discover-panel').offsetHeight > 0")
        browser.wait("document.querySelectorAll('#ai-providers [data-provider]').length >= 5", 15)
        checks["ai_panel"] = browser.evaluate(
            "document.querySelector('#ai-summary').textContent") == "FREE-ONLY"
        checks["source_slots_shown"] = "Sources 0/5" in browser.evaluate(
            "document.querySelector('#source-slots').textContent") or "Sources" in \
            browser.evaluate("document.querySelector('#source-slots').textContent")
        checks["no_console_errors"] = not [c for c in browser.console if c["level"] == "error"] \
            and not browser.exceptions
    finally:
        browser.close()
        kill_run(run, studio)
    time.sleep(2)
    checks["no_processes_left"] = not run_processes(run)
    checks["real_projects_untouched"] = real_projects_snapshot() == before
    result = {"run": str(run), "checks": checks, "passed": all(checks.values())}
    (run / "startup.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
