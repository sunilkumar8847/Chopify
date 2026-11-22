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
| Groq (free plan by default) | `whisper-large-v3-turbo` (first test candidate) ($0.04/h), `whisper-large-v3` ($0.111/h), `openai/gpt-oss-20b` ($0.075/$0.30 per 1M), `openai/gpt-oss-120b` ($0.15/$0.60), `qwen/qwen3.8-27b` (vision, preview) | Only after you confirm Zero Data Retention | console.groq.com/docs/models |
| OpenRouter | any listed text/vision model | Only for models with a listed ZDR endpoint; every request sends `zdr: true`, `data_collection: deny`, `allow_fallbacks: false` | `/api/v1/models` (live) |
| Gemini | `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` | Free tier: never (Google may use content and humans may review it). Paid: only with your approval | aistudio.google.com/docs/pricing |
| NVIDIA API Catalog | trial models, synthetic tests only | Never (trial terms: evaluation only; NVIDIA may use content) | trial credits |

Groq offers Zero Data Retention in Console > Settings > Data Controls but has
no API that reports it, so Chopify cannot verify it: you confirm it in AI
settings, the time is recorded, and you can withdraw it.

## Configuration: the `.env` file

All provider API keys and the spending limits live in one file:

    D:\Clipping\Chopify\chopify\.env

It is the `.env` next to `dashboard.py`, whatever folder you start Chopify
from. Start from the template: `copy .env.example .env`.

```dotenv
GROQ_API_KEY=gsk_...            # leave empty for providers you do not use
OPENROUTER_API_KEY=
GEMINI_API_KEY=
NVIDIA_API_KEY=
CHOPIFY_YOUTUBE_API_KEY=        # optional, public Data API lookups
CHOPIFY_AI_FREE_ONLY=true
CHOPIFY_AI_ALLOW_PAID=false
CHOPIFY_AI_DAILY_BUDGET_USD=0
CHOPIFY_AI_MONTHLY_BUDGET_USD=0
CHOPIFY_AI_LOCAL_FALLBACK=false
CHOPIFY_GROQ_PLAN=free          # free | developer
```

* **Restart Studio after editing `.env`**: stop it (Ctrl+C in its terminal),
  then run `.venv\Scripts\python dashboard.py` again. Studio and its workers read the file
  once at start.
* A Windows environment variable with the same name wins over `.env`.
* Values may be unquoted, `'single'` or `"double"` quoted; `#` starts a
  comment outside quotes. Write Windows paths unquoted or in single quotes
  (double quotes treat `\n` as an escape).
* Studio shows each key only as "configured", its source and the last four
  characters. Studio cannot save, change or remove keys: edit `.env`.
* A missing `.env` means no keys; a broken line is ignored and Studio says
  which line numbers could not be read (never their contents).
* `.env` values are kept inside Chopify, not copied into the process
  environment, so ffmpeg, yt-dlp and Ollama never inherit your keys.
* The spending values are defaults **and ceilings**: Studio can be stricter
  than `.env`, never looser. A paid call needs `CHOPIFY_AI_FREE_ONLY=false`,
  `CHOPIFY_AI_ALLOW_PAID=true` and a budget in `.env`, **and** the Studio
  switch. Invalid values count as the strict default.

The YouTube sign-in is different: its OAuth client and the rotating access
and refresh tokens stay encrypted with Windows DPAPI in `%APPDATA%\Chopify`
(`youtube_client.bin`, `youtube_token.bin`) and are never put in `.env`.
Phase 1's `ai_keys.bin` is no longer read; if it exists Studio says so (by
provider name only) and leaves it untouched.

### Check that Git ignores it

```powershell
git check-ignore .env          # prints .env
git ls-files .env              # prints nothing
git status --short             # .env is never listed
```

**Never commit** `.env`, `.env.*` (except `.env.example`), `ai_keys.bin`,
`client_secret*.json`, `youtube_*.bin`, tokens, or any file holding a key.
`.env.example` must keep empty values.

### Groq free account

1. Create a key at console.groq.com > API Keys; put it in `GROQ_API_KEY`.
2. Keep `CHOPIFY_GROQ_PLAN=free`. Chopify never assumes the Developer plan
   and never upgrades anything; `developer` makes Groq count as paid (blocked
   while free-only is on), anything unrecognised counts as unknown (blocked).
3. Turn on Zero Data Retention in Groq Console > Settings > Data Controls,
   then tick and confirm it in Studio's AI panel. Groq has no API that reports
   this setting, so Chopify cannot verify it: until you confirm, nothing real
   is sent to Groq. You can withdraw the confirmation at any time.
4. Free-plan limits are the account's own: Chopify reads Groq's
   `x-ratelimit-*` headers and `Retry-After`, pauses before a limit is hit,
   and when a limit is reached it stops that work with "Groq free-plan rate
   limit reached" (shown in the AI panel and counted as *limited* in usage).
   It never switches to another cloud or a paid provider.
5. Audio goes up as 16 kHz mono FLAC pieces; every piece's size is checked
   against the free-plan 25 MB limit before upload (split smaller if needed,
   refused if it still does not fit). Word timestamps are kept and the
   transcript has the same structure as local Whisper's.

### NVIDIA trial

Synthetic test data and internal evaluation only (trial terms). It can never
be a route for projects, never receives real recordings and is never a
fallback. Each trial model must be listed under "Approved trial models" in
its Studio card, and only after you read that model's card terms on
build.nvidia.com.

### Who may receive real recordings

Local always; Groq only after the ZDR confirmation; OpenRouter only models
with a listed ZDR endpoint; Gemini paid only with explicit approval;
Gemini Free and NVIDIA never.

## Policy (defaults)

`free_only = true`, paid calls off, daily and monthly paid budgets $0,
automatic paid fallback impossible, local fallback off (all from `.env`). Before anything is
sent: provider enabled -> model allowed and able -> data kind allowed by the
provider's privacy rules -> free/paid status (unknown price = paid) -> budget
-> quota cooldown. Blocked requests are recorded but never sent. Groq counts
as free only while `CHOPIFY_GROQ_PLAN=free` (the default).

Fallback goes only to local models, only when you enable it, only for
outages (5xx, timeouts, rate limits). Never to another cloud or a paid model.

## Reliability

Timeouts, exponential backoff for temporary errors, `Retry-After` for 429
(up to `max_rate_limit_wait_seconds`, then "quota" with a cooldown so nothing
else is sent), no retries for auth/permission/invalid requests, two
concurrent requests per provider, request IDs in logs and the ledger.

## Keys and privacy

Keys come only from `.env` or the environment (see above). Studio only shows
"configured", the source and the last four characters. Logs redact the exact
values of every `*_KEY`/`*_SECRET`/`*_TOKEN` variable plus `gsk_`,
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

## Live validation (2026-11-22, Groq Free plan)

Synthetic sample only: 28.8 s of Windows TTS English speech with a known
script (`D:\Clipping\Chopify\_phase1v`). Every existing project's media came
from YouTube, so none was sent to a cloud provider.

| Route | WER vs script | Words | Language | Latency | Cost |
|---|---|---|---|---|---|
| `groq:whisper-large-v3-turbo` | 0.043 (numbers written as digits) | 70 | en | 1.4 s | $0 (free plan) |
| `groq:whisper-large-v3` | 0.043 | 70 | en | 1.3 s | $0 |
| `local:medium` (GPU) | 0.057 | 74 | en | 13.5 s | local |
| `groq:openai/gpt-oss-20b` strict JSON judgement | valid, schema-conformant | 301 in / 332 out tokens | - | 0.9 s | $0 |

Word timestamps monotonic on all routes; the ledger recorded each request
(ok, free, audio minutes, tokens). Live limits seen from Groq's headers for
`gpt-oss-20b`: 1000 requests/day, 8000 tokens/minute. Hindi and Roman
Hinglish cloud accuracy remain unmeasured until an authorized recording is
available. After validation the default routes for **new** projects are
`groq:whisper-large-v3-turbo` (transcription) and `groq:openai/gpt-oss-20b`
(text); existing projects keep their local settings; local fallback stays off.
