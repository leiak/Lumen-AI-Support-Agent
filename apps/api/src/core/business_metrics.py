"""Stage 11.3: business-level Prometheus metrics.

The HTTP middleware in :mod:`core.metrics` already records generic
``http_requests_total`` / ``http_request_duration_seconds`` — those are
infrastructure-shaped and can't answer business questions. This module
adds the three counters stakeholders actually want to graph:

* ``lumen_messages_total{role}`` — conversation throughput by sender
  (customer / agent / ai / system / tool). Driven by
  :meth:`conversation.service.ConversationService.record_message`.
* ``lumen_llm_calls_total{provider, model, outcome}`` — LLM API call
  rate split by provider and whether it succeeded. Driven by
  :class:`llm_client.client.LLMClient` (``chat`` + ``stream_chat``).
* ``lumen_llm_tokens_total{provider, model, direction}`` — token
  consumption split by input vs output. Driven alongside ``llm_calls``
  in the same instrumentation sites.

Label cardinality
-----------------
Deliberately **no** ``tenant_id`` label. With ~hundreds of tenants
expected at M3 scale (and every conversation / every LLM call labelling
once), tenant-id would explode the time-series count past Prometheus'
default 100k cap. If per-tenant accounting is needed later, route it
through the usage table (``llm_usage``) which is the right shape for
billing — Prometheus is for *aggregate* signals.

The label space stays small:

* ``role``: 5 fixed values from :class:`conversation.enums.MessageRole`
* ``provider``: ``anthropic`` / ``openai`` (any future adapter adds one)
* ``model``: 5–10 model names in practice
* ``outcome``: ``success`` / ``rate_limited`` / ``invalid_request`` /
  ``unavailable`` / ``output_invalid``
* ``direction``: ``input`` / ``output``

Total time series ≈ ``5 + providers * models * (1 + 5 + 2)`` which is
well under 200 — Prometheus doesn't even blink.
"""
from __future__ import annotations

from prometheus_client import Counter

MESSAGES_TOTAL = Counter(
    "lumen_messages_total",
    "Conversation messages persisted, by sender role",
    ("role",),
)

LLM_CALLS_TOTAL = Counter(
    "lumen_llm_calls_total",
    "LLM API calls, by provider / model / outcome",
    ("provider", "model", "outcome"),
)

LLM_TOKENS_TOTAL = Counter(
    "lumen_llm_tokens_total",
    "LLM tokens consumed, by provider / model / direction",
    ("provider", "model", "direction"),
)


__all__ = ["LLM_CALLS_TOTAL", "LLM_TOKENS_TOTAL", "MESSAGES_TOTAL"]
