""".env configuration: loading, precedence, key safety, free-tier defaults.

All keys here are fake test strings. The real .env is never read: conftest
points CHOPIFY_ENV_FILE at a temporary file for every test.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import applog
import envconfig
import projects
import youtube_auth
from ai import gateway as gateway_mod
from ai import policy, secrets
from ai import studio as ai_studio
from ai.base import SYNTHETIC, AuthFailed, PolicyBlocked, QuotaExhausted
from ai.gateway import Gateway
from ai.registry import REGISTRY
from ai.usage import Ledger

REPO = Path(__file__).resolve().parent.parent
# A key in no known provider format, so only value-based redaction can catch it.
PLAIN_KEY = "plainTestKey" + "x7Q9" * 6
GROQ_KEY = "gsk_" + "T3stOnly" * 6


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    """Write a test .env and make envconfig use it."""
    path = tmp_path / "app" / ".env"
    path.parent.mkdir()

    def write(text, raw=None):
        if raw is not None:
            path.write_bytes(raw)
        else:
            path.write_text(text, encoding="utf-8")
        monkeypatch.setenv("CHOPIFY_ENV_FILE", str(path))
        envconfig.load(force=True)
        return path
    yield write
    envconfig.load(force=True)


@pytest.fixture(autouse=True)
def _fresh_gateway():
    gateway_mod.reset()
    yield
    gateway_mod.reset()


# ------------------------------------------------------------- location

def test_env_file_lives_in_the_application_folder(monkeypatch):
    monkeypatch.delenv("CHOPIFY_ENV_FILE")
    assert envconfig.path() == REPO / ".env"
    assert envconfig.APP_DIR == REPO


def test_running_from_another_working_directory_reads_the_app_folder_env(tmp_path):
    app = tmp_path / "app"
    cwd = tmp_path / "elsewhere"
    app.mkdir()
    cwd.mkdir()
    shutil.copy(REPO / "envconfig.py", app / "envconfig.py")
    (app / ".env").write_text("GROQ_API_KEY=from-app-folder-1234\n", encoding="utf-8")
    (cwd / ".env").write_text("GROQ_API_KEY=from-working-dir-1234\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CHOPIFY_ENV_FILE", "GROQ_API_KEY")}
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import envconfig; "
            "print(envconfig.path()); print(envconfig.get('GROQ_API_KEY'))")
    out = subprocess.run([sys.executable, "-c", code, str(app)], cwd=cwd, env=env,
                         capture_output=True, text=True, check=True).stdout.split()
    assert Path(out[0]) == app / ".env"
    assert out[1] == "from-app-folder-1234"


# ------------------------------------------------------------- parsing

def test_os_environment_wins_and_is_never_overwritten(env_file, monkeypatch):
    env_file(f"GROQ_API_KEY=file-value-12345678\nNVIDIA_API_KEY={PLAIN_KEY}\n")
    monkeypatch.setenv("GROQ_API_KEY", "os-value-12345678")
    assert envconfig.get("GROQ_API_KEY") == "os-value-12345678"
    assert envconfig.source("GROQ_API_KEY") == "environment"
    assert os.environ["GROQ_API_KEY"] == "os-value-12345678", "not overwritten"
    assert envconfig.get("NVIDIA_API_KEY") == PLAIN_KEY
    assert envconfig.source("NVIDIA_API_KEY") == ".env"
    # .env values are not copied into os.environ (child processes never inherit keys)
    assert "NVIDIA_API_KEY" not in os.environ
    # an empty OS variable does not hide the .env value
    monkeypatch.setenv("GROQ_API_KEY", "  ")
    assert envconfig.get("GROQ_API_KEY") == "file-value-12345678"


def test_missing_env_file_is_safe(env_file, monkeypatch, tmp_path):
    monkeypatch.setenv("CHOPIFY_ENV_FILE", str(tmp_path / "nothing" / ".env"))
    envconfig.load(force=True)
    status = envconfig.status()
    assert status["exists"] is False and status["error"] is None and status["names"] == []
    assert envconfig.get("GROQ_API_KEY") is None
    assert secrets.status("groq") == {"configured": False, "source": None,
                                      "env": "GROQ_API_KEY", "last4": None}


def test_empty_keys_count_as_not_configured(env_file):
    env_file((REPO / ".env.example").read_text(encoding="utf-8"))
    for provider in secrets.ENV:
        assert secrets.get(provider) is None
        assert secrets.status(provider)["configured"] is False
    assert envconfig.status()["bad_lines"] == []
    assert policy.env_limits()["groq_plan"] == "free_plan"


def test_quotes_comments_export_and_windows_paths(env_file):
    env_file("# comment line\n"
             "\n"
             "GROQ_API_KEY='gsk_single quoted value'\n"
             'OPENROUTER_API_KEY="sk-or double # not a comment"\n'
             "GEMINI_API_KEY=AIzaPlain   # trailing comment\n"
             "export NVIDIA_API_KEY=nvapi-exported\n"
             "CHOPIFY_MODELS=D:\\Clipping\\Chopify\\models\n"
             "WIN_QUOTED='C:\\Users\\me\\new folder'\n"
             "BLANK=\n"
             "NOVALUE\n")
    assert envconfig.get("GROQ_API_KEY") == "gsk_single quoted value"
    assert envconfig.get("OPENROUTER_API_KEY") == "sk-or double # not a comment"
    assert envconfig.get("GEMINI_API_KEY") == "AIzaPlain"
    assert envconfig.get("NVIDIA_API_KEY") == "nvapi-exported"
    assert envconfig.get("CHOPIFY_MODELS") == r"D:\Clipping\Chopify\models"
    assert envconfig.get("WIN_QUOTED") == r"C:\Users\me\new folder"
    assert envconfig.get("BLANK") is None and envconfig.get("NOVALUE") is None
    assert envconfig.status()["bad_lines"] == []


@pytest.mark.parametrize("text, raw", [
    ("GROQ_API_KEY=good-value-1234\nthis line is broken \"\nNVIDIA_API_KEY='unterminated\n",
     None),
    (None, b"GROQ_API_KEY=\xff\xfe\x00 not utf-8\n"),
])
def test_malformed_env_never_raises_or_reveals_values(env_file, text, raw):
    env_file(text, raw)
    status = envconfig.status()
    assert envconfig.get("CHOPIFY_AI_FREE_ONLY") is None
    assert policy.load()["free_only"] is True, "strict defaults still apply"
    blob = json.dumps(status)
    assert "good-value" not in blob and "unterminated" not in blob
    if raw is None:
        assert status["bad_lines"] and envconfig.get("GROQ_API_KEY") == "good-value-1234"
    else:
        assert status["error"] and "UTF-8" in status["error"]


def test_env_path_that_is_a_folder_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("CHOPIFY_ENV_FILE", str(tmp_path))
    envconfig.load(force=True)
    assert envconfig.status()["error"] and envconfig.get("GROQ_API_KEY") is None


# ------------------------------------------------------------------ Git

def _git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)


def test_env_and_secret_files_are_ignored_by_git():
    for name in (".env", ".env.local", "sub/.env", "ai_keys.bin", "client_secret_x.json",
                 "youtube_token.bin"):
        assert _git("check-ignore", "-q", name).returncode == 0, name
    assert _git("ls-files", ".env", "ai_keys.bin").stdout.strip() == ""
    assert _git("ls-files", "--cached", "--", "*.env").stdout.strip() == ""


def test_env_example_is_trackable_and_has_no_values():
    assert _git("check-ignore", "-q", ".env.example").returncode == 1
    for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip().endswith("_KEY=") or not line.strip() or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        assert not name.endswith("_KEY"), f"{name} must be empty in the example"


# ---------------------------------------------------------- key safety

def test_keys_never_appear_in_logs(env_file):
    env_file(f"NVIDIA_API_KEY={PLAIN_KEY}\nGROQ_API_KEY={GROQ_KEY}\n")
    applog.info("test", f"oops {PLAIN_KEY} and {GROQ_KEY}", detail=PLAIN_KEY)
    applog.warning("test", "x", url=f"https://x/?key={PLAIN_KEY}")
    raw = "".join(p.read_text(encoding="utf-8") for p in applog.log_dir().glob("*.jsonl"))
    assert PLAIN_KEY not in raw and GROQ_KEY not in raw and "[redacted" in raw


def test_provider_error_messages_never_contain_the_key(env_file, tmp_path):
    env_file(f"GROQ_API_KEY={PLAIN_KEY}\n")

    def http(method, url, headers, body, timeout):
        return 401, {}, json.dumps({"error": {"message": f"Invalid API Key {PLAIN_KEY}"}}).encode()
    settings = policy.load()
    settings["providers"]["groq"]["enabled"] = True
    settings["routes"]["generate"] = {"provider": "groq", "model": "openai/gpt-oss-20b"}
    g = Gateway(ledger=Ledger(tmp_path / "u.db"), http=http, settings=settings)
    with pytest.raises(AuthFailed) as caught:
        g.generate("s", "u", data_kind=SYNTHETIC)
    assert PLAIN_KEY not in str(caught.value)
    with g.ledger._db() as db:
        assert PLAIN_KEY not in json.dumps(db.execute("SELECT * FROM requests").fetchall())


def test_ai_settings_show_only_configured_and_last4(env_file):
    env_file(f"GROQ_API_KEY={GROQ_KEY}\nNVIDIA_API_KEY={PLAIN_KEY}\n")
    data = ai_studio.overview()
    blob = json.dumps(data)
    assert GROQ_KEY not in blob and PLAIN_KEY not in blob
    groq = next(p for p in data["providers"] if p["name"] == "groq")
    assert groq["key"] == {"configured": True, "source": ".env", "env": "GROQ_API_KEY",
                           "last4": GROQ_KEY[-4:]}
    assert data["env"]["exists"] is True and data["env"]["path"].endswith(".env")
    assert data["legacy_keys"]["exists"] is False


@pytest.fixture
def studio(tmp_path):
    import dashboard
    httpd = dashboard.make_server(tmp_path / "projects", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    yield base, httpd
    httpd.shutdown()
    httpd.app.close()
    httpd.server_close()


def _call(base, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, headers={
        "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_keys_never_appear_in_http_responses_and_studio_cannot_store_keys(env_file, studio):
    path = env_file(f"GROQ_API_KEY={GROQ_KEY}\nNVIDIA_API_KEY={PLAIN_KEY}\n")
    before = path.read_bytes()
    base, httpd = studio
    status, body = _call(base, "/api/ai")
    assert status == 200 and GROQ_KEY not in body and PLAIN_KEY not in body
    assert GROQ_KEY[-4:] in body
    headers = {"X-Chopify-Token": httpd.app.token}
    new_key = "gsk_" + "N3wKey00" * 6
    for action in ("key", "key-remove"):
        status, body = _call(base, f"/api/ai/{action}", {"provider": "groq", "key": new_key},
                             headers)
        assert status == 410 and ".env" in body and new_key not in body
    assert path.read_bytes() == before, ".env untouched"
    assert not (youtube_auth.config_dir() / "ai_keys.bin").exists()
    status, body = _call(base, "/api/ai/settings", {"paid_calls": True}, headers)
    assert status == 400 and "CHOPIFY_AI_ALLOW_PAID" in body


def test_keys_never_appear_in_project_json(env_file, tmp_path, monkeypatch):
    env_file(f"GROQ_API_KEY={GROQ_KEY}\n")
    policy.update({"providers": {"groq": {"enabled": True, "zdr_confirmed_at": 1.0}},
                   "routes": {"transcribe": {"provider": "groq",
                                             "model": "whisper-large-v3-turbo"}}}, REGISTRY)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\0" * 10)
    project = projects.init_project(tmp_path / "p1", str(video), {})
    folder = tmp_path / "p1"
    text = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in folder.rglob("*") if p.is_file())
    assert GROQ_KEY not in text and GROQ_KEY[-4:] not in text
    assert "groq:whisper-large-v3-turbo" in text, "the route (not the key) is recorded"
    settings_file = (youtube_auth.config_dir() / policy.SETTINGS_FILE).read_text(encoding="utf-8")
    assert GROQ_KEY not in settings_file
    del project


# ---------------------------------------------------- free-tier defaults

def test_groq_defaults_to_the_free_plan_and_never_assumes_developer(env_file):
    assert policy.load()["providers"]["groq"]["billing"] == "free_plan"
    env_file("CHOPIFY_GROQ_PLAN=developer\n")
    assert policy.load()["providers"]["groq"]["billing"] == "paid"
    env_file("CHOPIFY_GROQ_PLAN=enterprise-ish\n")
    assert policy.load()["providers"]["groq"]["billing"] == "unknown", "unknown = paid"
    with pytest.raises(ValueError, match="CHOPIFY_GROQ_PLAN"):
        ai_studio.apply({"providers": {"groq": {"enabled": True, "billing": "paid"}}})


def test_paid_calls_are_blocked_by_default_and_env_is_a_ceiling(env_file):
    s = policy.load()
    assert (s["free_only"], s["paid_calls"], s["daily_paid_budget_usd"],
            s["monthly_paid_budget_usd"], s["automatic_paid_fallback"],
            s["local_fallback"]) == (True, False, 0.0, 0.0, False, False)
    for change in ({"paid_calls": True}, {"free_only": False}, {"daily_paid_budget_usd": 1}):
        with pytest.raises(ValueError, match="\\.env"):
            ai_studio.apply(change)
    with pytest.raises(ValueError, match="cannot be turned on"):
        policy.update({"automatic_paid_fallback": True}, REGISTRY)
    # Invalid .env values fall back to the strict default.
    env_file("CHOPIFY_AI_FREE_ONLY=maybe\nCHOPIFY_AI_ALLOW_PAID=yes please\n"
             "CHOPIFY_AI_DAILY_BUDGET_USD=-5\n")
    s = policy.load()
    assert s["free_only"] is True and s["paid_calls"] is False and s["daily_paid_budget_usd"] == 0
    # .env alone does not turn paid calls on: Studio must too, within the budgets.
    env_file("CHOPIFY_AI_FREE_ONLY=false\nCHOPIFY_AI_ALLOW_PAID=true\n"
             "CHOPIFY_AI_DAILY_BUDGET_USD=1\nCHOPIFY_AI_MONTHLY_BUDGET_USD=5\n")
    s = policy.load()
    assert s["paid_calls"] is False and s["free_only"] is False
    s = ai_studio.apply({"paid_calls": True, "daily_paid_budget_usd": 0.5})
    assert s["paid_calls"] is True and s["daily_paid_budget_usd"] == 0.5
    with pytest.raises(ValueError, match="cannot exceed"):
        ai_studio.apply({"monthly_paid_budget_usd": 6})
    # Lowering the .env ceiling wins over what Studio saved.
    env_file("CHOPIFY_AI_ALLOW_PAID=false\n")
    s = policy.load()
    assert s["paid_calls"] is False and s["free_only"] is True and s["daily_paid_budget_usd"] == 0


def test_free_tier_rate_limit_pauses_without_switching_provider(env_file, tmp_path):
    calls = []

    def http(method, url, headers, body, timeout):
        calls.append(url)
        return 429, {"retry-after": "3600"}, b'{"error": {"message": "Rate limit reached"}}'
    settings = policy.load()
    settings["local_fallback"] = False
    settings["providers"]["groq"]["enabled"] = True
    settings["routes"]["generate"] = {"provider": "groq", "model": "openai/gpt-oss-20b"}
    g = Gateway(ledger=Ledger(tmp_path / "u.db"), http=http, settings=settings,
                keys=lambda n: GROQ_KEY)
    with pytest.raises(QuotaExhausted, match="free-plan rate limit"):
        g.generate("s", "u", data_kind=SYNTHETIC)
    with pytest.raises(QuotaExhausted, match="not sent"):
        g.generate("s", "u", data_kind=SYNTHETIC)
    assert len(calls) == 1 and all("api.groq.com" in u for u in calls)
    row = g.ledger.summary()[0]
    assert (row["limited"], row["failed"]) == (2, 0)
    assert "rate limit" in g.ledger.last_limited()["groq"]["error"]


def test_live_rate_limit_headers_pause_before_the_limit(tmp_path):
    from ai import http as aihttp
    assert aihttp.parse_reset("2m59.56s") == pytest.approx(179.56)
    assert aihttp.parse_reset("7.66s") == pytest.approx(7.66)
    assert aihttp.parse_reset("1h2m") == 3720 and aihttp.parse_reset("250ms") == 0.25
    assert aihttp.parse_reset("soon") is None
    answer = {"choices": [{"message": {"content": "{}"}}], "usage": {}}
    headers = {"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "0",
               "x-ratelimit-reset-requests": "1m30s", "x-ratelimit-limit-tokens": "8000",
               "x-ratelimit-remaining-tokens": "7000", "x-ratelimit-reset-tokens": "1s"}
    calls = []

    def http(method, url, h, body, timeout):
        calls.append(url)
        return 200, headers, json.dumps(answer).encode()
    settings = policy.load()
    settings["providers"]["groq"]["enabled"] = True
    g = Gateway(ledger=Ledger(tmp_path / "u.db"), http=http, settings=settings,
                keys=lambda n: GROQ_KEY)
    g.generate("s", "u", data_kind=SYNTHETIC, route="groq:openai/gpt-oss-20b")
    assert g.limits["groq"]["remaining_requests"] == 0
    with pytest.raises(QuotaExhausted):
        g.generate("s", "u", data_kind=SYNTHETIC, route="groq:openai/gpt-oss-20b")
    assert len(calls) == 1
    # 429 without Retry-After: wait for the exhausted limit's reset
    assert aihttp._retry_after({"x-ratelimit-remaining-tokens": "0",
                                "x-ratelimit-reset-tokens": "42s"}) == 42


def test_audio_pieces_are_size_checked_and_never_oversized(tmp_path):
    from ai import media
    audio = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "anoisesrc=d=130:c=white", "-ac", "1", "-ar", "16000", str(audio)],
                   check=True)
    with media.audio_pieces(audio, piece_seconds=130, max_bytes=3_000_000) as pieces:
        assert len(pieces) >= 2 and all(p.stat().st_size <= 3_000_000 for _, p, _ in pieces)
        assert sum(s for _, _, s in pieces) == pytest.approx(130, abs=0.5)
    with pytest.raises(media.PieceTooLarge):
        with media.audio_pieces(audio, piece_seconds=130, max_bytes=50_000):
            pass
    assert not list(tmp_path.glob(".ai-pieces-*"))


def test_nvidia_trial_cannot_be_a_project_route(env_file):
    with pytest.raises(ValueError, match="synthetic data only"):
        policy.update({"routes": {"generate": {"provider": "nvidia",
                                               "model": "openai/gpt-oss-20b"}}}, REGISTRY)


# ------------------------------------------------- legacy and OAuth (DPAPI)

def test_legacy_key_file_is_reported_by_name_only_and_never_used(env_file):
    legacy = youtube_auth.config_dir() / secrets.LEGACY_KEYS_FILE
    youtube_auth._write_secret(legacy, {"groq": GROQ_KEY})
    before = legacy.read_bytes()
    info = secrets.legacy_store()
    assert info["exists"] and info["providers"] == ["groq"]
    assert GROQ_KEY not in json.dumps(info) and GROQ_KEY not in json.dumps(ai_studio.overview())
    assert secrets.get("groq") is None, "only .env / environment are read"
    assert legacy.read_bytes() == before, "never changed or deleted"


def test_dpapi_youtube_oauth_tokens_still_work(env_file, tmp_path):
    env_file("GROQ_API_KEY=\n")
    account = youtube_auth.YouTubeAccount(tmp_path / "cfg", clock=lambda: 1000.0)
    account.dir.mkdir(parents=True, exist_ok=True)
    youtube_auth._write_secret(account.client_path, {"client_id": "id.apps.googleusercontent.com",
                                                     "client_secret": "GOCSPX-test"})
    youtube_auth._write_secret(account.token_path, {"access_token": "ya29.test-access",
                                                    "refresh_token": "1//test-refresh",
                                                    "expires_at": 99999.0})
    assert b"ya29.test-access" not in account.token_path.read_bytes(), "still encrypted"
    assert account.access_token() == "ya29.test-access"
    assert account.client()["client_id"] == "id.apps.googleusercontent.com"
