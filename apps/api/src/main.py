from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.responses import Response

from core.config import get_settings
from core.health import aggregate_health
from core.logging import configure_logging, get_logger
from core.metrics import prometheus_metrics_middleware, render_metrics
from core.qdrant import close_qdrant_client
from core.redis import close_redis, get_redis
from knowledge.startup import ensure_qdrant_collection
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
async def metrics_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    return await prometheus_metrics_middleware(request, call_next)


@app.get("/health")
async def health() -> JSONResponse:
    body, all_ok = await aggregate_health()
    # 200 when all components are healthy; 503 when degraded so that load
    # balancers / k8s probes / alerting can detect the failure.
    return JSONResponse(status_code=200 if all_ok else 503, content=body)


@app.get("/metrics")
async def metrics() -> Response:
    return render_metrics()


from agent.api import router as agents_router  # noqa: E402
from agent.ws import router as agents_ws_router  # noqa: E402
from auth.api import router as auth_router  # noqa: E402
from channel.api import router as channels_router  # noqa: E402
from channel.feishu.webhook import router as feishu_webhook_router  # noqa: E402
from conversation.api import router as conversations_router  # noqa: E402
from knowledge.api import router as knowledge_router  # noqa: E402
from widget.api import router as widget_router  # noqa: E402
from widget.ws.router import router as widget_ws_router  # noqa: E402

app.include_router(agents_router)
app.include_router(agents_ws_router)
app.include_router(auth_router)
app.include_router(channels_router)
app.include_router(conversations_router)
app.include_router(feishu_webhook_router)
app.include_router(knowledge_router)
app.include_router(widget_router)
app.include_router(widget_ws_router)