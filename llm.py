"""Configurable LLM providers for clip judging (local Ollama by default).

A provider answers two questions:
    available()                 -> (ok: bool, reason: str)
    chat_json(system, user, schema) -> parsed JSON (dict/list) or raises LLMError
    embed(texts)                -> list of vectors or raises LLMError

Only Ollama ships today. Another provider (a cloud API, llama.cpp, ...) can be
added by subclassing ``Provider`` and registering it in ``PROVIDERS``; nothing
else in Chopify needs to change. No API keys are read or stored here.

Set CHOPIFY_LLM_DISABLED=1 to force every provider to report unavailable
(the automated tests do this so they never touch a real model).
"""
import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_PROVIDER = "ollama"
DEFAULT_MODEL = "qwen2.5:3b"
OLLAMA_HOST = "http://127.0.0.1:11434"


class LLMError(RuntimeError):
    """The model could not be reached or returned unusable output."""


class Provider:
    name = "base"

    def __init__(self, model, host=None, timeout=180):
        self.model = model
        self.host = host
        self.timeout = timeout

    @property
    def label(self):
        return f"{self.name}:{self.model}"

    def available(self):
        return False, "No provider configured."

    def chat_json(self, system, user, schema=None):
        raise LLMError("Not implemented.")

    def embed(self, texts, model=None):
        raise LLMError("Not implemented.")


def _disabled():
    return os.environ.get("CHOPIFY_LLM_DISABLED", "").strip() not in {"", "0"}


def parse_json(text):
    """Parse model output that should be JSON, tolerating code fences/prose."""
    if isinstance(text, (dict, list)):
        return text
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    raise LLMError("The model did not return valid JSON.")


class OllamaProvider(Provider):
    """Local Ollama server (https://ollama.com). Free, offline, no keys."""
    name = "ollama"

    def __init__(self, model, host=None, timeout=180):
        super().__init__(model, host or os.environ.get(
            "CHOPIFY_OLLAMA_HOST", OLLAMA_HOST), timeout)
        self._models = None

    def _request(self, path, payload=None, timeout=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.host.rstrip("/") + path, data=data,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(
                    request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError) as exc:  # URLError is an OSError
            raise LLMError(f"Ollama request failed: {exc}") from exc

    def models(self):
        if self._models is None:
            tags = self._request("/api/tags", timeout=3)
            self._models = {item.get("name", "") for item in tags.get("models") or []}
        return self._models

    @staticmethod
    def _matches(wanted, installed):
        if wanted in installed:
            return True
        # "qwen2.5:3b" == "qwen2.5:3b", "nomic-embed-text" == "...:latest"
        return ":" not in wanted and f"{wanted}:latest" in installed

    def available(self):
        if _disabled():
            return False, "LLM judging is disabled (CHOPIFY_LLM_DISABLED)."
        try:
            installed = self.models()
        except LLMError:
            return False, (f"Ollama is not running at {self.host}. Install it "
                           "from ollama.com and start it.")
        if not self._matches(self.model, installed):
            return False, (f"Model '{self.model}' is not installed. Run: "
                           f"ollama pull {self.model}")
        return True, ""

    def chat_json(self, system, user, schema=None):
        payload = {
            "model": self.model, "stream": False,
            # A JSON schema makes Ollama constrain the output (structured
            # outputs); plain "json" is the fallback for older servers.
            "format": schema or "json",
            "options": {"temperature": 0, "seed": 7, "num_ctx": 8192},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        response = self._request("/api/chat", payload)
        content = (response.get("message") or {}).get("content", "")
        return parse_json(content)

    def embed(self, texts, model=None):
        model = model or self.model
        if _disabled():
            raise LLMError("LLM features are disabled.")
        try:
            if not self._matches(model, self.models()):
                raise LLMError(f"Embedding model '{model}' is not installed.")
        except LLMError:
            raise
        response = self._request("/api/embed", {
            "model": model, "input": list(texts), "truncate": True})
        vectors = response.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise LLMError("Embedding model returned the wrong number of vectors.")
        return vectors


class GatewayProvider(Provider):
    """Text generation through the AI Gateway (Phase 1).

    ``model`` is a route such as "groq:openai/gpt-oss-20b"; an empty model
    uses the AI settings' generate route. Judging, metadata and Hinglish
    polishing call ``chat_json`` exactly as with Ollama.
    """
    name = "gateway"

    def __init__(self, model, host=None, timeout=180):
        super().__init__(model, host, timeout)

    @property
    def label(self):
        return f"gateway:{self.model}"

    def available(self):
        if _disabled():
            return False, "LLM judging is disabled (CHOPIFY_LLM_DISABLED)."
        from ai import gateway
        return gateway.get().precheck("generate", self.model or None)

    def chat_json(self, system, user, schema=None):
        from ai import gateway
        from ai.base import AIError
        try:
            result = gateway.get().generate(system, user, schema=schema or {"type": "object"},
                                            route=self.model or None)
        except AIError as exc:
            raise LLMError(str(exc)) from exc
        if result.data is None:
            raise LLMError("The model did not return valid JSON.")
        return result.data

    def embed(self, texts, model=None):
        raise LLMError("Embeddings stay on the local Ollama model.")


PROVIDERS = {"ollama": OllamaProvider, "gateway": GatewayProvider}


def get_provider(name=None, model=None, host=None, timeout=180):
    name = (name or DEFAULT_PROVIDER).lower()
    if name not in PROVIDERS:
        raise ValueError(f"Unknown LLM provider '{name}'. "
                         f"Available: {', '.join(sorted(PROVIDERS))}")
    return PROVIDERS[name](model or DEFAULT_MODEL, host=host, timeout=timeout)
