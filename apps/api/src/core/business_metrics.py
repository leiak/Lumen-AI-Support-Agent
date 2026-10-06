"""Stage 11.3: business-level Prometheus metrics.

The HTTP middleware in :mod:`core.metrics` already records generic
``http_requests_total`` / ``http_request_duration_seconds`` — those are
infrastructure-shaped and can't answer business questions. This module
adds the three counters stakeholders actually want to graph:

* ``lumen_messages_total{role}`` — conversation throughput by sender
  (customer / agent / ai / system / tool). Driven by
  :meth:`conversation.service.ConversationService.record_message`.
* ``lumen_llm_calls_total{provider, model, route_mode, outcome}`` — LLM
  API call rate split by provider and whether it succeeded. Driven by
  :class:`llm_client.client.LLMClient` (``chat`` + ``stream_chat``).
* ``lumen_llm_tokens_total{provider, model, route_mode, direction}`` —
  token consumption split by input vs output. Driven alongside
  ``llm_calls`` in the same instrumentation sites.

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
* ``route_mode``: ``auto`` / ``pinned`` / ``unknown_model`` /
  ``resolver_error`` (4 enum values)
* ``outcome``: ``success`` / ``rate_limited`` / ``invalid_request`` /
  ``unavailable`` / ``output_invalid``
* ``direction``: ``input`` / ``output``

Total time series ≈ ``5 + providers * models * route_modes * (1 + 5 + 2)``
which is ≈ 3000 — still well under Prometheus' default 100k cap.
"""
from __future__ import annotations

from prometheus_client import Counter, Histogram

MESSAGES_TOTAL = Counter(
    "lumen_messages_total",
    "Conversation messages persisted, by sender role",
    ("role",),
)

LLM_CALLS_TOTAL = Counter(
    "lumen_llm_calls_total",
    "LLM API calls, by provider / model / route_mode / outcome",
    ("provider", "model", "route_mode", "outcome"),
)

LLM_TOKENS_TOTAL = Counter(
    "lumen_llm_tokens_total",
    "LLM tokens consumed, by provider / model / route_mode / direction",
    ("provider", "model", "route_mode", "direction"),
)

# M4.B — fallback chain per-step observability.
#
# Distinct from ``lumen_llm_calls_total`` which records the OUTCOME of
# the whole chain (single provider label = the winner). This counter
# fires once per STEP regardless of chain success, so dashboards can
# see "step 0 failed 5 times today" without needing to correlate with
# the call outcome. Labels:
#   provider: gateway-registered name
#   model:    the step's pinned model (may differ from request.model)
#   step:     0-based index in the chain
#   outcome:  success / provider_unavailable / output_invalid /
#             rate_limited / timeout
# Cardinality ≈ providers × models × chain_length × 5 outcomes.
LLM_FALLBACK_ATTEMPTS_TOTAL = Counter(
    "lumen_llm_fallback_attempts_total",
    "Per-step fallback chain attempts, by provider / model / step / outcome.",
    ("provider", "model", "step", "outcome"),
)

# M4.C — Tenant LLM not-configured counter (zero-label by design).
# Spec §8.5: tenant_id stays in logs only to keep cardinality bounded
# and avoid PII leakage via metric scraping.
LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL = Counter(
    "lumen_llm_tenant_not_configured_total",
    "Number of LLM calls rejected because the tenant has no enabled provider configs.",
)

# M4.D — Tenant budget enforcement counters (zero-label by design).
# tenant_id stays in log lines only to keep cardinality bounded and
# avoid PII leakage via metric scraping. Mirrors the M4.C discipline
# for LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL.
LLM_TENANT_BUDGET_EXCEEDED_TOTAL = Counter(
    "lumen_llm_tenant_budget_exceeded_total",
    "Number of LLM calls rejected because the tenant reached its monthly hard cap.",
)
LLM_TENANT_BUDGET_SOFT_WARN_TOTAL = Counter(
    "lumen_llm_tenant_budget_soft_warn_total",
    "Number of soft-warn events fired when a tenant crossed soft_warn_tokens.",
)

# M4.D Pack A — auto-cleanup observability.
LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL = Counter(
    "lumen_budget_cleanup_rows_deleted_total",
    "Number of tenant_budget_snapshots rows deleted by the cleanup task.",
)


# Stage 14 / Task 7 — real-time QA judge metrics.
#
# Cardinality budget (matches the docstring at the top of this file):
#
# * lumen_qa_scores_total{dimension, bucket}
#     dimension: relevance / safety / faithfulness / overall (4)
#     bucket: low / medium / high (3) => 12 series
# * lumen_qa_flagged_total{} — unlabelled, 1 series
# * lumen_qa_judge_failures_total{reason}
#     reason: timeout / malformed / exception / structured_output_invalid (4)
# * lumen_sla_breached_total{priority}
#     priority: low / normal / high / urgent (4) => 4 series
# * lumen_qa_judge_latency_seconds — Histogram, no labels, ~12 buckets
#
# Total: ~20 series. Well under Prometheus' default 100k cap.

LUMEN_QA_SCORES = Counter(
    "lumen_qa_scores_total",
    "QA judge scores by dimension and bucket (low/medium/high).",
    ("dimension", "bucket"),
)

LUMEN_QA_FLAGGED = Counter(
    "lumen_qa_flagged_total",
    "AI messages flagged because at least one dimension < threshold.",
)

LUMEN_QA_FAILURES = Counter(
    "lumen_qa_judge_failures_total",
    "Judge LLM call failures (timeout / malformed / exception / structured_output_invalid).",
    ("reason",),
)

LUMEN_SLA_BREACHED = Counter(
    "lumen_sla_breached_total",
    "Tickets that breached SLA without resolution.",
    ("priority",),
)

LUMEN_QA_SCORE_LATENCY = Histogram(
    "lumen_qa_judge_latency_seconds",
    "Time spent in Judge LLM call (seconds).",
    buckets=(0.5, 1.0, 2.0, 5.0, 10.0),
)


__all__ = [
    "LLM_BUDGET_CLEANUP_ROWS_DELETED_TOTAL",
    "LLM_CALLS_TOTAL",
    "LLM_FALLBACK_ATTEMPTS_TOTAL",
    "LLM_TENANT_BUDGET_EXCEEDED_TOTAL",
    "LLM_TENANT_BUDGET_SOFT_WARN_TOTAL",
    "LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL",
    "LLM_TOKENS_TOTAL",
    "LUMEN_QA_FAILURES",
    "LUMEN_QA_FLAGGED",
    "LUMEN_QA_SCORE_LATENCY",
    "LUMEN_QA_SCORES",
    "LUMEN_SLA_BREACHED",
    "MESSAGES_TOTAL",
]
