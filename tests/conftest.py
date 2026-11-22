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
    # Existing tests exercise the (fake) YouTube download paths, which stay in
    # the code for when YouTube grants written approval. They run with the
    # switch on; tests marked ``compliance_default`` see the real default (off).
    if request.node.get_closest_marker("compliance_default"):
        monkeypatch.delenv("CHOPIFY_YOUTUBE_DOWNLOADS", raising=False)
    else:
        monkeypatch.setenv("CHOPIFY_YOUTUBE_DOWNLOADS", "1")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "compliance_default: run with YouTube downloads at their real default (off)")
