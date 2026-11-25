"""NVIDIA API Catalog (trial): internal evaluation with synthetic data only.

NVIDIA API Trial Terms of Service (read 2026-11-22):
* 1.2 / 1.4: access is "for limited trial purposes only and without use of the
  API Service or Generated Content in production"; without a subscription it
  may be used "for internal testing and evaluation purposes, not in production".
* 3.3(iv): NVIDIA collects "User Content and Generated Content to improve
  NVIDIA products and services, including AI models".
* 2.6(a): no confidential or personal data.
So Chopify never sends real recordings or project text here and never uses its
output in production. The adapter stays isolated so a production agreement
could enable it later (``production_agreement`` setting; off).
OpenAI-compatible chat: https://integrate.api.nvidia.com/v1, Bearer key.
"""
import json
import time

from ai.base import GENERATE, ModelInfo, Provider
from ai.providers.groq import chat_result

API = "https://integrate.api.nvidia.com/v1"


class NvidiaProvider(Provider):
    name = "nvidia"
    title = "NVIDIA API Catalog (trial)"
    key_env = "NVIDIA_API_KEY"
    terms_url = ("https://assets.ngc.nvidia.com/products/api-catalog/legal/"
                 "NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf")

    def catalogue(self):
        verified = "2026-11-22"
        src = "https://docs.api.nvidia.com/nim/reference/llm-apis"
        return {m: ModelInfo(m, (GENERATE,), source=src, verified=verified)
                for m in ("meta/llama-3.3-70b-instruct", "openai/gpt-oss-20b",
                          "nvidia/nemotron-3.5-lightning-30b-a3b")}

    def _headers(self, extra=None):
        return {"Authorization": f"Bearer {self.key}", **(extra or {})}

    def is_free(self, model_id):
        return True                                # trial credits; no charge

    def real_media_allowed(self, model_id=None):
        return False, ("NVIDIA's trial terms allow internal evaluation only and let NVIDIA "
                       "use submitted content, so only synthetic test data is sent.")

    def health(self):
        _, _, body = self._request("GET", f"{API}/models", timeout=20)
        return {"ok": True, "detail": "key valid (trial: evaluation only)"}

    def generate(self, model_id, system, user, schema=None, images=None, **hints):
        self.model(model_id)
        if schema is not None:
            system += "\nAnswer with one JSON object matching this schema:\n" + json.dumps(schema)
        payload = {"model": model_id, "temperature": 0, "max_tokens": 1024,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}]}
        started = time.time()
        _, _, body = self._request("POST", f"{API}/chat/completions",
                                   {"Content-Type": "application/json"},
                                   json.dumps(payload).encode("utf-8"))
        return chat_result(self, model_id, body, started, schema is not None)
