"""Every provider Chopify knows. Add a provider by adding its adapter here."""
from ai.providers.gemini import GeminiProvider
from ai.providers.groq import GroqProvider
from ai.providers.local import LocalProvider
from ai.providers.nvidia import NvidiaProvider
from ai.providers.openrouter import OpenRouterProvider

REGISTRY = {cls.name: cls for cls in (LocalProvider, GroqProvider, OpenRouterProvider,
                                      GeminiProvider, NvidiaProvider)}

# What each provider is approved for (shown in Studio).
ROLE = {"local": "Your computer: private, no API cost; needs Ollama / Whisper models.",
        "groq": "Primary cloud transcription candidate. Real recordings only after you "
                "confirm Zero Data Retention.",
        "openrouter": "Alternative text/vision models, always with zdr + data_collection: deny "
                      "and no provider fallback.",
        "gemini": "Evaluation. Free tier: synthetic test data only. Paid: needs your approval.",
        "nvidia": "Trial: internal evaluation with synthetic data only (trial terms)."}
