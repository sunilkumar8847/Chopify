"""Provider-independent AI gateway (Phase 1).

``gateway.get()`` returns the process-wide Gateway. Business logic never talks
to a provider directly: it asks the gateway for a capability (transcribe,
generate, vision) and gets a normalized result, after the policy has checked
privacy, free-only mode, budgets and quota. See docs/AI.md.
"""
