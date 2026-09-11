from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core.config import get_settings
from core.health import aggregate_health
from core.logging import configure_logging, get_logger
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


@app.get("/health")
async def health() -> JSONResponse:
    body, all_ok = await aggregate_health()
    # 200 when all components are healthy; 503 when degraded so that load
    # balancers / k8s probes / alerting can detect the failure.
    return JSONResponse(status_code=200 if all_ok else 503, content=body)


from agent.api import router as agents_router  # noqa: E402
from auth.api import router as auth_router  # noqa: E402
from channel.api import router as channels_router  # noqa: E402
from channel.feishu.webhook import router as feishu_webhook_router  # noqa: E402
from conversation.api import router as conversations_router  # noqa: E402
from knowledge.api import router as knowledge_router  # noqa: E402
from widget.api import router as widget_router  # noqa: E402
from widget.ws.router import router as widget_ws_router  # noqa: E402

app.include_router(agents_router)
app.include_router(auth_router)
app.include_router(channels_router)
app.include_router(conversations_router)
app.include_router(feishu_webhook_router)
app.include_router(knowledge_router)
app.include_router(widget_router)
app.include_router(widget_ws_router)