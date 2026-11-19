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
