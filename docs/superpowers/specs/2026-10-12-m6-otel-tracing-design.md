# M6 — OpenTelemetry Tracing Design

> Production-readiness + observability milestone. Closes README known limitation #6 ("Plan 10.4 LLM/tracing spans 留 M3") and partially closes #13 ("No chain-warm metrics — only success/failure counts").

**Status:** Spec (brainstorming-approved)
**Date:** 2026-10-12
**Predecessor:** M5.3 at commit `6865596` (tenant LLM config audit log)
**Author:** Brainstorming session with user

---

## 1. Goals

1. **End-to-end distributed tracing** for every LLM / channel / agent / worker / admin path — one `trace_id` ties the full request from inbound HTTP to outbound provider call, including any worker fan-out.
2. **Production-usable backend** — local dev sees traces in Tempo UI; production points the same code at any OTLP-compatible backend (Honeycomb / Tempo Cloud / self-hosted) via env vars alone.
3. **Performance-budgeted** — P95 instrumentation overhead ≤ 0.5ms per request. Verified by a dedicated overhead test that fails the build if regression.
4. **PII-safe by construction** — span attributes follow a closed whitelist. A static-scan test fails the build if a forbidden token (`api_key`, `message.content`, `customer.email`, `customer.name`, `customer.ip`) appears in any `tracer.start_as_current_span(...)` / `set_attribute(...)` call.
5. **Single source of truth for request identity** — the M1 `request_id` middleware becomes a thin pass-through that reads / injects the W3C `traceparent` header and exposes the same `X-Trace-Id` response header. Logs carry the same `trace_id` via structlog correlation.
6. **Hybrid instrumentation** — `httpx` auto-instrumentation captures LLM-provider outbound HTTP for free; manual spans cover business logic with PII-safe attribute control. FastAPI / SQLAlchemy / asyncpg are NOT auto-instrumented (noise + PII risk).

## 2. Non-Goals

- **No OTel metrics migration** — existing Prometheus `lumen_*` counters / histograms stay as-is. Cardinality discipline (zero-tenant-id labels) is preserved by keeping the Prometheus path untouched.
- **No OTel logs migration** — existing `structlog` setup stays. OTel provides `LoggingHandler` to correlate logs with traces, but logs are still emitted to stdout (not via OTLP). Avoid double-emission.
- **No SQLAlchemy / asyncpg auto-instrumentation** — too noisy for the trace UI; can be enabled per-environment later if debugging demands it.
- **No production sampling tuning** — M6 ships `OTEL_TRACES_SAMPLER_ARG=1.0` for dev (catch everything) and `0.1` for prod (10% head-sampling). Tail-based sampling, error-only sampling, etc. are out of scope.
- **No OpenTelemetry Collector config beyond basic batch** — no `tail_sampling` processor, no `memory_limiter` tuning, no `transform` processor. Default batch processor only.
- **No K8s manifest with OTel sidecar** — known limitation #7 stays as-is. M6 is local-dev-first.
- **No long-term retention policy** — dev Tempo = 12h retention. Production retention is an ops decision.
- **No span-based alerting hooks** — M6 ships trace collection; alert wiring is a follow-up.
- **No PII redaction processor in OTel Collector** — PII safety is enforced at the application layer (whitelist + static scan). The collector just passes spans through.
- **No DB query / ORM-level tracing** — out of scope; can revisit if M7+ observability gaps emerge.

## 3. Background

### 3.1 Current observability surface (what's already there)

| Layer | Mechanism | Notes |
|---|---|---|
| Request ID | `apps/api/src/core/middleware.py` (M1) | Per-request ULID, returned as `X-Request-Id` header, threaded into structlog |
| Logging | `structlog` (project-wide) | JSON to stdout, every log carries `request_id` |
| Metrics | Prometheus client + `/metrics` endpoint | `lumen_*` counters / histograms, zero-tenant-id labels (intentional) |
| Health | `/health/live` + `/health/ready` | Split in M1 close-out |
| LLM Gateway | request_id + structlog only | No trace context — provider calls are invisible |
| Workers (Arq) | job name + structlog only | No propagation from HTTP-triggered enqueue |

### 3.2 What's missing

- No way to see a request's full path: inbound HTTP → middleware → channel router → agent runtime → LLM gateway → provider HTTP → response, with per-hop latency.
- No way to correlate "customer reported an error at 14:32" with the LLM call that returned 4xx at 14:32:01.
- No way to attribute cost / latency per-tenant from trace data (the existing Prometheus path deliberately omits tenant_id).
- No chain-warm metrics (known limitation #13) — only success / failure counts on the fallback chain. Latency histograms, error rates, and per-step timing are missing.

### 3.3 Closest existing pattern: `request_id` middleware (M1)

`apps/api/src/core/middleware.py` reads incoming `X-Request-Id` (or generates a ULID), stashes it in a ContextVar, and exposes it on the response. Structlog picks it up via a processor. M6 replaces this with a slightly richer middleware that:

1. Reads W3C `traceparent` header (per OTel spec) and uses the upstream trace_id when present.
2. Falls back to generating a new trace when no header is present.
3. Exposes the trace_id both as `traceparent` (so downstream services can chain) and `X-Trace-Id` (so the SPA / curl users can see it).
4. Drops the standalone `X-Request-Id` header (replaced by `X-Trace-Id`).

The `request_id` ContextVar stays in place (for back-compat with existing structlog output format) but is populated from the same trace_id.

## 4. Architecture

### 4.1 Component layout

```
┌─────────────────────────────────────────────┐
│ apps/api (FastAPI + Arq workers in same image) │
│  ┌──────────────────────────────────────┐  │
│  │ OTel SDK (auto-instrumentation)      │  │
│  │   - httpx  ──► LLM provider spans    │  │
│  │   - asyncio ──► task hierarchy       │  │
│  └──────────────────────────────────────┘  │
│  ┌──────────────────────────────────────┐  │
│  │ Manual spans (business logic)        │  │
│  │   - @traced decorator                │  │
│  │   - start_as_current_span("llm.chat")│  │
│  │   - start_as_current_span("agent.*") │  │
│  └──────────────────────────────────────┘  │
│  ┌──────────────────────────────────────┐  │
│  │ BatchSpanProcessor + OTLP gRPC       │  │
│  └──────────────────────────────────────┘  │
└──────────────┬──────────────────────────────┘
               │ OTLP gRPC
               ▼
┌─────────────────────────────────────────────┐
│ OTel Collector (docker-compose)             │
│   receivers: otlp (4317)                    │
│   processors: batch                         │
│   exporters: otlp → Tempo                   │
└──────────────┬──────────────────────────────┘
               │ OTLP gRPC
               ▼
┌─────────────────────────────────────────────┐
│ Tempo (docker-compose)                      │
│   port 3100 (HTTP query API + UI)           │
│   storage: local FS, 12h retention          │
└─────────────────────────────────────────────┘
```

### 4.2 Container topology (docker-compose addition)

Two new services, no changes to existing 13:

```yaml
# infra/docker-compose.yml (additions)
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.107.0
    command: ["--config=/etc/otelcol/config.yaml"]
    volumes:
      - ./deploy/otel-collector-config.yaml:/etc/otelcol/config.yaml:ro
    ports:
      - "4317:4317"   # OTLP gRPC ingest
      - "4318:4318"   # OTLP HTTP ingest
    depends_on: [tempo]
    restart: unless-stopped

  tempo:
    image: grafana/tempo:2.5.0
    command: ["-config.file=/etc/tempo.yaml"]
    volumes:
      - ./deploy/tempo.yaml:/etc/tempo.yaml:ro
      - tempo-data:/var/tempo
    ports:
      - "3100:3100"   # HTTP + UI
    restart: unless-stopped

volumes:
  tempo-data:
```

- `deploy/otel-collector-config.yaml`: `otlp` receiver → `batch` processor → `otlp{tempo backend}` exporter. ~30 lines.
- `deploy/tempo.yaml`: `server.http_listen_port: 3100` + `storage.trace.backend: local` + `storage.trace.local.path: /var/tempo/blocks` + retention 12h. ~20 lines.

### 4.3 SDK initialization

`apps/api/src/observability/__init__.py`:

```python
"""OpenTelemetry tracing setup for the Lumen API + workers.

The SDK is auto-configured via standard ``OTEL_*`` env vars (see
``deploy/env/.env.example``). The two things we explicitly do here:

1. Wire ``opentelemetry-instrumentation-httpx`` so outbound HTTP
   to LLM providers is traced for free.
2. Register a custom ``LoggingHandler`` so structlog picks up
   ``trace_id`` + ``span_id`` on every log line.

Sampling, exporter endpoint, and resource attributes are all
controlled via env — no code changes needed to point at a
different backend.
"""
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# HTTPXClientInstrumentor is idempotent — safe to call multiple times.
HTTPXClientInstrumentor().instrument()

# TracerProvider is auto-set by opentelemetry-distro when
# OTEL_TRACES_EXPORTER=otlp, so we don't set it manually. We do
# fetch the global tracer so import-time usage works.
tracer = trace.get_tracer("lumen")
```

Loaded once at app startup via `apps/api/src/main.py` (single `import observability` line added near the existing logger / middleware setup). Workers load the same module on their own entrypoint.

## 5. Instrumentation strategy

### 5.1 Hybrid model

| Layer | Mechanism | Span name pattern | Why |
|---|---|---|---|
| FastAPI | **manual** (root span in middleware) | `http.request` | One per request; attributes carry `http.method`, `http.route`, `http.status_code`. Avoid FastAPI auto-instrumentation's per-route noise. |
| LLM Gateway | **manual** (per call) | `llm.chat` | The crown jewel — provider / model / tokens / cost / latency. PII-safe whitelist. |
| LLM Provider HTTP | **httpx auto** | `POST api.anthropic.com` (or similar) | Auto-instrumented; child of `llm.chat`. Shows real wire latency / status. |
| Agent Runtime (LangGraph) | **manual** (per node) | `agent.node.{name}` | Each LangGraph node gets a span. Tool calls are children (`agent.tool.{name}`). |
| Channel Gateway | **manual** (per inbound msg) | `channel.inbound` / `channel.outbound` | Inbound = receive from widget/email/Feishu; outbound = write response. |
| Ticket Service | **manual** (`@traced`) | `ticket.{method}` | CRUD wrappers via decorator. |
| Admin REST | **manual** (`@traced`) | `admin.{resource}.{action}` | Mutation paths get `@traced`; pure read endpoints can be left bare. |
| Arq workers | **manual** (per job) | `worker.{job_name}` | `enqueue()` injects `traceparent`; worker `extract()` makes the job a child of the HTTP request. |
| SQLAlchemy | **❌ NOT instrumented** | — | Too noisy; defer. |
| asyncpg / DB driver | **❌ NOT instrumented** | — | Same. |
| `asyncio` | **NOT instrumented** in M6 | — | Considered but deferred — task hierarchy is helpful but adds many short spans. Re-evaluate in M7+. |

### 5.2 `@traced` decorator

File: `apps/api/src/observability/tracing.py`

```python
def traced(
    name: str | None = None,
    *,
    attributes: dict[str, AttributeValue] | None = None,
    record_exception: bool = True,
) -> Callable:
    """Open a child span around a function call.

    - Uses the current OTel context (auto-attaches as child of the
      active span — works inside FastAPI handlers, worker jobs,
      nested @traced calls).
    - Sets attributes from the decorator's static dict (the caller
      is responsible for keeping them on the PII whitelist).
    - Records exceptions and sets ``status=ERROR`` on raise, if
      ``record_exception`` is True (default).
    """
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            with tracer.start_as_current_span(
                name or fn.__qualname__,
                attributes=attributes or {},
            ) as span:
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    if record_exception:
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR))
                    raise
        return async_wrapper
    return deco
```

Use:

```python
from observability.tracing import traced

class TicketService:
    @traced("ticket.create", attributes={"ticket.priority": "normal"})
    async def create(self, ...):
        ...
```

For methods where attributes depend on runtime args, use the manual pattern:

```python
async def call_llm(self, request, *, tenant_id):
    with tracer.start_as_current_span(
        "llm.chat",
        attributes={"tenant_id": tenant_id, "model": request.model},
    ) as span:
        response = await provider.achat(request)
        span.set_attribute("llm.tokens.output", response.usage.output_tokens)
        return response
```

### 5.3 Manual span pattern (LLM Gateway)

The full span attribute set (PII-safe whitelist — see §6):

```python
with tracer.start_as_current_span(
    "llm.chat",
    attributes={
        "tenant_id": tenant_id,                       # ULID
        "provider_name": provider.name,               # "anthropic" / "openai" / "minimax"
        "model": request.model,                       # "claude-3-5-sonnet-20241022"
        "llm.tokens.input": request.max_tokens or 0,  # estimate
    },
) as span:
    t0 = time.perf_counter()
    response = await provider.achat(request)
    span.set_attribute("llm.tokens.output", response.usage.output_tokens)
    span.set_attribute("llm.cost_usd", cost_calc(response.usage))
    span.set_attribute("llm.latency_ms", (time.perf_counter() - t0) * 1000)
    return response
```

The `httpx` auto-instrumentation automatically emits a child span for the outbound HTTP call to `api.anthropic.com` / `api.openai.com` / etc. — that span carries the real wire status code, byte counts, and DNS / TCP latency.

## 6. PII discipline

### 6.1 Whitelist (allowed span attributes)

- `tenant_id` (ULID only)
- `provider_name` ("anthropic" / "openai" / "minimax")
- `model`
- `llm.tokens.input` / `llm.tokens.output`
- `llm.cost_usd` (numeric)
- `llm.latency_ms` (numeric)
- `http.method`, `http.route`, `http.status_code`
- `channel.name` ("widget" / "rest" / "email" / "feishu")
- `agent.id` (agent ULID)
- `conversation.id` (conversation ULID)
- `ticket.id` (ticket ULID)
- `worker.job_name` (e.g. "qa_judge")
- `worker.attempt` (1, 2, 3 for retries)

### 6.2 Forbidden (NEVER in any span attribute or event)

- `api_key` (in any form, including SHA-256 hash)
- `message.content` / `message.role` / `messages.*`
- `customer.email` / `customer.name` / `customer.phone`
- `customer.ip` / `customer.user_agent`
- Anything matching `password` / `secret` / `token` (broad net for future fields)

### 6.3 Enforcement

`tests/observability/test_pii_whitelist.py` is a **static scan** that:

1. Greps `apps/api/src/` for calls to `tracer.start_as_current_span` / `span.set_attribute` / `span.add_event` / `@traced(`.
2. For each match, looks for any forbidden token (`api_key`, `message.content`, `customer.email`, etc.) in the same line or the surrounding 5 lines.
3. **Fails the build** if any match is found. The test is annotated with a clear message naming the file:line and the offending token.

This is a coarse but effective net. It catches accidental additions at code-review time and CI.

For dynamic enforcement (e.g. a request accidentally pushes message content into a span attribute), the implementation is responsible — there's no way to whitelist attributes at the OTel API level. The PII discipline relies on code review + the static scan.

## 7. Trace context propagation

### 7.1 Inbound (HTTP)

The OTel SDK auto-instrumentation for HTTPX covers outbound. For inbound (the FastAPI middleware), M6 uses a small custom middleware that:

1. Reads the `traceparent` header (W3C Trace Context spec).
2. If present, extracts the trace_id / span_id / flags via `opentelemetry.propagate.extract()`.
3. If absent, generates a new trace (the SDK does this automatically when no parent context is active).
4. Opens a `http.request` span with attributes from §6.1.
5. Exposes `trace_id` on the response as `X-Trace-Id` header.

The middleware replaces the M1 `request_id` middleware (kept as a thin pass-through that stashes `trace_id` in the `request_id` ContextVar for back-compat with existing structlog format).

### 7.2 Outbound (provider HTTP)

Auto-instrumented. The `httpx` client inside `AnthropicProvider` / `OpenAIProvider` is patched by `HTTPXClientInstrumentor().instrument()` at SDK init. Spans are emitted with the parent context (the `llm.chat` span).

### 7.3 Worker (Arq)

Arq doesn't auto-propagate context. M6 does it manually:

```python
# Enqueue side
from opentelemetry import trace, propagate

ctx = {}
propagate.inject(ctx)  # adds "traceparent" to ctx
await arq_pool.enqueue_job("qa_judge", job_ctx=ctx, ...)

# Worker side
async def qa_judge(ctx, job_ctx, ...):
    from opentelemetry import propagate
    parent_ctx = propagate.extract(job_ctx or {})
    with attach_context(parent_ctx):  # makes subsequent spans children
        # ... actual job logic
```

The propagation is a single dict key (`traceparent`); Arq's `job_ctx` passes through naturally.

### 7.4 Frontend correlation

The FastAPI middleware adds `X-Trace-Id` to every response. The admin SPA / widget reads it and:

- Stamps it on error reports
- Includes it in the "report a bug" modal
- Surfaces it in the dev console (`console.info("Trace:", traceId)`)

This is a thin frontend change; not a full M6 deliverable but a one-line JS add to the existing API client.

## 8. Test strategy

### 8.1 Test pyramid

| Layer | File | Count (est) | Purpose |
|---|---|---|---|
| Unit | `tests/observability/test_traced_decorator.py` | 8 | @traced behavior on normal / exception / nested paths |
| Unit | `tests/observability/test_pii_whitelist.py` | 1 (parametrized) | Static scan; fails on any forbidden token in src |
| Unit | `tests/observability/test_overhead.py` | 3 | P95 overhead ≤ 0.5ms; runs 1000 wrapped vs unwrapped |
| Unit | `tests/observability/test_trace_context_propagation.py` | 6 | W3C `traceparent` round-trip + flag handling |
| Integration | `tests/observability/test_otel_sdk_wiring.py` | 5 | SDK init / shutdown, exporter config, resource attributes |
| Integration | `tests/llm_client/test_llm_gateway_creates_span.py` | 4 | LLM call produces `llm.chat` span + httpx child |
| Integration | `tests/agent/test_langgraph_node_spans.py` | 3 | Per-node + per-tool spans with correct parent |
| Integration | `tests/admin/test_admin_traced.py` | 3 | @traced wrapping admin mutations |
| Integration | `tests/worker/test_arq_traceparent_propagation.py` | 3 | enqueue → worker inherits trace_id |
| E2E | `tests/e2e/scripts/test_otel_e2e.py` | 6 steps | docker-compose up, send request, query Tempo, assert spans |
| Explore | `tests/e2e/scripts/explore.py` Round 12 | 1 | Real widget → LLM flow traced end-to-end |

**Total:** ~37 new tests.

### 8.2 Test infrastructure: `InMemorySpanExporter`

`tests/observability/conftest.py` provides a `memory_exporter` fixture that swaps in `InMemorySpanExporter` for the test session. Tests assert on `memory_exporter.get_finished_spans()` — fast, no network.

### 8.3 E2E flow (`tests/e2e/scripts/test_otel_e2e.py`)

1. Check `otel-collector:4317` + `tempo:3100` are reachable.
2. Send `POST /api/v1/chat` with a `traceparent` header.
3. Poll Tempo's `/api/search?tags=trace_id=<id>` until the trace appears (max 5s).
4. Assert the trace contains:
   - 1 `http.request` span (the inbound request)
   - 1 `llm.chat` span
   - 1 `POST api.anthropic.com` (or similar) httpx span
   - All parent links are correct (no orphans)
5. Assert each span's attributes are on the whitelist (defense in depth — the static scan doesn't catch runtime dynamic attributes).
6. Assert no forbidden token appears in any span's attributes or events.

Exit codes 0/1/2 (matches project convention).

### 8.4 Explore Round 12

Adds a round to `tests/e2e/scripts/explore.py` that runs the real widget → agent → LLM flow against the docker stack, then queries Tempo for the resulting trace. The round asserts the trace structure and prints a Tempo UI link so the operator can spot-check the visualization.

## 9. Environment variables

`deploy/env/.env.example` additions:

```bash
# OpenTelemetry tracing (M6)
OTEL_SERVICE_NAME=lumen-api
OTEL_RESOURCE_ATTRIBUTES=service.namespace=lumen,deployment.environment=dev
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=1.0
OTEL_PYTHON_LOG_CORRELATION=true
```

Production: change `OTEL_EXPORTER_OTLP_ENDPOINT` to the real backend, set `OTEL_TRACES_SAMPLER_ARG=0.1` (10%).

## 10. Production deployment

- M6 ships dev-only (docker-compose + Tempo). Production deployment is "point env at the real backend."
- Sampling defaults: dev = 1.0, prod = 0.1. Tunable per-environment.
- Retention: 12h (dev), production is ops-driven (Honeycomb has its own; Tempo Cloud has its own; self-hosted needs config).
- No new infra requirements: any OTLP-compatible backend works.

## 11. Rollback

Three escape hatches, no code change needed:

1. **Disable SDK at boot** — set `OTEL_SDK_DISABLED=true`. SDK init is a no-op; decorators + manual spans become pass-through (no span emission, no overhead). Code stays in tree.
2. **Turn off sampling** — set `OTEL_TRACES_SAMPLER=always_off`. SDK runs but emits no spans.
3. **Remove collector / Tempo containers** — `docker compose stop otel-collector tempo`. SDK attempts to export to `otel-collector:4317` and gets connection refused; BatchSpanProcessor retries with backoff and drops after timeout. Spans lost, no application impact.

In all three cases, the application continues to function. The only visible effect: traces stop appearing in Tempo.

## 12. Task breakdown (estimated 7 commits)

1. **M6.1 — Foundation**: OTel SDK init + docker-compose collector/Tempo + env vars + structlog correlation. README tech debt #6 partially closed. No business-code changes yet.
2. **M6.2 — @traced + test infrastructure**: decorator + `InMemorySpanExporter` + PII static scan test + overhead test. No business-code changes.
3. **M6.3 — LLM Gateway + Provider spans**: manual `llm.chat` spans in `LLMGateway` / `TenantResolver` / `BudgetResolver` + httpx auto-instrumentation. 4 integration tests.
4. **M6.4 — Agent + Channel + Ticket + Admin spans**: per-LangGraph-node spans, channel inbound/outbound, @traced on TicketService, @traced on admin mutations. 9 integration tests.
5. **M6.5 — Worker trace context**: Arq enqueue/extract helpers, per-job spans, retry attempt attribute. 3 integration tests.
6. **M6.6 — Middleware upgrade**: `request_id` middleware → trace_id middleware; `X-Trace-Id` response header; frontend single-line consumption in API client. 6 unit tests.
7. **M6.7 — E2E + Round 12 + README + memory**: E2E script + Explore Round 12 + README row 32 (new slot) + tech debt #6/#13 close-out + memory file.

Each task is one commit; spec calls for 7 commits total.

## 13. Verification

End-to-end test sequence:

```bash
# 1. Bring up stack (now includes otel-collector + tempo)
scripts/bring-up.sh

# 2. Run all M6 tests
cd apps/api && uv run pytest \
    tests/observability/ \
    tests/llm_client/test_llm_gateway_creates_span.py \
    tests/agent/test_langgraph_node_spans.py \
    tests/admin/test_admin_traced.py \
    tests/worker/test_arq_traceparent_propagation.py \
    -v

# 3. E2E
python tests/e2e/scripts/test_otel_e2e.py --api-base http://localhost:8000

# 4. Manual: send a request and view the trace
curl -X POST http://localhost:8000/api/v1/chat \
    -H "traceparent: 00-aaaa-bbbb-01" \
    -H "Content-Type: application/json" \
    -d '{"tenant_id": "...", "messages": [{"role": "user", "content": "hi"}]}' \
    | jq -r '._headers["X-Trace-Id"]'
# → open http://localhost:3100 in browser, paste trace_id, see the full trace

# 5. Explore Round 12
python tests/e2e/scripts/explore.py --rounds 12
```

Pass criteria:

- All 37 new tests pass.
- The PII static scan passes (no forbidden tokens in src).
- The overhead test passes (P95 overhead ≤ 0.5ms).
- E2E `test_otel_e2e.py` exits 0 and finds the expected spans in Tempo.
- Manual curl shows the full request trace in Tempo UI with PII-safe attributes only.
- `test_traced_decorator.py` exception path test confirms `status=ERROR` is set on raise.

## 14. Deferred (out of M6 scope)

- OTel metrics / logs exporter — explicitly out (§2). Prometheus + structlog stay.
- SQLAlchemy / asyncpg auto-instrumentation — noise, defer.
- Tail-based sampling (errors always sampled, success sampled at 10%) — out of scope; revisit if 10% head sampling loses important traces in prod.
- K8s manifest with OTel sidecar — known limitation #7; defer.
- Long-term retention policy — dev = 12h, prod is ops-driven.
- Span-based alerting hooks — collect first, alert later.
- OTel Collector `tail_sampling` / `transform` / `memory_limiter` processors — default `batch` only.
- Cross-service trace correlation (e.g. Web Widget in browser → OTel JS SDK → backend) — would require browser OTel SDK + JS bundle changes. Out of M6 scope; the `traceparent` header is forwarded by the SDK but browser-side instrumentation is a follow-up.
- DB query latency attribution (via SQLAlchemy spans) — defer.
- PII redaction in OTel Collector (redaction processor) — defer; rely on application-layer whitelist + static scan.

---

**End of M6 spec.** Ready for writing-plans skill → implementation.
