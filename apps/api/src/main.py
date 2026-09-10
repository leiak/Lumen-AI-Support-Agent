from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core.config import get_settings
from core.health import aggregate_health
from core.logging import configure_logging, get_logger
from core.qdrant import close_qdrant_client
from core.redis import close_redis, get_redis


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    settings = get_settings()
    configure_logging()
    log = get_logger("startup")
    log.info("api.starting", environment=settings.environment, service=settings.service_name)
    # Eagerly create the redis client pool so the first request doesn't pay
    # connection-setup latency. (The pool itself connects lazily on first command.)
    get_redis()
    yield
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


from auth.api import router as auth_router  # noqa: E402
from channel.feishu.webhook import router as feishu_webhook_router  # noqa: E402
from widget.api import router as widget_router  # noqa: E402

app.include_router(auth_router)
# TODO(Task 4.13): wire unified channel CRUD endpoints here.
app.include_router(feishu_webhook_router)
app.include_router(widget_router)