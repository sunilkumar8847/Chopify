"""Google Gemini API (generateContent): evaluation adapter.

Verified 2026-11-22 (aistudio.google.com/docs: models, pricing, terms):
* Free tier ("Unpaid Services"): Google uses submitted content and responses to
  improve its products, and human reviewers may read them. So on the free tier
  Chopify sends synthetic test data only - never recordings, frames or project
  text.
* Paid tier: content is not used to improve products. Real data still needs
  the user's explicit approval in AI settings, plus paid calls and a budget.
* Stable IDs used here: gemini-3.5-flash-lite ($0.30 in / $2.50 out per 1M
  tokens) and gemini-3.1-flash-lite ($0.25 / $1.50). JSON output via
  generationConfig.responseMimeType + responseJsonSchema; images as inlineData.
"""
import base64
import json
import time

from ai import http as aihttp
from ai.base import GENERATE, VISION, Generation, InvalidOutput, ModelInfo, Provider, Usage

API = "https://generativelanguage.googleapis.com/v1beta"
PRICES = "https://aistudio.google.com/docs/pricing"


class GeminiProvider(Provider):
    name = "gemini"
    title = "Gemini"
    key_env = "GEMINI_API_KEY"
    terms_url = "https://aistudio.google.com/docs/terms"

    def catalogue(self):
        verified = "2026-11-22"
        return {
            "gemini-3.5-flash-lite": ModelInfo("gemini-3.5-flash-lite", (GENERATE, VISION),
                                               0.30, 2.50, source=PRICES, verified=verified),
            "gemini-3.1-flash-lite": ModelInfo("gemini-3.1-flash-lite", (GENERATE, VISION),
                                               0.25, 1.50, source=PRICES, verified=verified),
        }

    def _headers(self, extra=None):
        return {"x-goog-api-key": self.key or "", **(extra or {})}

    def is_free(self, model_id):
        return self.settings.get("tier", "free") == "free"

    def real_media_allowed(self, model_id=None):
        if self.settings.get("tier", "free") == "free":
            return False, ("Gemini's free tier may use submitted content to improve Google "
                           "products and lets human reviewers read it, so only synthetic "
                           "test data is sent to it.")
        if not self.settings.get("real_media_approved_at"):
            return False, ("Real recordings need your explicit approval for paid Gemini in "
                           "AI settings.")
        return True, ""

    def health(self):
        _, _, body = self._request("GET", f"{API}/models?pageSize=50", timeout=20)
        names = {m.get("name", "").split("/")[-1]
                 for m in aihttp.json_body(self.name, body).get("models") or []}
        missing = [m for m in self.models if m not in names]
        return {"ok": len(missing) < len(self.models),
                "detail": "models listed" if not missing else f"not listed: {', '.join(missing)}"}

    def generate(self, model_id, system, user, schema=None, images=None):
        self.model(model_id)
        parts = [{"text": user}] + [
            {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(img).decode()}}
            for img in (images or [])[:4]]
        payload = {"systemInstruction": {"parts": [{"text": system}]},
                   "contents": [{"role": "user", "parts": parts}],
                   "generationConfig": {"temperature": 0}}
        if schema is not None:
            payload["generationConfig"].update(responseMimeType="application/json",
                                               responseJsonSchema=schema)
        started = time.time()
        _, _, body = self._request("POST", f"{API}/models/{model_id}:generateContent",
                                   {"Content-Type": "application/json"},
                                   json.dumps(payload).encode("utf-8"))
        data = aihttp.json_body(self.name, body)
        try:
            text = "".join(p.get("text", "") for p in
                           data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as exc:
            raise InvalidOutput("Gemini returned no answer.", self.name) from exc
        meta = data.get("usageMetadata") or {}
        tokens_in = int(meta.get("promptTokenCount") or 0)
        tokens_out = int(meta.get("candidatesTokenCount") or 0)
        free = self.is_free(model_id)
        cost = 0.0 if free else (self.estimate_cost(GENERATE, model_id, input_tokens=tokens_in,
                                                    output_tokens=tokens_out) or 0.0)
        usage = Usage(self.name, model_id, VISION if images else GENERATE,
                      latency_ms=int((time.time() - started) * 1000), input_tokens=tokens_in,
                      output_tokens=tokens_out, images=len(images or []), cost_usd=cost,
                      free=free)
        parsed = None
        if schema is not None:
            import llm
            try:
                parsed = llm.parse_json(text)
            except llm.LLMError as exc:
                raise InvalidOutput("Gemini did not return valid JSON.", self.name) from exc
        return Generation(text, parsed, usage)
