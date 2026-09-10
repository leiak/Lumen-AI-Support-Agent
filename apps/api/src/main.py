from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from core.config import get_settings
from core.health import aggregate_health
from core.logging import configure_logging, get_logger
from core.redis import close_redis, get_redis


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    settings = get_settings()
    configure_logging()
    log = get_logger("startup")
    log.info("api.starting", environment=settings.environment, service=settings.service_name)
    # warm up redis
    _ = get_redis()
    yield
    await close_redis()
    log.info("api.shutdown")


app = FastAPI(
    title="AI Customer API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return await aggregate_health()
