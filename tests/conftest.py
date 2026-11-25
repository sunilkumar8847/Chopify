"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    # Tests must never call a real Ollama server, even if one is running on
    # the developer's machine. Tests that exercise judging inject a fake
    # provider instead.
    monkeypatch.setenv("CHOPIFY_LLM_DISABLED", "1")


@pytest.fixture(autouse=True)
def _software_encoder(monkeypatch):
    # Deterministic encoder in tests (no GPU needed, no probe encode).
    monkeypatch.setenv("CHOPIFY_ENCODER", "x264")


@pytest.fixture(autouse=True)
def _isolated_youtube_config(monkeypatch, tmp_path_factory):
    # Tests never read or write the real YouTube sign-in or upload queue.
    monkeypatch.setenv("CHOPIFY_CONFIG", str(tmp_path_factory.mktemp("config")))


@pytest.fixture(autouse=True)
def _isolated_logs(monkeypatch, tmp_path_factory):
    # Tests never write to the real logs/ folder.
    monkeypatch.setenv("CHOPIFY_LOG_DIR", str(tmp_path_factory.mktemp("logs")))
    for name in ("CHOPIFY_PROJECT_ID", "CHOPIFY_JOB_ID", "CHOPIFY_REQUEST_ID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _legacy_youtube_paths(monkeypatch, request):
    # YouTube downloads are always on. Older tests also exercise monitoring of
    # other channels (yt-dlp listing), which is opt-in: they run with it on;
    # tests marked ``compliance_default`` see the real default (off).
    monkeypatch.delenv("CHOPIFY_YOUTUBE_DOWNLOADS", raising=False)
    if request.node.get_closest_marker("compliance_default"):
        monkeypatch.delenv("CHOPIFY_YOUTUBE_MONITORING", raising=False)
    else:
        monkeypatch.setenv("CHOPIFY_YOUTUBE_MONITORING", "1")


@pytest.fixture(autouse=True)
def _scheduler_flag(monkeypatch, request):
    # The persistent scheduler (Phase 2) is on by default. Tests written for
    # the legacy in-memory job lanes (still available with the flag off) run
    # with it off; tests marked ``scheduler`` exercise the new default.
    monkeypatch.setenv("CHOPIFY_FEATURE_SCHEDULER",
                       "1" if request.node.get_closest_marker("scheduler") else "0")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "scheduler: run with the Phase 2 persistent scheduler (the default)")
    config.addinivalue_line(
        "markers", "compliance_default: run with external channel monitoring at its real default (off)")


ENV_NAMES = ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "NVIDIA_API_KEY",
             "CHOPIFY_YOUTUBE_API_KEY", "CHOPIFY_AI_FREE_ONLY", "CHOPIFY_AI_ALLOW_PAID",
             "CHOPIFY_AI_DAILY_BUDGET_USD", "CHOPIFY_AI_MONTHLY_BUDGET_USD",
             "CHOPIFY_AI_LOCAL_FALLBACK", "CHOPIFY_GROQ_PLAN")


@pytest.fixture(autouse=True)
def _isolated_env_file(monkeypatch, tmp_path_factory):
    # Tests never read the real .env (your API keys) or provider variables
    # set in Windows; a test that needs a .env writes its own.
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CHOPIFY_ENV_FILE", str(tmp_path_factory.mktemp("env") / ".env"))
    import envconfig
    envconfig.load(force=True)
