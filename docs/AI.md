# AI Gateway (cloud-first, provider-independent)

Chopify's AI work - transcription, text/JSON generation (clip judging,
titles, Hinglish polishing) and image analysis - goes through one gateway
(`ai/`). Business logic asks for a capability; the gateway picks the route
from AI settings, applies privacy and spending rules, retries safely and
records usage. Nothing changes until you configure a cloud provider: the
default routes are the existing local models.

```
highlights / publish_meta / hinglish ── llm.get_provider("gateway", "groq:openai/gpt-oss-20b")
transcription / download_and_transcribe ── _load_model(route, "cloud") -> CloudWhisperModel
                     │
                ai.gateway.Gateway ── policy.check ── quota cooldown ── _send (retries)
                     │                                         │
       providers: local | groq | openrouter | gemini | nvidia   usage ledger (SQLite)
```

## Providers (verified 2026-11-22)

| Provider | Used for | Real recordings | Pricing source |
|---|---|---|---|
| Local | faster-whisper, Ollama `qwen2.5:3b` | Yes (on this PC) | free |
| Groq | `whisper-large-v3-turbo` ($0.04/h), `whisper-large-v3` ($0.111/h), `openai/gpt-oss-20b` ($0.075/$0.30 per 1M), `openai/gpt-oss-120b` ($0.15/$0.60), `qwen/qwen3.8-27b` (vision, preview) | Only after you confirm Zero Data Retention | console.groq.com/docs/models |
| OpenRouter | any listed text/vision model | Only for models with a listed ZDR endpoint; every request sends `zdr: true`, `data_collection: deny`, `allow_fallbacks: false` | `/api/v1/models` (live) |
| Gemini | `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` | Free tier: never (Google may use content and humans may review it). Paid: only with your approval | aistudio.google.com/docs/pricing |
| NVIDIA API Catalog | trial models | Never (trial terms: evaluation only; NVIDIA may use content) | trial credits |

Groq offers Zero Data Retention in Console > Settings > Data Controls but has
no API that reports it, so Chopify cannot verify it: you confirm it in AI
settings, the time is recorded, and you can withdraw it.

## Policy (defaults)

`free_only = true`, paid calls off, daily and monthly paid budgets $0,
automatic paid fallback impossible, local fallback off. Before anything is
sent: provider enabled -> model allowed and able -> data kind allowed by the
provider's privacy rules -> free/paid status (unknown price = paid) -> budget
-> quota cooldown. Blocked requests are recorded but never sent. Groq's free
plan counts as free only when you select "Free plan" in settings.

Fallback goes only to local models, only when you enable it, only for
outages (5xx, timeouts, rate limits). Never to another cloud or a paid model.

## Reliability

Timeouts, exponential backoff for temporary errors, `Retry-After` for 429
(up to `max_rate_limit_wait_seconds`, then "quota" with a cooldown so nothing
else is sent), no retries for auth/permission/invalid requests, two
concurrent requests per provider, request IDs in logs and the ledger.

## Keys and privacy

Keys are stored with Windows DPAPI in `%APPDATA%\Chopify\ai_keys.bin` (same
scheme as the YouTube token) or read from `GROQ_API_KEY`,
`OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `NVIDIA_API_KEY`. Studio only shows
"configured", the source and the last four characters. Logs redact `gsk_`,
`sk-or-v1-`, `nvapi-`, `AIza` keys, `Bearer` tokens and `key=` parameters.
The ledger (`ai_usage.db`) stores counts, never media, prompts or keys.
Cloud transcription sends 16 kHz mono FLAC pieces of at most 10 minutes;
images are downscaled JPEGs.

## Projects

Saved projects keep `transcriber: local` and their LLM settings. New
projects record the AI settings' routes when created (`transcriber:
"groq:whisper-large-v3-turbo"`, `llm_provider: "gateway"`), so changing
settings later never re-transcribes or re-judges older projects. Cloud and
local transcripts have different cache identities and never mix.

## Benchmarks

```
python -m ai.benchmark transcription --audio projects/<id>/audio.webm \
    --reference projects/<id>/captions.en-orig.json3 --start 2900 --seconds 300 \
    --routes local:medium groq:whisper-large-v3-turbo groq:whisper-large-v3
python -m ai.benchmark judging --project projects/<id> --baseline local:qwen2.5:3b \
    --routes groq:openai/gpt-oss-20b
python -m ai.benchmark synthetic --routes gemini:gemini-3.5-flash-lite
```

Reports go to `D:\Clipping\Chopify\_bench`. Routes that cannot run are listed
as unavailable with the reason. Roman Hinglish is pending a reference
recording.
