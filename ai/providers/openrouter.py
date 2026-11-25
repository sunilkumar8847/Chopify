"""OpenRouter: many text/vision models behind one API, with privacy routing.

Verified 2026-11-22 (openrouter.ai/docs: provider-routing, zdr, models,
structured-outputs, limits):
* Every request carries provider preferences {"zdr": true, "data_collection":
  "deny", "allow_fallbacks": false, "require_parameters": true}; OpenRouter
  refuses the request when no endpoint satisfies them (no silent reroute).
* Real data is only sent when the model has an endpoint in the public ZDR list
  (GET /api/v1/endpoints/zdr) that is not "safety retention".
* Prices come from GET /api/v1/models (USD per token, strings); ":free" models
  price prompt and completion at "0". Unknown price = treated as paid.
"""
import base64
import json
import re
import threading
import time

from ai import http as aihttp
from ai.base import GENERATE, VISION, ModelInfo, Provider
from ai.providers.groq import chat_result

API = "https://openrouter.ai/api/v1"
MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*/[A-Za-z0-9._:-]+$")
CACHE_SECONDS = 3600
PRIVACY = {"zdr": True, "data_collection": "deny", "allow_fallbacks": False,
           "require_parameters": True}


class OpenRouterProvider(Provider):
    name = "openrouter"
    title = "OpenRouter"
    key_env = "OPENROUTER_API_KEY"
    terms_url = "https://openrouter.ai/docs/features/privacy-and-logging"
    _cache = {}
    _cache_lock = threading.Lock()

    @classmethod
    def catalogue_supports(cls, capability, model_id):
        return capability in (GENERATE, VISION) and bool(MODEL_ID.match(str(model_id or "")))

    def _headers(self, extra=None):
        headers = {"X-Title": "Chopify", **(extra or {})}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        return headers

    def _cached(self, name, url, auth=False):
        with self._cache_lock:
            hit = self._cache.get(name)
            if hit and time.time() - hit[0] < CACHE_SECONDS:
                return hit[1]
        _, _, body = self._request("GET", url, timeout=30)
        data = aihttp.json_body(self.name, body).get("data") or []
        with self._cache_lock:
            self._cache[name] = (time.time(), data)
        return data

    def catalogue(self):
        return {}                                  # filled lazily from the live model list

    def _info(self, model_id):
        if model_id in self.models:
            return self.models[model_id]
        if not MODEL_ID.match(str(model_id or "")):
            return None
        try:
            listing = {m.get("id"): m for m in self._cached("models", f"{API}/models")}
        except Exception:                          # noqa: BLE001 - unknown, priced as paid
            listing = {}
        entry = listing.get(model_id)
        if entry is None:
            return None
        pricing = entry.get("pricing") or {}

        def per_mtok(value):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return None if number < 0 else number * 1e6
        prompt, completion = per_mtok(pricing.get("prompt")), per_mtok(pricing.get("completion"))
        modalities = ((entry.get("architecture") or {}).get("input_modalities") or [])
        params = entry.get("supported_parameters") or []
        info = ModelInfo(model_id, (GENERATE, VISION) if "image" in modalities else (GENERATE,),
                         prompt, completion, strict_json="structured_outputs" in params,
                         free=(prompt == 0 and completion == 0),
                         source=f"{API}/models", verified=time.strftime("%Y-%m-%d"))
        self.models[model_id] = info
        return info

    def supports(self, capability, model_id):
        info = self._info(model_id)
        return bool(info and capability in info.capabilities)

    def model(self, model_id):
        info = self._info(model_id)
        if info is None:
            from ai.base import Unsupported
            raise Unsupported(f"OpenRouter does not list '{model_id}'.", self.name)
        return info

    def is_free(self, model_id):
        info = self._info(model_id)
        if info is None or info.price_input_per_mtok is None:
            return None
        return bool(info.free)

    def real_media_allowed(self, model_id=None):
        try:
            endpoints = self._cached("zdr", f"{API}/endpoints/zdr")
        except Exception as exc:                   # noqa: BLE001
            return False, f"OpenRouter's Zero Data Retention list could not be checked ({exc})."
        eligible = [e for e in endpoints if (e.get("model_id") or e.get("id")) == model_id
                    and not e.get("safety_retention")]
        if not eligible:
            return False, (f"OpenRouter lists no Zero Data Retention endpoint for '{model_id}', "
                           "so real recordings are not sent to it.")
        return True, ""

    def health(self):
        _, _, body = self._request("GET", f"{API}/key", timeout=20)
        data = aihttp.json_body(self.name, body).get("data") or {}
        free = data.get("free_model_daily_requests") or {}
        return {"ok": True, "detail": f"key valid; free-model requests left today: "
                                      f"{free.get('remaining', 'unknown')}"}

    def generate(self, model_id, system, user, schema=None, images=None, **hints):
        info = self.model(model_id)
        content = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}}
                for img in images[:4]]
        payload = {"model": model_id, "temperature": 0, "provider": dict(PRIVACY),
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": content}]}
        if schema is not None:
            if info.strict_json:
                payload["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "chopify", "strict": True, "schema": schema}}
            else:
                payload["messages"][0]["content"] = (
                    system + "\nAnswer with one JSON object matching this schema:\n"
                    + json.dumps(schema))
        started = time.time()
        _, _, body = self._request("POST", f"{API}/chat/completions",
                                   {"Content-Type": "application/json",
                                    "X-OpenRouter-Metadata": "enabled"},
                                   json.dumps(payload).encode("utf-8"))
        served = ""
        try:
            meta = json.loads(body.decode("utf-8")).get("openrouter_metadata") or {}
            served = next((e.get("provider") for e in (meta.get("endpoints") or {})
                           .get("available") or [] if e.get("selected")), "") or ""
        except (ValueError, AttributeError):
            pass
        return chat_result(self, model_id, body, started, schema is not None,
                           len(images or []), served_by=served)
