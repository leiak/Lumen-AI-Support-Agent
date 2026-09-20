from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import Response

from channel.enums import ChannelType
from core.config import get_settings
from core.health import aggregate_health, liveness, readiness
from core.logging import configure_logging, get_logger
from core.metrics import prometheus_metrics_middleware, render_metrics
from core.qdrant import close_qdrant_client, get_qdrant_client
from core.redis import close_redis, get_redis
from core.request_context import (
    bind_request_context,
    clear_request_context,
    get_request_id,
)
from knowledge.startup import ensure_image_collection, ensure_qdrant_collection
from llm_client.embeddings import aclose_default_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    settings = get_settings()
    configure_logging()
    log = get_logger("startup")
    log.info("api.starting", environment=settings.environment, service=settings.service_name)
    # Eagerly create the redis client pool so the first request doesn't pay
    # connection-setup latency. (The pool itself connects lazily on first command.)
    get_redis()
    # Ensure the Qdrant collection used for RAG exists. Failures are
    # logged and swallowed by the hook itself so the API still boots
    # when Qdrant is briefly unavailable; /health will surface the
    # degraded state.
    await ensure_qdrant_collection()
    # Stage 17 / M2.B Task 6 — also ensure the multimodal image
    # collection exists. Failures are logged at WARNING (the hook
    # itself never raises) so a flaky Qdrant at boot doesn't block
    # the API; uploads will get a 503 instead of crashing.
    await ensure_image_collection(await get_qdrant_client())
    yield
    # Close the embedding client's singleton AsyncOpenAI so its HTTPX pool
    # is released; safe even if embed_texts was never called (no-op).
    await aclose_default_client()
    await close_redis()
    await close_qdrant_client()
    log.info("api.shutdown")


app = FastAPI(
    title="AI Customer API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — required for the browser-side widget (cross-origin POST to
# /api/v1/widget/token) and the agent workspace SPA. The allowlist is
# the same env-driven list used by the widget WebSocket origin check
# (see ``widget.ws.router._is_origin_allowed``). Credentials are enabled
# because the agent SPA sends a Bearer token via Authorization header
# and we may add cookie-based session auth in Stage 10+.
# IMPORTANT: when ``allow_credentials=True`` starlette rejects
# ``allow_origins=["*"]``; we echo the request Origin back to clients
# whose Origin is in the allowlist. An empty allowlist disables
# cross-origin responses entirely (default-deny).
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.widget_allowed_origins_global,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["Content-Type", "Authorization", "X-Tenant-Id"],
    expose_headers=["Content-Type"],
    max_age=600,
)


@app.middleware("http")
async def request_context_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Stage 11.2: bind a per-request id into structlog contextvars.

    Reads ``X-Request-ID`` from the inbound request (clients / upstream
    proxies use it for end-to-end tracing) or generates a ULID. The id
    is bound via ``bind_request_context`` which both stores it in the
    module-level ``ContextVar`` AND calls
    ``structlog.contextvars.bind_contextvars`` so every log line emitted
    during the request inherits it automatically (the
    ``merge_contextvars`` processor is the first link in the structlog
    chain configured by :func:`core.logging.configure_logging`).

    The id is echoed back in ``X-Request-ID`` so the client can correlate
    a 5xx response with the server-side log search.

    Order: this middleware runs **before** ``metrics_middleware`` so the
    request id is available for log lines emitted from inside the metrics
    middleware (none today, but cheap insurance against future additions).
    """
    inbound = request.headers.get("x-request-id")
    rid = bind_request_context(request_id=inbound)
    try:
        response = await call_next(request)
    finally:
        clear_request_context()
    response.headers["X-Request-ID"] = rid
    return response


@app.middleware("http")
async def metrics_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    return await prometheus_metrics_middleware(request, call_next)


@app.get("/health")
async def health() -> JSONResponse:
    body, all_ok = await readiness()
    # 200 when all components are healthy; 503 when degraded so that load
    # balancers / k8s probes / alerting can detect the failure.
    return JSONResponse(status_code=200 if all_ok else 503, content=body)


@app.get("/health/live")
async def health_live() -> JSONResponse:
    """Stage 11.4: process liveness — never touches deps.

    Used by Kubernetes ``livenessProbe``. Returning 200 means "the
    process is responsive"; a dependency outage MUST NOT cause this
    endpoint to fail (that would restart pods and amplify the outage).
    """
    return JSONResponse(status_code=200, content=liveness())


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    """Stage 11.4: dependency readiness — 503 on degraded.

    Used by Kubernetes ``readinessProbe`` / load-balancer health
    checks. Reflects whether the process can actually serve traffic
    given the current state of its dependencies.
    """
    body, all_ok = await readiness()
    return JSONResponse(status_code=200 if all_ok else 503, content=body)


@app.get("/metrics")
async def metrics() -> Response:
    return render_metrics()


logger = get_logger("email")


from agent.api import router as agents_router  # noqa: E402
from agent.simple_responder import SimpleResponder  # noqa: E402
from agent.ws import router as agents_ws_router  # noqa: E402
from auth.api import router as auth_router  # noqa: E402
from channel.api import router as channels_router  # noqa: E402
from channel.feishu.webhook import router as feishu_webhook_router  # noqa: E402
from channel.inbound_email import handle_email_inbound  # noqa: E402
from channel.models import Channel  # noqa: E402
from channel.outbound_email import EmailOutbound  # noqa: E402
from conversation.api import router as conversations_router  # noqa: E402
from core.database import get_sessionmaker  # noqa: E402
from core.email_parser import parse_ses_inbound  # noqa: E402
from knowledge.api import router as knowledge_router  # noqa: E402
from knowledge.multimodal.api import router as kb_multimodal_router  # noqa: E402
from ticket.api import router as tickets_router  # noqa: E402
from widget.api import router as widget_router  # noqa: E402
from widget.ws.router import router as widget_ws_router  # noqa: E402

app.include_router(agents_router)
app.include_router(agents_ws_router)
app.include_router(auth_router)
app.include_router(channels_router)
app.include_router(conversations_router)
app.include_router(feishu_webhook_router)
app.include_router(knowledge_router)
app.include_router(kb_multimodal_router)
app.include_router(tickets_router)
app.include_router(widget_router)
app.include_router(widget_ws_router)


# Stage 16 / M2.B — SES inbound webhook. Returns 200 for ALL outcomes
# (ok / duplicate / malformed / unknown recipient) so SES stops
# retrying. The handler also fans an AI auto-reply out via the
# configured EmailOutbound client.
_email_outbound = EmailOutbound(
    region=_settings.aws_region,
    from_address=_settings.ses_from_address,
    aws_access_key_id=_settings.aws_access_key_id,
    aws_secret_access_key=_settings.aws_secret_access_key,
)


# KNOWN DEBT (M2.B / Task 2 code review):
# tenant_id is currently sourced from the X-Tenant-ID request header, which
# is trivially spoofable. Production must verify tenant identity via either:
#   (a) SNS subscription confirmation (SES inbound sends to an SNS topic;
#       verify the SigningCertURL against the SNS pinned CA),
#   (b) AWS SigV4 signature verification on the raw request body, or
#   (c) a reverse-lookup from the recipient address (Channel.config_json.tenant_id).
# Tracked as README tech-debt #16 (Stage 19 M2.B close-out).


@app.post("/api/v1/email/inbound")
async def email_inbound_webhook(request: Request):
    """SES inbound webhook. Always returns 200.

    SES retries on 5xx — we MUST return 200 to stop the retry storm,
    even for malformed payloads (logged + dropped). The 4 outcomes:

    * ``ok`` — customer message persisted (and AI auto-reply generated
      when ``ai_handling`` is on).
    * ``duplicate`` — ``email_message_id_header`` already seen (SES
      retry).
    * ``dropped`` — payload was malformed, the recipient ``to_address``
      did not resolve to an EmailChannel for the tenant, or no tenant
      context was supplied.
    """
    payload = await request.body()
    parsed = parse_ses_inbound(payload)
    if parsed is None:
        logger.warning("email.inbound.malformed_payload")
        return {"status": "dropped"}

    # Resolve tenant from X-Tenant-ID header (demo path). Production
    # would look up the tenant via the EmailChannel row's
    # ``config_json["tenant_id"]`` instead.
    tenant_id = request.headers.get("X-Tenant-ID", _settings.default_tenant_id)
    if not tenant_id:
        logger.warning("email.inbound.no_tenant_resolved")
        return {"status": "dropped"}

    # Look up EmailChannel by ``to_address`` for this tenant. The
    # ``address`` lives inside ``Channel.config_json`` JSONB — we
    # compare via ``astext`` so the index can be used.
    sm = get_sessionmaker()
    async with sm() as session:
        channel = (
            await session.execute(
                select(Channel)
                .where(
                    Channel.tenant_id == tenant_id,
                    Channel.type == ChannelType.EMAIL,
                    Channel.config_json["address"].astext == parsed.to_address,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if channel is None:
            # ``to_address`` is OUR OWN SES recipient (the company's
            # support address), not customer PII — safe to log at
            # WARNING for routing triage.
            logger.warning(
                "email.inbound.unknown_recipient",
                extra={"to_address": parsed.to_address},
            )
            return {"status": "dropped"}

    result = await handle_email_inbound(
        tenant_id=tenant_id,
        parsed=parsed,
        channel_id=channel.id,
        responder_factory=lambda: SimpleResponder(),
        outbound=_email_outbound,
    )
    if result is None:
        return {"status": "duplicate"}
    return {"status": "ok", **result.__dict__}