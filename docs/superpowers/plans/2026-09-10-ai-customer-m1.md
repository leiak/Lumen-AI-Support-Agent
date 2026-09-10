# AI 客服系统 M1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 M1 端到端可演示切片:Web Widget + 飞书双渠道接入,RAG 知识库问答,人工坐席收件箱,基础多租户隔离。

**Architecture:** Python 3.11 + FastAPI 模块化单体 + Arq 异步 Worker + PostgreSQL(RLS) + Redis + Qdrant。M1 使用最小 LLM 客户端(provider 适配 + 基础重试),完整 LLM Gateway 留到 M3。

**Tech Stack:**
- 后端:Python 3.11 / FastAPI / SQLAlchemy 2 / Alembic / Pydantic v2 / uv
- 数据库:PostgreSQL 16 / Redis 7 / Qdrant 1.7+
- 异步:Arq(基于 Redis)
- LLM:Anthropic Claude / OpenAI / Ollama(via OpenAI 兼容)
- 前端:React 18 / TypeScript / Vite / TanStack Query
- 部署:Docker / Docker Compose(本地) / Kubernetes(生产)
- 测试:pytest / pytest-asyncio / httpx / testcontainers

**Spec Reference:** `docs/superpowers/specs/2026-09-10-ai-customer-service-design.md`

---

## 阶段总览

| 阶段 | 内容 | 关键产物 |
|------|------|---------|
| 1. 基础脚手架 | 仓库、Python 环境、Docker Compose、CI 雏形、FastAPI 启动、Postgres/Redis/Qdrant 联通 | 可启动的 FastAPI + `GET /health` |
| 2. 租户 + 鉴权 | Tenant/User 模型、JWT、密码哈希、租户上下文、RLS hook | 登录可用,带租户隔离的 repository |
| 3. 最小 LLM 客户端 | Provider 抽象、Anthropic/OpenAI/Ollama 适配、usage 落库 | `LLMClient.chat()` 可用 |
| 4. Channel Gateway | Web Widget(WS) + 飞书 Webhook,统一 MessageEnvelope,outbox | 双渠道发消息可入库 |
| 5. 会话 + 消息 | Conversation/Message 模型,WebSocket 推流 | 坐席能看到客户消息 |
| 6. 知识库 + RAG | Article/Chunk 模型,Worker 索引,Qdrant 检索,重排 | RAG 召回可用 |
| 7. Agent Runtime | 基础单步 Agent,接 RAG,转人工决策 | AI 自动应答闭环 |
| 8. 坐席工作台 API | 收件箱、会话详情、AI 实时推荐 | 工作台 API 可调 |
| 9. 前端 + Web Widget | React 工作台、登录、收件箱、会话详情;嵌入 Widget | 端到端可演示 |
| 10. 集成 + 可观测性 | E2E 测试、结构化日志、Prometheus metrics、Doc | M1 可发布 |

> **注**:由于计划体量,本文件给出**阶段 1-3 的完整 TDD 步骤**(奠定后续模式),阶段 4-10 给出任务大纲 + 关键代码模式(执行阶段会按相同 TDD 模式展开)。

---

# 阶段 1:基础脚手架

## Task 1.1:初始化仓库与 Python 环境

**Files:**
- Create: `apps/api/pyproject.toml`
- Create: `apps/api/src/__init__.py`
- Create: `.gitignore`
- Create: `README.md`

- [ ] **Step 1: 在项目根初始化 git 仓库并配置 .gitignore**

```bash
cd D:/work-ai/0401-ai-customer
git init
git config user.email "dev@example.com"
git config user.name "AI Customer Dev"
```

Create `.gitignore`:

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage

# Env
.env
.env.local
*.pem

# Node
node_modules/
dist/
.vite/

# IDE
.vscode/
.idea/

# OS
.DS_Store
Thumbs.db
```

- [ ] **Step 2: 安装 uv**

Windows (PowerShell):
```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

验证:
```bash
uv --version
```

- [ ] **Step 3: 用 uv 初始化 API 项目**

```bash
cd apps/api
uv init --no-readme --python 3.11
```

- [ ] **Step 4: 编写 `pyproject.toml`**

```toml
[project]
name = "ai-customer-api"
version = "0.1.0"
description = "AI Customer Service API"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.29",
    "alembic>=1.13",
    "redis>=5.0",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "python-jose[cryptography]>=3.3",
    "passlib[bcrypt]>=1.7",
    "httpx>=0.27",
    "anthropic>=0.21",
    "openai>=1.30",
    "qdrant-client>=1.7",
    "structlog>=24.1",
    "prometheus-client>=0.20",
    "arq>=0.25",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.1",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "httpx>=0.27",
    "testcontainers[postgres,redis]>=4.7",
    "ruff>=0.4",
    "mypy>=1.10",
    "freezegun>=1.4",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests", "src"]
addopts = "-ra --strict-markers"
markers = [
    "integration: requires real DB/Redis/Qdrant",
    "slow: slow tests",
]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "N", "ASYNC", "S", "RUF"]
ignore = ["S101"]  # allow assert in tests

[tool.mypy]
python_version = "3.11"
strict = true
files = ["src"]
```

- [ ] **Step 5: 同步依赖并验证**

```bash
cd apps/api
uv sync --extra dev
uv run python -c "import fastapi; print(fastapi.__version__)"
```

Expected: prints a version like `0.110.0`

- [ ] **Step 6: 提交**

```bash
cd D:/work-ai/0401-ai-customer
git add .
git commit -m "chore: scaffold api project with uv"
```

---

## Task 1.2:核心配置 + 日志

**Files:**
- Create: `apps/api/src/core/__init__.py`
- Create: `apps/api/src/core/config.py`
- Create: `apps/api/src/core/logging.py`
- Create: `apps/api/src/core/settings.py`
- Create: `apps/api/tests/core/test_config.py`

- [ ] **Step 1: 写失败的测试**

```python
# apps/api/tests/core/test_config.py
import pytest
from core.config import Settings


def test_settings_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "test-secret-32-chars-minimum-length")

    s = Settings()
    assert s.database_url == "postgresql+asyncpg://x:y@localhost:5432/z"
    assert s.jwt_secret == "test-secret-32-chars-minimum-length"
    assert s.environment == "development"


def test_settings_rejects_short_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "short")

    with pytest.raises(ValueError, match="JWT_SECRET must be at least 32"):
        Settings()
```

- [ ] **Step 2: 运行测试,确认失败**

```bash
cd apps/api
uv run pytest tests/core/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'core.config'`

- [ ] **Step 3: 实现 `Settings`**

```python
# apps/api/src/core/settings.py
from enum import StrEnum


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
```

```python
# apps/api/src/core/config.py
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import Environment


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Environment = Environment.DEVELOPMENT

    # Database
    database_url: str = Field(alias="DATABASE_URL")
    database_pool_size: int = 10
    database_max_overflow: int = 20

    # Redis
    redis_url: str = Field(alias="REDIS_URL")

    # Vector DB
    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    qdrant_api_key: str | None = Field(default=None, alias="QDRANT_API_KEY")

    # Auth
    jwt_secret: str = Field(alias="JWT_SECRET")
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_minutes: int = 60

    # LLM (M1: 最小客户端)
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    ollama_base_url: str | None = Field(default=None, alias="OLLAMA_BASE_URL")
    default_llm_model: str = Field(default="claude-3-5-sonnet-20241022", alias="DEFAULT_LLM_MODEL")

    # Observability
    log_level: str = "INFO"
    service_name: str = "ai-customer-api"

    @model_validator(mode="after")
    def _validate_jwt_secret(self) -> Self:
        if len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        if self.environment == Environment.PRODUCTION and self.jwt_secret == "dev-secret":
            raise ValueError("Refusing to use dev secret in production")
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
```

- [ ] **Step 4: 创建 `__init__.py`**

```python
# apps/api/src/core/__init__.py
```

```python
# apps/api/tests/__init__.py
```

```python
# apps/api/tests/core/__init__.py
```

- [ ] **Step 5: 配置 pytest 路径**

`apps/api/pyproject.toml` 已有 `tool.pytest.ini_options` 配置。需在 `apps/api/conftest.py` 添加:

```python
# apps/api/conftest.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
```

- [ ] **Step 6: 跑测试确认通过**

```bash
cd apps/api
uv run pytest tests/core/test_config.py -v
```

Expected: PASS, 2 passed

- [ ] **Step 7: 实现结构化日志**

```python
# apps/api/src/core/logging.py
import logging
import sys
from typing import Any

import structlog

from core.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper())

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    logger = structlog.get_logger(name)
    return logger.bind(**initial_values) if initial_values else logger
```

- [ ] **Step 8: 提交**

```bash
git add apps/api/src/core apps/api/tests/core apps/api/conftest.py apps/api/pyproject.toml
git commit -m "feat(core): add settings and structured logging"
```

---

## Task 1.3:数据库连接 + RLS-aware 引擎

**Files:**
- Create: `apps/api/src/core/database.py`
- Create: `apps/api/tests/core/test_database.py`
- Create: `deploy/docker-compose.yml`
- Create: `apps/api/.env.example`

- [ ] **Step 1: 写失败的测试**

```python
# apps/api/tests/core/test_database.py
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session, set_tenant_context


@pytest.mark.integration
async def test_set_tenant_context_sets_session_var() -> None:
    """验证 set_tenant_context 真的发出 SET LOCAL app.tenant_id 命令"""
    async with get_session() as session:
        # 第一次设置
        await set_tenant_context(session, "tenant-a")
        result = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        assert result.scalar() == "tenant-a"

        # 切换
        await set_tenant_context(session, "tenant-b")
        result = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        assert result.scalar() == "tenant-b"
```

- [ ] **Step 2: 写 docker-compose 并启动依赖**

```yaml
# deploy/docker-compose.yml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: aicustomer
      POSTGRES_PASSWORD: aicustomer
      POSTGRES_DB: aicustomer
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U aicustomer"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  qdrant:
    image: qdrant/qdrant:v1.7.4
    ports:
      - "6333:6333"
    volumes:
      - qdrantdata:/qdrant/storage

volumes:
  pgdata:
  qdrantdata:
```

启动:
```bash
cd D:/work-ai/0401-ai-customer
docker compose -f deploy/docker-compose.yml up -d
```

- [ ] **Step 3: 写 .env 文件**

```bash
# apps/api/.env
ENVIRONMENT=development
LOG_LEVEL=DEBUG

DATABASE_URL=postgresql+asyncpg://aicustomer:aicustomer@localhost:5432/aicustomer
REDIS_URL=redis://localhost:6379/0
QDRANT_URL=http://localhost:6333

JWT_SECRET=dev-secret-please-change-in-production-32chars
JWT_ACCESS_TOKEN_TTL_MINUTES=60

ANTHROPIC_API_KEY=sk-ant-xxx
OPENAI_API_KEY=sk-xxx
```

```bash
# apps/api/.env.example (committed, no real secrets)
ENVIRONMENT=development
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/dbname
REDIS_URL=redis://localhost:6379/0
QDRANT_URL=http://localhost:6333
JWT_SECRET=change-me-to-32-char-random-string
JWT_ACCESS_TOKEN_TTL_MINUTES=60
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
DEFAULT_LLM_MODEL=claude-3-5-sonnet-20241022
```

- [ ] **Step 4: 实现 database 引擎 + tenant context**

```python
# apps/api/src/core/database.py
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import get_settings

# 当前异步上下文中的 tenant_id
_current_tenant_id: ContextVar[str | None] = ContextVar("current_tenant_id", default=None)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_pre_ping=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """通用 session 入口(非请求路径,Worker 用)"""
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def set_tenant_contextvar(tenant_id: str) -> None:
    _current_tenant_id.set(tenant_id)


def get_tenant_contextvar() -> str | None:
    return _current_tenant_id.get()


async def set_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    """在事务中设置 Postgres 会话变量,供 RLS 策略使用"""
    # 必须参数化,防止注入
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


async def reset_tenant_context(session: AsyncSession) -> None:
    await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
```

- [ ] **Step 5: 跑集成测试**

```bash
cd apps/api
uv run pytest tests/core/test_database.py -v -m integration
```

Expected: PASS, 1 passed

- [ ] **Step 6: 提交**

```bash
git add deploy apps/api/src/core/database.py apps/api/tests/core/test_database.py apps/api/.env.example
git commit -m "feat(core): async db engine with tenant context"
```

---

## Task 1.4:Redis 客户端 + 健康检查

**Files:**
- Create: `apps/api/src/core/redis.py`
- Create: `apps/api/src/main.py`
- Create: `apps/api/src/core/health.py`
- Create: `apps/api/tests/core/test_health.py`

- [ ] **Step 1: 写失败测试**

```python
# apps/api/tests/core/test_health.py
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_health_endpoint_returns_ok_when_dependencies_up() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["components"]["postgres"] == "ok"
    assert data["components"]["redis"] == "ok"
    assert data["components"]["qdrant"] == "ok"
```

- [ ] **Step 2: 实现 Redis 客户端**

```python
# apps/api/src/core/redis.py
from typing import Any

import redis.asyncio as aioredis

from core.config import get_settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        settings = get_settings()
        _client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
```

- [ ] **Step 3: 实现健康检查**

```python
# apps/api/src/core/health.py
from typing import Any

from sqlalchemy import text

from core.database import get_session
from core.qdrant import get_qdrant_client
from core.redis import get_redis


async def check_postgres() -> dict[str, Any]:
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def check_redis() -> dict[str, Any]:
    try:
        r = get_redis()
        await r.ping()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def check_qdrant() -> dict[str, Any]:
    try:
        client = get_qdrant_client()
        await client.get_collections()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def aggregate_health() -> dict[str, Any]:
    pg, rd, qd = await asyncio.gather(
        check_postgres(), check_redis(), check_qdrant()
    )
    components = {
        "postgres": pg["status"],
        "redis": rd["status"],
        "qdrant": qd["status"],
    }
    all_ok = all(c == "ok" for c in components.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "components": components,
    }
```

需先添加 qdrant 客户端封装:

```python
# apps/api/src/core/qdrant.py
from qdrant_client import AsyncQdrantClient

from core.config import get_settings

_client: AsyncQdrantClient | None = None


def get_qdrant_client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
        )
    return _client
```

- [ ] **Step 4: 实现 FastAPI 主入口**

```python
# apps/api/src/main.py
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
```

需要在 `health.py` 顶部加 `import asyncio`。

- [ ] **Step 5: 跑测试**

```bash
cd apps/api
uv run pytest tests/core/test_health.py -v
```

Expected: PASS, 1 passed

- [ ] **Step 6: 手动启动验证**

```bash
cd apps/api
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

另一终端:
```bash
curl http://localhost:8000/health
```

Expected:
```json
{"status":"ok","components":{"postgres":"ok","redis":"ok","qdrant":"ok"}}
```

- [ ] **Step 7: 提交**

```bash
git add apps/api/src/core apps/api/src/main.py apps/api/tests/core
git commit -m "feat(core): health endpoint with pg/redis/qdrant checks"
```

---

# 阶段 2:租户 + 鉴权

## Task 2.1:Alembic 初始化 + 基础迁移框架

**Files:**
- Create: `apps/api/alembic.ini`
- Create: `apps/api/migrations/env.py`
- Create: `apps/api/migrations/script.py.mako`
- Create: `apps/api/src/core/migrations.py`

- [ ] **Step 1: 初始化 alembic**

```bash
cd apps/api
uv run alembic init -t async migrations
```

- [ ] **Step 2: 配置 `alembic.ini`**

修改 `alembic.ini` 顶部:

```ini
[alembic]
script_location = migrations
prepend_sys_path = src
sqlalchemy.url = driver://user:pass@localhost/dbname
# 其余默认
```

`sqlalchemy.url` 留占位,实际从 settings 读。

- [ ] **Step 3: 改写 `migrations/env.py` 读取 settings**

```python
# apps/api/migrations/env.py
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from core.config import get_settings
from core.database import Base  # 后续会创建

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 注入真实的 database_url
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 4: 创建空的 `Base` 占位(下一步会扩展)**

```python
# apps/api/src/core/database.py 追加(保留已有代码)

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的基类"""
    pass
```

- [ ] **Step 5: 测试迁移可运行(空状态)**

```bash
cd apps/api
uv run alembic current
```

Expected: 无输出,退出码 0(因为还没有任何迁移)

- [ ] **Step 6: 提交**

```bash
git add apps/api/alembic.ini apps/api/migrations apps/api/src/core/database.py
git commit -m "feat(db): alembic init with async support"
```

---

## Task 2.2:Tenant + User 模型与迁移

**Files:**
- Create: `apps/api/src/tenant/__init__.py`
- Create: `apps/api/src/tenant/models.py`
- Create: `apps/api/src/tenant/enums.py`
- Create: `apps/api/migrations/versions/0001_create_tenants_users.py`
- Create: `apps/api/tests/tenant/test_models.py`

- [ ] **Step 1: 定义枚举**

```python
# apps/api/src/tenant/enums.py
from enum import StrEnum


class TenantPlan(StrEnum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class UserRole(StrEnum):
    ADMIN = "admin"
    SUPERVISOR = "supervisor"
    AGENT = "agent"
    VIEWER = "viewer"


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"
```

- [ ] **Step 2: 写测试(模型可被创建并 round-trip)**

```python
# apps/api/tests/tenant/test_models.py
import pytest
from sqlalchemy import select

from tenant.enums import TenantPlan, UserRole
from tenant.models import Tenant, User


@pytest.mark.integration
async def test_create_tenant_and_user() -> None:
    from core.database import get_session

    async with get_session() as session:
        tenant = Tenant(
            name="Acme Corp",
            plan=TenantPlan.PRO,
        )
        session.add(tenant)
        await session.flush()

        user = User(
            tenant_id=tenant.id,
            email="admin@acme.com",
            password_hash="fake-hash",
            role=UserRole.ADMIN,
        )
        session.add(user)
        await session.flush()

        # 重新查询
        result = await session.execute(
            select(User).where(User.email == "admin@acme.com")
        )
        loaded = result.scalar_one()
        assert loaded.tenant_id == tenant.id
        assert loaded.role == UserRole.ADMIN
```

- [ ] **Step 3: 实现模型**

```python
# apps/api/src/tenant/models.py
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from tenant.enums import TenantPlan, TenantStatus, UserRole

if TYPE_CHECKING:
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    plan: Mapped[TenantPlan] = mapped_column(String(20), nullable=False, default=TenantPlan.FREE)
    status: Mapped[TenantStatus] = mapped_column(String(20), nullable=False, default=TenantStatus.ACTIVE)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now(), nullable=False)

    users: Mapped[list["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),)

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(String(20), nullable=False, default=UserRole.AGENT)
    sso_subject: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now(), nullable=False)

    tenant: Mapped["Tenant"] = relationship(back_populates="users")
```

需添加 `ulid-py` 到依赖,并实现 `id_gen`:

```bash
cd apps/api
uv add ulid-py
```

```python
# apps/api/src/core/id_gen.py
import ulid


def new_id() -> str:
    return str(ulid.new())
```

- [ ] **Step 4: 创建迁移**

```bash
cd apps/api
uv run alembic revision --autogenerate -m "create tenants and users"
```

Review 生成的 `migrations/versions/0001_*.py`,确认:
- tenants 和 users 表结构正确
- 外键 ON DELETE CASCADE 正确
- 唯一约束存在
- 索引存在

- [ ] **Step 5: 应用迁移**

```bash
uv run alembic upgrade head
```

验证:
```bash
uv run python -c "
import asyncio
from sqlalchemy import text
from core.database import get_session
async def m():
    async with get_session() as s:
        r = await s.execute(text(\"SELECT tablename FROM pg_tables WHERE schemaname='public'\"))
        print([row[0] for row in r])
asyncio.run(m())
"
```

Expected: `['tenants', 'users', 'alembic_version']`

- [ ] **Step 6: 跑测试**

```bash
uv run pytest tests/tenant/test_models.py -v -m integration
```

Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add apps/api/src/tenant apps/api/src/core/id_gen.py apps/api/migrations apps/api/tests/tenant
git commit -m "feat(tenant): tenant and user models with migration"
```

---

## Task 2.3:密码哈希 + Repository

**Files:**
- Create: `apps/api/src/auth/password.py`
- Create: `apps/api/src/tenant/repository.py`
- Create: `apps/api/tests/auth/test_password.py`
- Create: `apps/api/tests/tenant/test_repository.py`

- [ ] **Step 1: 写密码测试**

```python
# apps/api/tests/auth/test_password.py
from auth.password import hash_password, verify_password


def test_hash_and_verify_roundtrip() -> None:
    plain = "correct-horse-battery-staple"
    hashed = hash_password(plain)
    assert hashed != plain
    assert verify_password(plain, hashed) is True
    assert verify_password("wrong", hashed) is False
```

- [ ] **Step 2: 实现密码**

```bash
cd apps/api
uv add 'passlib[bcrypt]>=1.7'
```

```python
# apps/api/src/auth/__init__.py
```

```python
# apps/api/src/auth/password.py
from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)
```

- [ ] **Step 3: 跑测试**

```bash
cd apps/api
uv run pytest tests/auth/test_password.py -v
```

Expected: PASS

- [ ] **Step 4: 写 Repository 测试**

```python
# apps/api/tests/tenant/test_repository.py
import pytest

from core.database import get_session
from core.id_gen import new_id
from tenant.enums import TenantPlan, UserRole, TenantStatus
from tenant.models import Tenant, User
from tenant.repository import TenantRepository, UserRepository


@pytest.mark.integration
async def test_tenant_repository_create_and_get() -> None:
    repo = TenantRepository()
    tenant = await repo.create(name="Acme", plan=TenantPlan.PRO)
    assert tenant.id
    assert tenant.status == TenantStatus.ACTIVE

    loaded = await repo.get_by_id(tenant.id)
    assert loaded is not None
    assert loaded.name == "Acme"


@pytest.mark.integration
async def test_user_repository_unique_per_tenant() -> None:
    async with get_session() as session:
        t = Tenant(name="X", plan=TenantPlan.FREE)
        session.add(t)
        await session.flush()
        tenant_id = t.id

    repo = UserRepository()
    u1 = await repo.create(tenant_id=tenant_id, email="a@x.com", password_hash="h", role=UserRole.AGENT)
    assert u1.id

    with pytest.raises(Exception):  # IntegrityError
        await repo.create(tenant_id=tenant_id, email="a@x.com", password_hash="h", role=UserRole.AGENT)
```

- [ ] **Step 5: 实现 Repository**

```python
# apps/api/src/tenant/repository.py
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session, get_sessionmaker
from core.id_gen import new_id
from tenant.enums import TenantPlan, TenantStatus, UserRole
from tenant.models import Tenant, User


class TenantRepository:
    async def create(self, *, name: str, plan: TenantPlan = TenantPlan.FREE) -> Tenant:
        async with get_session() as session:
            tenant = Tenant(id=new_id(), name=name, plan=plan)
            session.add(tenant)
            await session.flush()
            await session.refresh(tenant)
            return tenant

    async def get_by_id(self, tenant_id: str) -> Tenant | None:
        async with get_session() as session:
            return await session.get(Tenant, tenant_id)


class UserRepository:
    async def create(
        self,
        *,
        tenant_id: str,
        email: str,
        password_hash: str,
        role: UserRole = UserRole.AGENT,
        full_name: str | None = None,
    ) -> User:
        async with get_session() as session:
            user = User(
                id=new_id(),
                tenant_id=tenant_id,
                email=email,
                password_hash=password_hash,
                role=role,
                full_name=full_name,
            )
            session.add(user)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(user)
            return user

    async def get_by_email(self, tenant_id: str, email: str) -> User | None:
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.tenant_id == tenant_id, User.email == email)
            )
            return result.scalar_one_or_none()
```

- [ ] **Step 6: 跑测试**

```bash
cd apps/api
uv run pytest tests/tenant/test_repository.py -v -m integration
```

Expected: PASS (2 passed)

- [ ] **Step 7: 提交**

```bash
git add apps/api/src/auth apps/api/src/tenant/repository.py apps/api/tests
git commit -m "feat(tenant,auth): repositories and password hashing"
```

---

## Task 2.4:JWT + 登录 API

**Files:**
- Create: `apps/api/src/auth/jwt.py`
- Create: `apps/api/src/auth/service.py`
- Create: `apps/api/src/auth/api.py`
- Create: `apps/api/src/auth/schemas.py`
- Create: `apps/api/src/auth/dependencies.py`
- Create: `apps/api/tests/auth/test_jwt.py`
- Create: `apps/api/tests/auth/test_api.py`

- [ ] **Step 1: 写 JWT 测试**

```python
# apps/api/tests/auth/test_jwt.py
import time

import pytest

from auth.jwt import create_access_token, decode_token
from core.config import get_settings


def test_jwt_roundtrip() -> None:
    token = create_access_token(tenant_id="t1", user_id="u1", role="admin")
    payload = decode_token(token)
    assert payload["sub"] == "u1"
    assert payload["tenant_id"] == "t1"
    assert payload["role"] == "admin"


def test_jwt_invalid_signature_rejected() -> None:
    token = create_access_token(tenant_id="t1", user_id="u1", role="admin")
    bad = token[:-2] + "xx"
    with pytest.raises(ValueError, match="Invalid token"):
        decode_token(bad)
```

- [ ] **Step 2: 实现 JWT**

```python
# apps/api/src/auth/jwt.py
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt

from core.config import get_settings


class TokenError(Exception):
    pass


def create_access_token(*, tenant_id: str, user_id: str, role: str, extra: dict[str, Any] | None = None) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_access_token_ttl_minutes)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        raise TokenError(f"Invalid token: {e}") from e
```

- [ ] **Step 3: 跑 JWT 测试**

```bash
cd apps/api
uv run pytest tests/auth/test_jwt.py -v
```

Expected: PASS

- [ ] **Step 4: 写 Pydantic schemas**

```python
# apps/api/src/auth/schemas.py
from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserInfo"


class UserInfo(BaseModel):
    id: str
    tenant_id: str
    email: str
    full_name: str | None
    role: str


LoginResponse.model_rebuild()
```

- [ ] **Step 5: 写 Service 测试**

```python
# apps/api/tests/auth/test_api.py
import pytest
from fastapi.testclient import TestClient

from main import app
from auth.password import hash_password
from core.id_gen import new_id
from tenant.enums import TenantPlan, TenantStatus, UserRole
from tenant.models import Tenant
from tenant.repository import TenantRepository, UserRepository


@pytest.fixture
async def seeded_tenant_user():
    from core.database import get_session
    async with get_session() as session:
        t = Tenant(id=new_id(), name="Acme", plan=TenantPlan.PRO, status=TenantStatus.ACTIVE)
        session.add(t)
        await session.flush()
    u = await UserRepository().create(
        tenant_id=t.id,
        email="alice@acme.com",
        password_hash=hash_password("password123"),
        role=UserRole.ADMIN,
    )
    return t, u


def test_login_success(seeded_tenant_user) -> None:
    t, u = seeded_tenant_user
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "alice@acme.com", "password": "password123"},
        headers={"X-Tenant-Id": t.id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == "alice@acme.com"
    assert body["user"]["tenant_id"] == t.id
```

- [ ] **Step 6: 实现 Service + API + Dependencies**

```python
# apps/api/src/auth/service.py
from auth.jwt import create_access_token
from auth.password import verify_password
from auth.schemas import LoginResponse, UserInfo
from core.config import get_settings
from tenant.repository import TenantRepository, UserRepository


class AuthService:
    def __init__(self) -> None:
        self.tenants = TenantRepository()
        self.users = UserRepository()

    async def login(self, tenant_id: str, email: str, password: str) -> LoginResponse:
        user = await self.users.get_by_email(tenant_id, email)
        if user is None or not user.is_active:
            raise ValueError("Invalid credentials")
        if not verify_password(password, user.password_hash):
            raise ValueError("Invalid credentials")
        settings = get_settings()
        token = create_access_token(tenant_id=tenant_id, user_id=user.id, role=user.role)
        return LoginResponse(
            access_token=token,
            expires_in=settings.jwt_access_token_ttl_minutes * 60,
            user=UserInfo(
                id=user.id,
                tenant_id=user.tenant_id,
                email=user.email,
                full_name=user.full_name,
                role=user.role,
            ),
        )
```

```python
# apps/api/src/auth/dependencies.py
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, status

from auth.jwt import TokenError, decode_token
from core.database import set_tenant_contextvar


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization[len("Bearer "):]
    try:
        payload = decode_token(token)
    except TokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e
    set_tenant_contextvar(payload["tenant_id"])
    return payload
```

```python
# apps/api/src/auth/api.py
from fastapi import APIRouter, Depends, HTTPException, Header, status

from auth.schemas import LoginRequest, LoginResponse
from auth.service import AuthService

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
) -> LoginResponse:
    try:
        return await AuthService().login(x_tenant_id, payload.email, payload.password)
    except ValueError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e
```

- [ ] **Step 7: 在 main.py 注册路由**

```python
# apps/api/src/main.py 追加
from auth.api import router as auth_router
app.include_router(auth_router)
```

- [ ] **Step 8: 跑测试**

```bash
cd apps/api
uv run pytest tests/auth/test_api.py -v -m integration
```

Expected: PASS

- [ ] **Step 9: 手动 curl 验证**

```bash
cd apps/api
# 先 seed 一个用户
uv run python -c "
import asyncio
from auth.password import hash_password
from core.id_gen import new_id
from core.database import get_session
from tenant.enums import TenantPlan, UserRole
from tenant.models import Tenant
from tenant.repository import UserRepository

async def seed():
    async with get_session() as s:
        t = Tenant(id=new_id(), name='Acme', plan=TenantPlan.PRO)
        s.add(t); await s.flush()
    await UserRepository().create(
        tenant_id=t.id, email='alice@acme.com',
        password_hash=hash_password('password123'), role=UserRole.ADMIN,
    )
    print('tenant_id:', t.id)
asyncio.run(seed())
"
```

记下打印的 `tenant_id`,然后:

```bash
# 启动服务
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 另一终端
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "X-Tenant-Id: <上面打印的 tenant_id>" \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@acme.com","password":"password123"}'
```

Expected: 返回 access_token

- [ ] **Step 10: 提交**

```bash
git add apps/api/src/auth apps/api/src/main.py apps/api/tests
git commit -m "feat(auth): jwt login api with tenant context"
```

---

# 阶段 3:最小 LLM 客户端

> **设计原则**:抽象 Provider 接口,统一 chat / stream / embed;每个 provider 一个适配器;失败重试 + 错误归一化。M3 才会升级为完整 LLM Gateway(配额/缓存/降级)。

## Task 3.1:LLM 类型 + Provider 抽象

**Files:**
- Create: `apps/api/src/llm_client/__init__.py`
- Create: `apps/api/src/llm_client/types.py`
- Create: `apps/api/src/llm_client/providers/base.py`
- Create: `apps/api/src/llm_client/exceptions.py`
- Create: `apps/api/tests/llm_client/test_types.py`

- [ ] **Step 1: 写测试**

```python
# apps/api/tests/llm_client/test_types.py
from llm_client.types import ChatMessage, ChatRequest, ChatResponse, MessageRole


def test_chat_request_validation() -> None:
    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="Hello")],
        temperature=0.0,
        max_tokens=100,
    )
    assert req.messages[0].content == "Hello"
    assert req.temperature == 0.0


def test_chat_response_construct() -> None:
    resp = ChatResponse(
        content="Hi there",
        model="claude-3-5-sonnet-20241022",
        prompt_tokens=10,
        completion_tokens=5,
        finish_reason="stop",
    )
    assert resp.total_tokens == 15
```

- [ ] **Step 2: 定义异常**

```python
# apps/api/src/llm_client/exceptions.py
class LLMError(Exception):
    """基础错误"""


class ProviderUnavailable(LLMError):
    """provider 不可用(网络/服务挂掉)"""


class RateLimited(LLMError):
    """被限流"""


class InvalidRequest(LLMError):
    """请求参数非法,4xx"""


class OutputInvalid(LLMError):
    """provider 返回了不可解析的内容"""
```

- [ ] **Step 3: 定义类型**

```python
# apps/api/src/llm_client/types.py
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ChatMessage(BaseModel):
    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    stop: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    tool_calls: list[dict[str, Any]] | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
```

- [ ] **Step 4: 定义 Provider 抽象**

```python
# apps/api/src/llm_client/providers/base.py
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from llm_client.types import ChatRequest, ChatResponse


class BaseProvider(ABC):
    name: str

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse: ...

    @abstractmethod
    async def stream(self, request: ChatRequest) -> AsyncIterator[str]: ...
```

- [ ] **Step 5: 跑测试**

```bash
cd apps/api
uv run pytest tests/llm_client/test_types.py -v
```

Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add apps/api/src/llm_client apps/api/tests/llm_client
git commit -m "feat(llm_client): types and provider abstraction"
```

---

## Task 3.2:Anthropic Provider

**Files:**
- Create: `apps/api/src/llm_client/providers/anthropic_provider.py`
- Create: `apps/api/tests/llm_client/test_anthropic_provider.py`

- [ ] **Step 1: 写失败的测试(mock HTTP)**

```python
# apps/api/tests/llm_client/test_anthropic_provider.py
import pytest
from pytest_httpx import HTTPXMock

from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.types import ChatMessage, ChatRequest, MessageRole


@pytest.fixture
def provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="test-key", model="claude-3-5-sonnet-20241022")


async def test_anthropic_chat_success(provider: AnthropicProvider, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json={
            "id": "msg_01",
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "Hello back"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 8},
        },
    )

    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="Hello")],
    )
    resp = await provider.chat(req)
    assert resp.content == "Hello back"
    assert resp.prompt_tokens == 12
    assert resp.completion_tokens == 8
    assert resp.finish_reason == "stop"


async def test_anthropic_chat_rate_limited(provider: AnthropicProvider, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=429,
        json={"error": {"type": "rate_limit_error", "message": "Too many requests"}},
    )

    from llm_client.exceptions import RateLimited
    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="Hi")],
    )
    with pytest.raises(RateLimited):
        await provider.chat(req)
```

```bash
cd apps/api
uv add --dev pytest-httpx
```

- [ ] **Step 2: 实现 Anthropic provider**

```python
# apps/api/src/llm_client/providers/anthropic_provider.py
import os
from typing import Any

import httpx

from llm_client.exceptions import (
    InvalidRequest,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.providers.base import BaseProvider
from llm_client.types import ChatMessage, ChatRequest, ChatResponse, MessageRole

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def _convert_messages(messages: list[ChatMessage]) -> tuple[str | None, list[dict[str, Any]]]:
    """anthropic 要求 system 消息单独成 system 字段,其余转换 role/content"""
    system: str | None = None
    converted: list[dict[str, Any]] = []
    for m in messages:
        if m.role == MessageRole.SYSTEM:
            system = m.content if system is None else f"{system}\n\n{m.content}"
        else:
            converted.append({"role": m.role.value, "content": m.content})
    return system, converted


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(self, *, api_key: str, model: str, timeout: float = 30.0) -> None:
        if not api_key:
            raise ValueError("Anthropic API key required")
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(
            base_url="https://api.anthropic.com",
            timeout=timeout,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(self, request: ChatRequest) -> ChatResponse:
        system, messages = _convert_messages(request.messages)
        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens or 1024,
        }
        if system is not None:
            body["system"] = system
        if request.stop:
            body["stop_sequences"] = request.stop
        if request.tools:
            body["tools"] = request.tools

        try:
            resp = await self._client.post(ANTHROPIC_API_URL, json=body)
        except httpx.HTTPError as e:
            raise ProviderUnavailable(f"Anthropic network error: {e}") from e

        if resp.status_code == 429:
            raise RateLimited("Anthropic rate limited")
        if 400 <= resp.status_code < 500:
            try:
                err = resp.json().get("error", {})
            except Exception:
                err = {"message": resp.text}
            raise InvalidRequest(f"Anthropic 4xx: {err.get('message', resp.text)}")
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"Anthropic 5xx: {resp.status_code}")

        try:
            data = resp.json()
        except Exception as e:
            raise OutputInvalid(f"Anthropic returned non-JSON: {resp.text[:200]}") from e

        content_blocks = data.get("content") or []
        text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
        tool_calls = [b for b in content_blocks if b.get("type") == "tool_use"]

        stop_reason = data.get("stop_reason", "")
        finish = "tool_use" if tool_calls and not text_parts else ("stop" if stop_reason == "end_turn" else stop_reason)

        usage = data.get("usage", {})
        return ChatResponse(
            content="".join(text_parts),
            model=data.get("model", request.model),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            finish_reason=finish,
            tool_calls=tool_calls or None,
            raw=data,
        )

    async def stream(self, request: ChatRequest):
        # M1 先不实现流式,留到 M2
        raise NotImplementedError("Anthropic streaming pending M2")
```

- [ ] **Step 3: 跑测试**

```bash
cd apps/api
uv run pytest tests/llm_client/test_anthropic_provider.py -v
```

Expected: PASS (2 passed)

- [ ] **Step 4: 提交**

```bash
git add apps/api/src/llm_client/providers/anthropic_provider.py apps/api/tests/llm_client
git commit -m "feat(llm_client): anthropic provider with retry mapping"
```

---

## Task 3.3:OpenAI / Ollama 适配器(轻量)

**Files:**
- Create: `apps/api/src/llm_client/providers/openai_provider.py`
- Create: `apps/api/src/llm_client/providers/ollama_provider.py`
- Create: `apps/api/tests/llm_client/test_openai_provider.py`

> 模式与 Anthropic 一致:用 OpenAI 官方 SDK(OpenAI 和 Ollama 都兼容 OpenAI 协议)。本任务给完整 OpenAI 测试,Ollama 同模式只列实现。

- [ ] **Step 1: 写 OpenAI 测试**

```python
# apps/api/tests/llm_client/test_openai_provider.py
import pytest
from pytest_httpx import HTTPXMock

from llm_client.providers.openai_provider import OpenAIProvider
from llm_client.types import ChatMessage, ChatRequest, MessageRole


async def test_openai_chat_success(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.openai.com/v1/chat/completions",
        json={
            "id": "cmpl-1",
            "model": "gpt-4o",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop", "index": 0}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
    )

    p = OpenAIProvider(api_key="k", model="gpt-4o", base_url="https://api.openai.com/v1")
    req = ChatRequest(model="gpt-4o", messages=[ChatMessage(role=MessageRole.USER, content="hi")])
    resp = await p.chat(req)
    assert resp.content == "ok"
    assert resp.total_tokens == 8
```

- [ ] **Step 2: 实现 OpenAI provider**

```python
# apps/api/src/llm_client/providers/openai_provider.py
import httpx

from llm_client.exceptions import (
    InvalidRequest,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.providers.base import BaseProvider
from llm_client.types import ChatRequest, ChatResponse


class OpenAIProvider(BaseProvider):
    """OpenAI 兼容协议(OpenAI 官方、Ollama、vLLM 等)"""

    def __init__(self, *, api_key: str, model: str, base_url: str = "https://api.openai.com/v1", timeout: float = 30.0) -> None:
        if not api_key and "openai.com" in base_url:
            raise ValueError("API key required for OpenAI")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(self, request: ChatRequest) -> ChatResponse:
        body = {
            "model": request.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens:
            body["max_tokens"] = request.max_tokens
        if request.stop:
            body["stop"] = request.stop
        if request.tools:
            body["tools"] = request.tools
        if request.tool_choice:
            body["tool_choice"] = request.tool_choice

        try:
            resp = await self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise ProviderUnavailable(f"OpenAI network error: {e}") from e

        if resp.status_code == 429:
            raise RateLimited("OpenAI rate limited")
        if 400 <= resp.status_code < 500:
            raise InvalidRequest(f"OpenAI 4xx: {resp.text}")
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"OpenAI 5xx: {resp.status_code}")

        try:
            data = resp.json()
        except Exception as e:
            raise OutputInvalid(f"OpenAI non-JSON: {resp.text[:200]}") from e

        choice = data["choices"][0]
        msg = choice["message"]
        return ChatResponse(
            content=msg.get("content", "") or "",
            model=data.get("model", request.model),
            prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
            completion_tokens=data.get("usage", {}).get("completion_tokens", 0),
            finish_reason=choice.get("finish_reason", "stop"),
            tool_calls=msg.get("tool_calls"),
            raw=data,
        )

    async def stream(self, request: ChatRequest):
        raise NotImplementedError("OpenAI streaming pending M2")
```

- [ ] **Step 3: Ollama 适配器(Ollama 兼容 OpenAI 协议,直接复用)**

```python
# apps/api/src/llm_client/providers/ollama_provider.py
from llm_client.providers.openai_provider import OpenAIProvider


class OllamaProvider(OpenAIProvider):
    """Ollama 通过 OpenAI 兼容协议对外提供接口"""

    def __init__(self, *, base_url: str, model: str, timeout: float = 60.0) -> None:
        super().__init__(api_key="ollama", model=model, base_url=base_url, timeout=timeout)
```

- [ ] **Step 4: 跑测试**

```bash
cd apps/api
uv run pytest tests/llm_client/test_openai_provider.py -v
```

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add apps/api/src/llm_client/providers apps/api/tests/llm_client/test_openai_provider.py
git commit -m "feat(llm_client): openai and ollama providers"
```

---

## Task 3.4:LLMClient(带重试)+ Usage 落库

**Files:**
- Create: `apps/api/src/llm_client/usage.py`
- Create: `apps/api/src/llm_client/client.py`
- Create: `apps/api/tests/llm_client/test_client.py`
- Create: `apps/api/migrations/versions/0002_create_llm_usage.py`
- Create: `apps/api/src/llm_client/models.py`

- [ ] **Step 1: LLMUsage 模型 + 迁移**

```python
# apps/api/src/llm_client/models.py
from datetime import datetime

from sqlalchemy import JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class LLMUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(default=0.0, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cached: Mapped[bool] = mapped_column(default=False, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
```

```bash
cd apps/api
uv run alembic revision --autogenerate -m "create llm_usage"
uv run alembic upgrade head
```

- [ ] **Step 2: 写 usage 落库测试**

```python
# apps/api/tests/llm_client/test_client.py
import pytest
from pytest_httpx import HTTPXMock

from llm_client.client import LLMClient
from llm_client.exceptions import ProviderUnavailable, RateLimited
from llm_client.providers.anthropic_provider import AnthropicProvider
from llm_client.types import ChatMessage, ChatRequest, MessageRole


@pytest.fixture
def client_with_anthropic() -> LLMClient:
    p = AnthropicProvider(api_key="k", model="claude-3-5-sonnet-20241022")
    return LLMClient(default_provider=p, tenant_id="t1")


async def test_client_retries_on_5xx(client_with_anthropic: LLMClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=503,
        json={"error": {"type": "overloaded", "message": "x"}},
    )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "Recovered"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    req = ChatRequest(model="claude-3-5-sonnet-20241022", messages=[ChatMessage(role=MessageRole.USER, content="hi")])
    resp = await client_with_anthropic.chat(req, max_retries=2)
    assert resp.content == "Recovered"


async def test_client_does_not_retry_on_rate_limit(client_with_anthropic: LLMClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        status_code=429,
        json={"error": {"type": "rate_limit", "message": "slow"}},
    )
    req = ChatRequest(model="claude-3-5-sonnet-20241022", messages=[ChatMessage(role=MessageRole.USER, content="hi")])
    with pytest.raises(RateLimited):
        await client_with_anthropic.chat(req, max_retries=3)


async def test_client_records_usage(client_with_anthropic: LLMClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )
    req = ChatRequest(model="claude-3-5-sonnet-20241022", messages=[ChatMessage(role=MessageRole.USER, content="hi")])
    await client_with_anthropic.chat(req)
    # 等待异步 usage 落库
    await client_with_anthropic.flush_usage()

    from sqlalchemy import select
    from core.database import get_session
    from llm_client.models import LLMUsage

    async with get_session() as s:
        r = await s.execute(select(LLMUsage).where(LLMUsage.tenant_id == "t1"))
        rows = r.scalars().all()
    assert len(rows) == 1
    assert rows[0].prompt_tokens == 10
    assert rows[0].completion_tokens == 5
```

- [ ] **Step 3: 实现 LLMClient**

```python
# apps/api/src/llm_client/usage.py
import asyncio
from typing import Any

from sqlalchemy import insert

from core.database import get_sessionmaker
from core.id_gen import new_id
from llm_client.models import LLMUsage

# 简单成本估算,实际应以计费表为准
_COST_PER_1K = {
    "claude-3-5-sonnet-20241022": (0.003, 0.015),
    "claude-3-5-haiku-20241022": (0.0008, 0.004),
    "gpt-4o": (0.005, 0.015),
    "gpt-4o-mini": (0.00015, 0.0006),
}


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    in_cost, out_cost = _COST_PER_1K.get(model, (0.0, 0.0))
    return (prompt_tokens / 1000.0) * in_cost + (completion_tokens / 1000.0) * out_cost


class UsageRecorder:
    def __init__(self) -> None:
        self._pending: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    def enqueue(
        self,
        *,
        tenant_id: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        request_id: str,
        cached: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        cost = estimate_cost_usd(model, prompt_tokens, completion_tokens)
        self._pending.append(
            {
                "id": new_id(),
                "tenant_id": tenant_id,
                "provider": provider,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost,
                "request_id": request_id,
                "cached": cached,
                "metadata_json": metadata or {},
            }
        )

    async def flush(self) -> None:
        async with self._lock:
            if not self._pending:
                return
            batch = self._pending[:]
            self._pending.clear()
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(insert(LLMUsage), batch)
            await session.commit()
```

```python
# apps/api/src/llm_client/client.py
import asyncio
import random
import uuid
from typing import Any

from llm_client.exceptions import (
    InvalidRequest,
    LLMError,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.providers.base import BaseProvider
from llm_client.types import ChatRequest, ChatResponse
from llm_client.usage import UsageRecorder


class LLMClient:
    def __init__(
        self,
        *,
        default_provider: BaseProvider,
        tenant_id: str,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self.default_provider = default_provider
        self.tenant_id = tenant_id
        self.usage = usage_recorder or UsageRecorder()

    async def aclose(self) -> None:
        if hasattr(self.default_provider, "aclose"):
            await self.default_provider.aclose()
        await self.flush_usage()

    async def flush_usage(self) -> None:
        await self.usage.flush()

    async def chat(self, request: ChatRequest, *, max_retries: int = 3) -> ChatResponse:
        request_id = uuid.uuid4().hex
        attempt = 0
        last_exc: Exception | None = None
        while attempt <= max_retries:
            try:
                resp = await self.default_provider.chat(request)
                self.usage.enqueue(
                    tenant_id=self.tenant_id,
                    provider=self.default_provider.name,
                    model=resp.model,
                    prompt_tokens=resp.prompt_tokens,
                    completion_tokens=resp.completion_tokens,
                    request_id=request_id,
                )
                return resp
            except RateLimited as e:
                # 限流立刻抛,不重试
                raise
            except InvalidRequest as e:
                # 4xx 不重试
                raise
            except (ProviderUnavailable, OutputInvalid) as e:
                last_exc = e
                if attempt == max_retries:
                    break
                backoff = min(2 ** attempt, 8) + random.uniform(0, 0.5)
                await asyncio.sleep(backoff)
                attempt += 1

        assert last_exc is not None
        raise last_exc
```

- [ ] **Step 4: 跑测试**

```bash
cd apps/api
uv run pytest tests/llm_client/test_client.py -v -m integration
```

Expected: PASS (3 passed)

- [ ] **Step 5: 提交**

```bash
git add apps/api/src/llm_client apps/api/migrations apps/api/tests
git commit -m "feat(llm_client): client with retry and usage tracking"
```

---

# 阶段 4-10:任务大纲

> 后续阶段采用相同 TDD 模式(测试 → 失败 → 实现 → 通过 → 提交)。每节给出关键设计点、关键代码骨架、需要写哪些任务。执行时按相同模式展开每个任务。

## 阶段 4:Channel Gateway(预计 12-15 任务)

**关键设计点**:
- `MessageEnvelope` 是渠道无关的统一消息模型
- 渠道适配器实现 `ChannelAdapter` 协议:`parse_inbound()` 和 `send_outbound()`
- 飞书 Webhook:验证签名、解析事件、回复消息走飞书 Open API
- Web Widget:WebSocket 长连接 + 短 token 鉴权
- 渠道接入走 outbox 模式:消息先入库 outbox,Worker 异步投递

**核心模型**:
- `Channel` (id, tenant_id, type, credentials_encrypted, status, config_json)
- `ChannelBinding` (channel_id, external_conversation_id, internal_conversation_id)
- `OutboxEvent` (id, tenant_id, type, payload_json, status, attempts, created_at, processed_at)

**关键文件骨架**:

```python
# channel/envelope.py
class MessageEnvelope(BaseModel):
    tenant_id: str
    channel_type: Literal["web", "feishu", "email"]
    channel_id: str
    external_user_id: str
    external_conversation_id: str
    text: str
    attachments: list[Attachment] = []
    raw: dict[str, Any]
    received_at: datetime
```

```python
# channel/protocols.py
class ChannelAdapter(Protocol):
    channel_type: str
    async def parse_inbound(self, request: Request) -> MessageEnvelope: ...
    async def send_outbound(self, channel: Channel, external_user: str, text: str) -> None: ...
```

**飞书签名验证**:
```python
# channel/feishu/signature.py
def verify_feishu_signature(*, timestamp: str, nonce: str, body: bytes, encrypt_key: str) -> bool:
    """飞书加密模式签名校验;非加密模式只校验 timestamp 在 5 分钟内"""
    string_to_sign = f"{timestamp}{nonce}{encrypt_key}{body.decode()}"
    digest = hmac.new(string_to_sign.encode(), digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, request.headers.get("X-Lark-Signature", ""))
```

**WebSocket Widget 鉴权**:
- 客户端用公开 token + 用户标识 换取 短期 JWT(5 分钟)
- WS 连接时校验 JWT 并绑定到 tenant_id

**任务清单**:
- 4.1 outbox 事件表 + 迁移
- 4.2 Outbox dispatcher(Worker 任务)
- 4.3 Channel 模型 + Repository
- 4.4 ChannelBinding 模型
- 4.5 MessageEnvelope 类型 + Adapter 协议
- 4.6 飞书签名校验
- 4.7 飞书 Webhook 入口 + 适配器
- 4.8 飞书 outbound(回复消息)
- 4.9 Web Widget token 颁发 API
- 4.10 WebSocket 连接管理(connection manager)
- 4.11 Web Widget inbound/outbound
- 4.12 渠道集成测试(飞书 sandbox)
- 4.13 渠道统一路由 API(给后续模块用)

## 阶段 5:会话 + 消息(预计 8-10 任务)

**关键设计点**:
- `Conversation` 按 (channel_id, external_conversation_id) upsert
- `Message` 是不可变事件,role: customer/agent/ai/system/tool
- AI 响应流式通过 WS 推给前端(`message_id` + delta 块)
- 会话状态机:`open` → `pending`(待人工)→ `closed`,`assigned_agent_id` 控制归属

**核心模型**:
- `Conversation` (id, tenant_id, channel_id, customer_external_id, status, assigned_agent_id, ai_handling, opened_at, last_activity_at)
- `Message` (id, conversation_id, role, content_text, content_blocks_json, sender_id, tool_calls_json, created_at)

**WS 推流协议**:
```json
// 推 message delta
{"type": "message.delta", "conversation_id": "...", "message_id": "...", "delta": "..."}
// 推 message 完成
{"type": "message.complete", "conversation_id": "...", "message_id": "...", "content": "..."}
```

**任务清单**:
- 5.1 Conversation + Message 模型与迁移(含 RLS 策略)
- 5.2 渠道入站 → 创建/查找会话
- 5.3 写消息 + 触发 AI(发领域事件)
- 5.4 WS 推送 message.delta/完成
- 5.5 会话状态转移 API(转人工、关单)
- 5.6 会话上下文 summary(简单版,长对话压缩)
- 5.7 Conversation Repository
- 5.8 Conversation Service
- 5.9 会话集成测试
- 5.10 WS 鉴权 + 跨租户拒绝测试

## 阶段 6:知识库 + RAG(预计 12-15 任务)

**关键设计点**:
- `Article` 多版本,`ArticleVersion` 是不可变快照
- Worker 解析文档 → 切片 → embedding → 写 Qdrant
- Qdrant collection 按租户/知识库分集合,或单 collection 带 tenant_id payload 过滤(本项目选后者,简单)
- 检索:向量召回 + BM25(用 PG `tsvector` 或 PGroonga) → 重排(cross-encoder 或 LLM)
- M1 用纯向量召回 + 简单相似度阈值,BM25 留 M2

**核心模型**:
- `KnowledgeBase` (id, tenant_id, name, default_for_conversation)
- `Article` (id, kb_id, title, slug, status, current_version_id, tags)
- `ArticleVersion` (id, article_id, content_md, parsed_chunks_json, multimodal_refs, status)
- `Chunk` (id, version_id, ordinal, text, embedding_id, modality)

**RAG 检索骨架**:
```python
# rag/retriever.py
class HybridRetriever:
    def __init__(self, vector_store: QdrantVectorStore, embedder: Embedder, top_k: int = 5):
        self.vector_store = vector_store
        self.embedder = embedder
        self.top_k = top_k

    async def retrieve(self, *, query: str, tenant_id: str, kb_ids: list[str]) -> list[RetrievedChunk]:
        # M1: 纯向量
        query_vec = await self.embedder.embed(query)
        results = await self.vector_store.search(
            vector=query_vec,
            top_k=self.top_k,
            filter={"tenant_id": tenant_id, "kb_id": {"$in": kb_ids}},
        )
        return [RetrievedChunk.from_qdrant(r) for r in results]
```

**任务清单**:
- 6.1 KnowledgeBase / Article / ArticleVersion / Chunk 模型与迁移
- 6.2 Qdrant collection 初始化(启动 hook)
- 6.3 文档解析(unstructured/markitdown)
- 6.4 多模态处理(图片 OCR + 描述,代码块保留)
- 6.5 切片策略(semantic + length)
- 6.6 Embedding 调用(走 LLM Client 的 embed 接口,M1 用 OpenAI 兼容)
- 6.7 Worker:索引任务(parse → chunk → embed → upsert)
- 6.8 Worker:重索引 / 删除任务
- 6.9 文章 CRUD API
- 6.10 知识库上传 API
- 6.11 向量检索实现
- 6.12 RAG service(retrieve + 构造 prompt context)
- 6.13 RAG 集成测试
- 6.14 离线 eval 集(M1 50+ 样本,跑通流水线)

## 阶段 7:Agent Runtime(预计 6-8 任务)

**关键设计点**:
- M1 单步 Agent:RAG 召回 → 拼 prompt → LLM 决策 → 直接答 / 转人工
- 不做工具调用循环(M2 加),不做复杂 plan(M2 加)
- 转人工决策:LLM 输出特定 token(如 `<TRANSFER>`)或置信度低 → 抛事件
- `AgentPolicy` 是租户级配置,M1 给个默认

**Agent 单步骨架**:
```python
# agent_runtime/runtime.py
class AgentRuntime:
    def __init__(self, llm_client: LLMClient, retriever: Retriever, policy: AgentPolicy):
        self.llm = llm_client
        self.retriever = retriever
        self.policy = policy

    async def handle(self, *, tenant_id: str, conversation_id: str, user_message: str) -> AgentTurn:
        chunks = await self.retriever.retrieve(
            query=user_message, tenant_id=tenant_id, kb_ids=self.policy.allowed_kb_ids
        )
        context = "\n\n".join(f"[{i+1}] {c.text}" for i, c in enumerate(chunks))
        prompt = self._build_prompt(user_message=user_message, context=context)
        resp = await self.llm.chat(prompt)
        if self._should_transfer_to_human(resp):
            return AgentTurn(action=Action.TRANSFER, content=resp.content, references=chunks)
        return AgentTurn(action=Action.ANSWER, content=resp.content, references=chunks)
```

**任务清单**:
- 7.1 AgentPolicy 模型 + 迁移
- 7.2 默认策略 seed
- 7.3 Agent turn 类型 / 决策逻辑
- 7.4 Agent Runtime(单步 RAG + LLM)
- 7.5 转人工事件 + 接收
- 7.6 Agent 集成测试(mock LLM)
- 7.7 Agent 端到端测试(真实 LLM,sandbox 租户)
- 7.8 引用一致性 check(LLM 答案是否引用了检索的 chunk)

## 阶段 8:坐席工作台 API(预计 6-8 任务)

**关键设计点**:
- 收件箱:按 tenant + 可选 filter(assigned/me/unassigned)列出会话
- 会话详情:消息时间线 + 客户信息 + RAG 引用 + 工单
- AI 实时推荐:坐席输入时调用 `agent.suggest_reply()`(基于 RAG + 上一轮对话)
- 派单 / 接管 / 关单

**API 骨架**:
```
GET    /api/v1/workspace/inbox?status=open&assigned_to=me
GET    /api/v1/workspace/conversations/{id}
POST   /api/v1/workspace/conversations/{id}/messages
POST   /api/v1/workspace/conversations/{id}/transfer
POST   /api/v1/workspace/conversations/{id}/close
POST   /api/v1/workspace/conversations/{id}/suggest  # 返回 AI 推荐
```

**任务清单**:
- 8.1 收件箱查询
- 8.2 会话详情(含 RAG 引用)
- 8.3 坐席发消息(关闭 AI 处理)
- 8.4 转派 / 接管
- 8.5 关单
- 8.6 AI 实时推荐 API
- 8.7 工作台集成测试

## 阶段 9:前端 + Web Widget(预计 10-12 任务)

**关键栈**:React 18 + TypeScript + Vite + TanStack Query + TailwindCSS

**两个产物**:
- **坐席工作台**(主前端):登录 → 收件箱 → 会话详情 → 客户信息
- **Web Widget**(嵌入到客户网站):浮动按钮 → 弹窗 → 聊天

**工作台页面**:
- `/login` - 邮箱密码登录,带租户选择
- `/inbox` - 会话列表(状态/分配人/渠道筛选)
- `/inbox/:id` - 会话详情(消息流 + 输入框 + AI 推荐 + 客户信息)
- `/kb` - 知识库管理(浏览/上传/编辑)
- `/settings` - 租户设置(基础)

**Web Widget**:
- 打包成单文件 JS,客户 `<script>` 引入
- 配置:`window.AICustomerConfig = { tenantId, widgetToken }`
- 浮动按钮 + 弹出 iframe 加载 widget UI

**任务清单**:
- 9.1 Vite 项目脚手架
- 9.2 Tailwind + 路由 + Query 客户端
- 9.3 登录页
- 9.4 收件箱页
- 9.5 会话详情页 + WS 消息流
- 9.6 知识库管理页(基础)
- 9.7 租户设置页(基础)
- 9.8 Web Widget SDK(单文件)
- 9.9 Web Widget UI(浮按钮 + 弹窗)
- 9.10 跨域 / origin 校验测试
- 9.11 前端 E2E(Playwright)
- 9.12 端到端演示流程脚本

## 阶段 10:集成 + 可观测性(预计 6-8 任务)

**任务清单**:
- 10.1 结构化日志补全(贯穿 request_id / tenant_id / conversation_id)
- 10.2 Prometheus 指标 API(`/metrics`)
  - 业务:conversations_total, llm_calls_total{cached,provider}, llm_tokens_total
  - 系统:http_request_duration_seconds, http_requests_total
- 10.3 健康检查增强(包含外部依赖)
- 10.4 关键路径 tracing(LLM 调用 / 工具调用 span)
- 10.5 金丝雀 E2E 测试(完整客户旅程)
- 10.6 Dockerfile + docker-compose 一键起
- 10.7 K8s manifests(基础)
- 10.8 README + 演示录屏脚本

---

# 全局测试与质量门

每个 Task 完成后,需运行:

```bash
# 单元 + 集成
cd apps/api && uv run pytest -m "not slow" --cov=src --cov-report=term-missing

# lint + 类型
cd apps/api && uv run ruff check src tests
cd apps/api && uv run mypy src

# 前端
cd apps/web && pnpm lint && pnpm type-check
```

**M1 验收标准**:

1. ✅ 用户从 Web Widget 提问 → AI 用 RAG 知识库回答 → 完整闭环
2. ✅ 同一问题 AI 答不上来 → 自动转人工 → 坐席在收件箱看到 → 接管回复
3. ✅ 飞书用户私聊 Bot → 同样走 AI → 走转人工
4. ✅ 多租户隔离:租户 A 的运营看不到租户 B 的会话
5. ✅ 端到端 E2E 测试(Playwright)过
6. ✅ `/health` / `/metrics` 可用
7. ✅ Docker Compose 一键起

**M1 暂不做**(留给 M2+):

- Agent 多步工具调用循环
- BM25 关键词召回 + 重排
- 流式 LLM 输出(M1 等模型全量返回)
- 历史会话挖掘
- 邮件 / 其他 IM 渠道
- LLM Gateway 完整版(配额/缓存/降级)
- SSO / 审计 / 高级权限

---

# 执行 Handoff

计划已保存到 `docs/superpowers/plans/2026-09-10-ai-customer-m1.md`。

**两种执行方式可选**:

1. **Subagent-Driven(推荐)**:每个任务派一个独立子代理执行,任务间 review,快速迭代
2. **Inline Execution**:在当前会话中按任务顺序批量执行,带 checkpoint 让我审阅

你想用哪种方式启动 M1?如果要 Subagent-Driven,我会用 `superpowers:subagent-driven-development` 技能;Inline 则用 `superpowers:executing-plans`。
