"""Phase 1 AI Gateway: providers, policy, reliability, secrets, integration."""
import json
import socket
import subprocess
from pathlib import Path

import pytest

import applog
import download_and_transcribe as dt
import llm
import projects
import transcription
from ai import gateway as gateway_mod
from ai import policy, secrets
from ai.base import (GENERATE, REAL, SYNTHETIC, TRANSCRIBE, AuthFailed, BudgetExceeded,
                     InvalidOutput, PolicyBlocked, PrivacyBlocked, QuotaExhausted,
                     TemporaryError, Unsupported)
from ai.gateway import Gateway
from ai.usage import Ledger


# ----------------------------------------------------------------- fakes

@pytest.fixture(autouse=True)
def _fresh_provider_caches():
    from ai.providers.openrouter import OpenRouterProvider
    OpenRouterProvider._cache.clear()
    gateway_mod.reset()
    yield
    OpenRouterProvider._cache.clear()
    gateway_mod.reset()


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


class FakeHTTP:
    """Scripted responses per URL substring; records every request."""

    def __init__(self, routes):
        self.routes = {k: list(v) if isinstance(v, list) else v for k, v in routes.items()}
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "body": body})
        for part, answer in self.routes.items():
            if part in url:
                item = answer.pop(0) if isinstance(answer, list) else answer
                if isinstance(item, BaseException):
                    raise item
                status, payload = item[0], item[1]
                headers_out = item[2] if len(item) > 2 else {}
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return status, headers_out, data
        raise AssertionError(f"unexpected request {url}")


def settings_with(**changes):
    s = policy._merge(policy.DEFAULTS, {})
    for key, value in changes.items():
        if key == "providers":
            s["providers"] = policy._merge(s["providers"], value)
        elif key == "routes":
            s["routes"] = policy._merge(s["routes"], value)
        else:
            s[key] = value
    return s


GROQ_ON = {"groq": {"enabled": True, "billing": "free_plan", "zdr_confirmed_at": 1.0}}


def gw(tmp_path, http, keys=None, **settings):
    return Gateway(ledger=Ledger(tmp_path / "usage.db"), http=http, clock=FakeClock(),
                   keys=lambda name: (keys or {}).get(name, f"{name}-test-key-123456"),
                   settings=settings_with(**settings))


def chat(content, usage=(100, 20)):
    return (200, {"choices": [{"message": {"content": content}}],
                  "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "clip.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=5", "-ac", "1", "-ar", "16000", str(path)],
                   check=True)
    return path


def verbose(words, language="hindi"):
    return (200, {"text": " ".join(w for w, _, _ in words), "language": language,
                  "duration": 2.5,
                  "segments": [{"start": 0.0, "end": 2.5, "text": " ".join(w for w, _, _ in words)}],
                  "words": [{"word": w, "start": s, "end": e} for w, s, e in words]})


# ----------------------------------------------------------- Groq: audio

def test_groq_transcribes_in_pieces_with_word_timestamps(tmp_path, audio, monkeypatch):
    from ai import media
    monkeypatch.setattr(media, "PIECE_SECONDS", 2.5)
    http = FakeHTTP({"/audio/transcriptions": [verbose([("नमस्ते", 0.2, 0.8), ("दोस्तों", 0.9, 1.6)]),
                                               verbose([("आज", 0.1, 0.5)])]})
    g = gw(tmp_path, http, providers=GROQ_ON,
           routes={"transcribe": {"provider": "groq", "model": "whisper-large-v3-turbo"}})
    result = g.transcribe(str(audio), offset=100.0)
    words = [(w.word, w.start) for s in result.segments for w in s.words]
    assert words == [("नमस्ते", 100.2), ("दोस्तों", 100.9), ("आज", 102.6)]
    assert result.language == "hi" and result.duration == pytest.approx(5.0, abs=0.1)
    body = http.calls[0]["body"]
    assert b'name="model"\r\n\r\nwhisper-large-v3-turbo' in body
    assert body.count(b'name="timestamp_granularities[]"') == 2
    assert b'name="response_format"\r\n\r\nverbose_json' in body
    assert b"fLaC" in body, "sent as FLAC, not the original file"
    assert http.calls[0]["headers"]["Authorization"] == "Bearer groq-test-key-123456"
    assert not list(audio.parent.glob(".ai-pieces-*")), "temporary pieces removed"
    row = g.ledger.summary()[0]
    assert (row["provider"], row["requests"], row["ok"], row["free"]) == ("groq", 1, 1, True)
    assert row["audio_minutes"] == pytest.approx(5 / 60, abs=0.01)


def test_groq_paid_plan_records_cost(tmp_path, audio):
    http = FakeHTTP({"/audio/transcriptions": verbose([("hi", 0, 1)], "english")})
    g = gw(tmp_path, http, free_only=False, paid_calls=True, daily_paid_budget_usd=1.0,
           monthly_paid_budget_usd=5.0,
           providers={"groq": {"enabled": True, "billing": "paid", "zdr_confirmed_at": 1.0}},
           routes={"transcribe": {"provider": "groq", "model": "whisper-large-v3"}})
    result = g.transcribe(str(audio))
    assert result.usage.free is False
    assert result.usage.cost_usd == pytest.approx(0.111 * 10 / 3600)   # 10 s minimum billed
    assert g.ledger.spent("day") == pytest.approx(result.usage.cost_usd)


# ------------------------------------------------------------ reliability

def test_auth_failures_are_never_retried(tmp_path, audio):
    http = FakeHTTP({"/audio/transcriptions": (401, {"error": {"message": "Invalid API Key"}})})
    g = gw(tmp_path, http, providers=GROQ_ON,
           routes={"transcribe": {"provider": "groq", "model": "whisper-large-v3-turbo"}})
    with pytest.raises(AuthFailed, match="rejected the API key"):
        g.transcribe(str(audio))
    assert len(http.calls) == 1 and g.clock.slept == []


def test_missing_key_is_reported_before_any_request(tmp_path, audio):
    http = FakeHTTP({})
    g = gw(tmp_path, http, keys={"groq": None}, providers=GROQ_ON,
           routes={"transcribe": {"provider": "groq", "model": "whisper-large-v3-turbo"}})
    with pytest.raises(AuthFailed, match="No API key"):
        g.transcribe(str(audio))
    assert http.calls == []


def test_429_retry_after_is_honoured_then_succeeds(tmp_path, audio):
    http = FakeHTTP({"/audio/transcriptions": [
        (429, {"error": {"message": "slow down"}}, {"retry-after": "3"}),
        verbose([("ok", 0, 1)], "english")]})
    g = gw(tmp_path, http, providers=GROQ_ON,
           routes={"transcribe": {"provider": "groq", "model": "whisper-large-v3-turbo"}})
    g.transcribe(str(audio))
    assert g.clock.slept == [3.0] and len(http.calls) == 2


def test_long_rate_limit_becomes_quota_and_blocks_further_sends(tmp_path, audio):
    http = FakeHTTP({"/audio/transcriptions": (429, {"error": "x"}, {"retry-after": "900"})})
    g = gw(tmp_path, http, providers=GROQ_ON,
           routes={"transcribe": {"provider": "groq", "model": "whisper-large-v3-turbo"}})
    with pytest.raises(QuotaExhausted, match="retry in about 900s"):
        g.transcribe(str(audio))
    with pytest.raises(QuotaExhausted, match="wait"):
        g.transcribe(str(audio))
    assert len(http.calls) == 1, "the cooldown stops the second request before sending"


def test_5xx_and_timeouts_are_retried_with_backoff(tmp_path, audio):
    http = FakeHTTP({"/audio/transcriptions": [(503, {"error": "busy"}), socket.timeout(),
                                               verbose([("ok", 0, 1)], "english")]})
    g = gw(tmp_path, http, providers=GROQ_ON,
           routes={"transcribe": {"provider": "groq", "model": "whisper-large-v3-turbo"}})
    g.transcribe(str(audio))
    assert len(http.calls) == 3 and len(g.clock.slept) == 2
    assert g.clock.slept[1] > g.clock.slept[0], "exponential backoff"


def test_persistent_outage_stops_after_max_retries(tmp_path, audio):
    http = FakeHTTP({"/audio/transcriptions": (502, {"error": "down"})})
    g = gw(tmp_path, http, providers=GROQ_ON, max_retries=2,
           routes={"transcribe": {"provider": "groq", "model": "whisper-large-v3-turbo"}})
    with pytest.raises(TemporaryError):
        g.transcribe(str(audio))
    assert len(http.calls) == 3
    assert g.ledger.summary()[0]["failed"] == 1


def test_malformed_json_is_an_invalid_output(tmp_path):
    http = FakeHTTP({"/chat/completions": chat("this is not json")})
    g = gw(tmp_path, http, providers=GROQ_ON,
           routes={"generate": {"provider": "groq", "model": "openai/gpt-oss-20b"}})
    with pytest.raises(InvalidOutput):
        g.generate("system", "user", schema={"type": "object"})
    http = FakeHTTP({"/chat/completions": (200, b"<html>oops</html>")})
    g = gw(tmp_path, http, providers=GROQ_ON,
           routes={"generate": {"provider": "groq", "model": "openai/gpt-oss-20b"}})
    with pytest.raises(InvalidOutput, match="not JSON"):
        g.generate("system", "user")


def test_unsupported_capability_is_rejected_before_sending(tmp_path):
    http = FakeHTTP({})
    g = gw(tmp_path, http, providers=GROQ_ON)
    with pytest.raises(Unsupported):
        g.generate("s", "u", route="groq:whisper-large-v3-turbo")
    with pytest.raises(Unsupported):
        g.analyze_images([b"\xff\xd8jpeg"], "describe", route="groq:openai/gpt-oss-20b")
    assert http.calls == []


# -------------------------------------------------- structured output

STRICT = {"type": "object", "additionalProperties": False, "required": ["score"],
          "properties": {"score": {"type": "number"}}}


def test_structured_output_uses_strict_schema_when_supported(tmp_path):
    http = FakeHTTP({"/chat/completions": [chat('{"score": 8}'), chat('{"score": 7}')]})
    g = gw(tmp_path, http, providers=GROQ_ON,
           routes={"generate": {"provider": "groq", "model": "openai/gpt-oss-20b"}})
    assert g.generate("s", "u", schema=STRICT).data == {"score": 8}
    sent = json.loads(http.calls[0]["body"])
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["strict"] is True
    loose = {"type": "object", "properties": {"score": {"type": "number"}}}
    g.generate("s", "u", schema=loose)
    sent = json.loads(http.calls[1]["body"])
    assert sent["response_format"] == {"type": "json_object"}
    assert "matching this schema" in sent["messages"][0]["content"]


# ------------------------------------------------------ free-only/budgets

def test_free_only_blocks_paid_and_unknown_pricing(tmp_path, audio):
    http = FakeHTTP({})
    for billing in ("paid", "unknown"):
        g = gw(tmp_path, http, providers={"groq": {"enabled": True, "billing": billing,
                                                   "zdr_confirmed_at": 1.0}},
               routes={"transcribe": {"provider": "groq", "model": "whisper-large-v3-turbo"}})
        with pytest.raises(PolicyBlocked, match="Free-only mode is on"):
            g.transcribe(str(audio))
    assert http.calls == []


def test_paid_calls_need_approval_and_a_budget(tmp_path, audio):
    http = FakeHTTP({})
    paid = {"groq": {"enabled": True, "billing": "paid", "zdr_confirmed_at": 1.0}}
    route = {"transcribe": {"provider": "groq", "model": "whisper-large-v3"}}
    g = gw(tmp_path, http, free_only=False, providers=paid, routes=route)
    with pytest.raises(PolicyBlocked, match="Paid AI calls are turned off"):
        g.transcribe(str(audio))
    g = gw(tmp_path, http, free_only=False, paid_calls=True, providers=paid, routes=route)
    with pytest.raises(BudgetExceeded, match="daily AI budget"):
        g.transcribe(str(audio))
    assert http.calls == []
    blocked = g.ledger.summary()[0]
    assert blocked["blocked"] == 2 and blocked["ok"] == 0, "both refusals are recorded"


def test_unknown_price_is_blocked_even_when_paid_calls_are_on(tmp_path):
    http = FakeHTTP({"openrouter.ai/api/v1/models": (200, {"data": [
        {"id": "vendor/mystery", "pricing": {"prompt": "-1", "completion": "-1"}}]})})
    g = gw(tmp_path, http, free_only=False, paid_calls=True, daily_paid_budget_usd=5.0,
           monthly_paid_budget_usd=5.0, providers={"openrouter": {"enabled": True}})
    with pytest.raises(BudgetExceeded, match="no verified price"):
        g.generate("s", "u", data_kind=SYNTHETIC, route="openrouter:vendor/mystery")


# ------------------------------------------------------------- privacy

def test_groq_needs_zdr_confirmation_for_real_recordings(tmp_path, audio):
    http = FakeHTTP({"/audio/transcriptions": verbose([("x", 0, 1)], "english")})
    g = gw(tmp_path, http, providers={"groq": {"enabled": True, "billing": "free_plan"}},
           routes={"transcribe": {"provider": "groq", "model": "whisper-large-v3-turbo"}})
    with pytest.raises(PrivacyBlocked, match="Zero Data Retention"):
        g.transcribe(str(audio))
    assert http.calls == []
    g.transcribe(str(audio), data_kind=SYNTHETIC)        # synthetic test audio is fine
    assert len(http.calls) == 1


def test_gemini_free_and_nvidia_trial_only_get_synthetic_data(tmp_path):
    gemini_answer = (200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}],
                           "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3}})
    http = FakeHTTP({":generateContent": gemini_answer, "integrate.api.nvidia.com":
                     chat('{"ok": true}')})
    g = gw(tmp_path, http, providers={"gemini": {"enabled": True, "tier": "free"},
                                      "nvidia": {"enabled": True}})
    for route in ("gemini:gemini-3.5-flash-lite", "nvidia:meta/llama-3.3-70b-instruct"):
        with pytest.raises(PrivacyBlocked):
            g.generate("s", "real transcript", schema={"type": "object"}, route=route)
    assert http.calls == []
    assert g.generate("s", "synthetic", schema={"type": "object"}, data_kind=SYNTHETIC,
                      route="gemini:gemini-3.5-flash-lite").data == {"ok": True}
    sent = json.loads(http.calls[0]["body"])
    assert sent["generationConfig"]["responseMimeType"] == "application/json"
    assert http.calls[0]["headers"]["x-goog-api-key"] == "gemini-test-key-123456"
    # NVIDIA: each trial model must be approved (model card terms read) first.
    calls = len(http.calls)
    with pytest.raises(PolicyBlocked, match="not approved"):
        g.generate("s", "synthetic", data_kind=SYNTHETIC,
                   route="nvidia:meta/llama-3.3-70b-instruct")
    assert len(http.calls) == calls
    g = gw(tmp_path, http, providers={"nvidia": {"enabled": True,
                                                 "models": ["meta/llama-3.3-70b-instruct"]}})
    g.generate("s", "synthetic", data_kind=SYNTHETIC, route="nvidia:meta/llama-3.3-70b-instruct")
    with pytest.raises(PrivacyBlocked):
        g.generate("s", "real transcript", route="nvidia:meta/llama-3.3-70b-instruct")


def test_gemini_paid_real_data_needs_explicit_approval(tmp_path):
    http = FakeHTTP({})
    g = gw(tmp_path, http, free_only=False, paid_calls=True, daily_paid_budget_usd=1.0,
           monthly_paid_budget_usd=1.0, providers={"gemini": {"enabled": True, "tier": "paid"}})
    with pytest.raises(PrivacyBlocked, match="explicit approval"):
        g.generate("s", "u", route="gemini:gemini-3.5-flash-lite")


def openrouter_routes(zdr_models, price="0"):
    return {"/api/v1/endpoints/zdr": (200, {"data": [{"model_id": m, "safety_retention": False}
                                                     for m in zdr_models]}),
            "/api/v1/models": (200, {"data": [
                {"id": "vendor/model-a:free", "pricing": {"prompt": price, "completion": price},
                 "architecture": {"input_modalities": ["text", "image"]},
                 "supported_parameters": ["structured_outputs", "response_format"]}]}),
            "/chat/completions": chat('{"score": 9}')}


def test_openrouter_enforces_zdr_routing_and_refuses_ineligible_models(tmp_path):
    http = FakeHTTP(openrouter_routes([]))
    g = gw(tmp_path, http, providers={"openrouter": {"enabled": True}})
    with pytest.raises(PrivacyBlocked, match="no Zero Data Retention endpoint"):
        g.generate("s", "u", schema=STRICT, route="openrouter:vendor/model-a:free")
    assert not any("/chat/completions" in c["url"] for c in http.calls)
    http = FakeHTTP(openrouter_routes(["vendor/model-a:free"]))
    g = gw(tmp_path, http, providers={"openrouter": {"enabled": True}})
    from ai.providers.openrouter import OpenRouterProvider
    OpenRouterProvider._cache.clear()
    result = g.generate("s", "u", schema=STRICT, route="openrouter:vendor/model-a:free")
    assert result.data == {"score": 9} and result.usage.free is True
    sent = json.loads(next(c for c in http.calls if "/chat/completions" in c["url"])["body"])
    assert sent["provider"] == {"zdr": True, "data_collection": "deny",
                                "allow_fallbacks": False, "require_parameters": True}
    OpenRouterProvider._cache.clear()


def test_openrouter_routing_error_is_reported_not_rerouted(tmp_path):
    from ai.providers.openrouter import OpenRouterProvider
    OpenRouterProvider._cache.clear()
    routes = openrouter_routes(["vendor/model-a:free"])
    routes["/chat/completions"] = (404, {"error": {"message": "No endpoints found matching "
                                                              "your data policy"}})
    http = FakeHTTP(routes)
    g = gw(tmp_path, http, providers={"openrouter": {"enabled": True}})
    from ai.base import PermanentError
    with pytest.raises(PermanentError, match="data policy"):
        g.generate("s", "u", route="openrouter:vendor/model-a:free")
    assert sum("/chat/completions" in c["url"] for c in http.calls) == 1
    OpenRouterProvider._cache.clear()


# ------------------------------------------------------- local fallback

def test_local_fallback_only_when_enabled_and_never_to_paid(tmp_path, monkeypatch):
    from ai.providers import local
    calls = []
    monkeypatch.setattr(local.LocalProvider, "generate", lambda self, m, s, u, schema=None,
                        images=None: calls.append(m) or __import__("ai.base").base.Generation(
                            "{}", {"local": True}, __import__("ai.base").base.Usage(
                                "local", m, GENERATE)))
    http = FakeHTTP({"/chat/completions": (503, {"error": "down"})})
    g = gw(tmp_path, http, providers=GROQ_ON, max_retries=0,
           routes={"generate": {"provider": "groq", "model": "openai/gpt-oss-20b"}})
    with pytest.raises(TemporaryError):
        g.generate("s", "u", schema={"type": "object"})
    assert calls == [], "fallback is off by default"
    g = gw(tmp_path, http, providers=GROQ_ON, max_retries=0, local_fallback=True,
           routes={"generate": {"provider": "groq", "model": "openai/gpt-oss-20b"}})
    assert g.generate("s", "u", schema={"type": "object"}).data == {"local": True}
    assert calls == ["qwen2.5:3b"]
    providers = {r["provider"] for r in g.ledger.summary()}
    assert providers == {"groq", "local"}, "no other cloud provider was tried"


def test_policy_block_does_not_fall_back_to_another_cloud(tmp_path):
    http = FakeHTTP({})
    g = gw(tmp_path, http, local_fallback=True,
           providers={"groq": {"enabled": True, "billing": "paid", "zdr_confirmed_at": 1.0}},
           routes={"generate": {"provider": "groq", "model": "openai/gpt-oss-20b"}})
    with pytest.raises(PolicyBlocked):
        g.generate("s", "u")
    assert http.calls == []


# -------------------------------------------------------------- secrets

@pytest.mark.parametrize("key", ["gsk_" + "A1b2C3d4" * 6, "sk-or-v1-" + "ab12" * 16,
                                 "nvapi-" + "Xy_9" * 12, "AIza" + "Sy0" * 12])
def test_provider_keys_are_redacted_from_logs(key):
    applog.info("test", f"calling with {key} and Bearer {key}")
    raw = "".join(p.read_text(encoding="utf-8") for p in applog.log_dir().glob("*.jsonl"))
    assert key not in raw


# ------------------------------------------------------- integration

def test_gateway_llm_provider_keeps_judging_unchanged(tmp_path, monkeypatch):
    import highlights
    answer = {"verdict": "keep", "hook": 8, "payoff": 8, "emotion": 7, "standalone": 8,
              "shareability": 8, "reason": "strong moment"}
    http = FakeHTTP({"/chat/completions": [chat(json.dumps(answer)), chat(json.dumps(answer))]})
    g = gw(tmp_path, http, providers=GROQ_ON,
           routes={"generate": {"provider": "groq", "model": "openai/gpt-oss-20b"}})
    monkeypatch.setattr(gateway_mod, "_gateway", g)
    monkeypatch.delenv("CHOPIFY_LLM_DISABLED", raising=False)
    provider = llm.get_provider("gateway", "groq:openai/gpt-oss-20b")
    assert provider.label == "gateway:groq:openai/gpt-oss-20b"
    assert provider.available() == (True, "")
    data = provider.chat_json("judge", "clip text", schema={"type": "object"})
    assert highlights.validate_judgment(data)["verdict"] == "keep"
    # The real judging path, end to end: prompt, schema, validation, cache.
    score = highlights.judge_text(tmp_path, provider, {"start": 10.0, "end": 40.0},
                                  "and then the car flew off the bridge", [], "en")
    expected = highlights.llm_score(highlights.validate_judgment(answer))
    assert score == expected and score > 7
    cached = highlights.load_cache(tmp_path)
    assert any(v.get("verdict") == "keep" for v in cached.values())
    assert llm.get_provider("ollama").label == "ollama:qwen2.5:3b", "local path unchanged"


def test_gateway_llm_provider_reports_unavailable_without_a_key(tmp_path, monkeypatch):
    g = gw(tmp_path, FakeHTTP({}), keys={"groq": None}, providers=GROQ_ON)
    monkeypatch.setattr(gateway_mod, "_gateway", g)
    monkeypatch.delenv("CHOPIFY_LLM_DISABLED", raising=False)
    ok, reason = llm.get_provider("gateway", "groq:openai/gpt-oss-20b").available()
    assert not ok and "No API key" in reason


def test_cloud_route_keeps_the_transcript_format_and_offsets(tmp_path, audio, monkeypatch):
    http = FakeHTTP({"/audio/transcriptions": verbose([("hello", 0.5, 1.0), ("world", 1.1, 1.6)],
                                                      "english")})
    g = gw(tmp_path, http, providers=GROQ_ON)
    monkeypatch.setattr(gateway_mod, "_gateway", g)
    out = dt.transcribe(audio, tmp_path, "groq:whisper-large-v3-turbo", "cloud", "cloud")
    data = json.loads(Path(out).read_text(encoding="utf-8"))
    assert [w["word"] for w in data["words"]] == ["hello", "world"]
    assert data["language"] == "en" and data["model"] == "groq:whisper-large-v3-turbo"
    seg, words, lang, _ = dt._transcribe_media(dt._load_model("groq:whisper-large-v3-turbo",
                                                              "cloud", "cloud"), audio, offset=60)
    assert words[0]["start"] == 60.5 and seg[0]["start"] == 60.0


def test_cloud_transcription_failure_is_clear(tmp_path, audio, monkeypatch):
    from ai.compat import CloudTranscriptionError
    g = gw(tmp_path, FakeHTTP({}), providers={"groq": {"enabled": False}})
    monkeypatch.setattr(gateway_mod, "_gateway", g)
    with pytest.raises(CloudTranscriptionError, match="turned off"):
        dt._load_model("groq:whisper-large-v3-turbo", "cloud", "cloud").transcribe(audio)


def test_project_options_and_cache_identity(tmp_path):
    assert projects.validate_options({})["transcriber"] == "local"
    settings = transcription.plan({"transcriber": "groq:whisper-large-v3-turbo"})
    assert settings["device"] == "cloud" and settings["model"] == "groq:whisper-large-v3-turbo"
    runtime = transcription.Runtime(settings)
    assert runtime.device == "cloud" and runtime.warning is None
    assert dt.fingerprint(settings["model"], "cloud") != dt.fingerprint("medium", "float16"), \
        "cloud and local chunks never mix"
    for bad in ("groq:openai/gpt-oss-20b", "nobody:model", "groq"):
        with pytest.raises(ValueError):
            projects.validate_options({"transcriber": bad})


def test_new_projects_follow_ai_routes_old_projects_do_not(tmp_path):
    policy.update({"providers": {"groq": {"enabled": True}},
                   "routes": {"transcribe": {"provider": "groq", "model": "whisper-large-v3-turbo"},
                              "generate": {"provider": "groq", "model": "openai/gpt-oss-20b"}}},
                  __import__("ai.registry").registry.REGISTRY)
    state = projects.create_project(tmp_path, "https://vimeo.com/1", {})
    assert state["options"]["transcriber"] == "groq:whisper-large-v3-turbo"
    assert (state["options"]["llm_provider"], state["options"]["llm"]) == \
        ("gateway", "groq:openai/gpt-oss-20b")
    old = projects.validate_options({"whisper_model": "medium", "llm": "qwen2.5:3b"})
    assert old["transcriber"] == "local" and old["llm_provider"] == "ollama"


def test_settings_validation_refuses_unsafe_values():
    registry = __import__("ai.registry").registry.REGISTRY
    with pytest.raises(ValueError, match="Automatic paid fallback"):
        policy.update({"automatic_paid_fallback": True}, registry)
    with pytest.raises(ValueError):
        policy.update({"daily_paid_budget_usd": -1}, registry)
    with pytest.raises(ValueError, match="cannot transcribe"):
        policy.update({"routes": {"transcribe": {"provider": "groq",
                                                 "model": "openai/gpt-oss-20b"}}}, registry)
    defaults = policy.load()
    assert (defaults["free_only"], defaults["paid_calls"], defaults["daily_paid_budget_usd"],
            defaults["monthly_paid_budget_usd"], defaults["automatic_paid_fallback"],
            defaults["local_fallback"]) == (True, False, 0.0, 0.0, False, False)
