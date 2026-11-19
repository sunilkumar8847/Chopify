"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    # Tests must never call a real Ollama server, even if one is running on
    # the developer's machine. Tests that exercise judging inject a fake
    # provider instead.
    monkeypatch.setenv("CHOPIFY_LLM_DISABLED", "1")
