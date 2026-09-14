# Lumen AI Support Agent

> 智能化多租户 B2B SaaS 客服系统 — SaaS / 软件企业的 AI 深度介入客服平台,服务外部客户(Web Widget、邮件、企业 IM)的同时为内部坐席提供统一工作台。

[![status](https://img.shields.io/badge/M1-Stages_1--7_complete-brightgreen)]()
[![tests](https://img.shields.io/badge/agent_tests-64_passed-brightgreen)]()
[![python](https://img.shields.io/badge/python-3.11+-blue)]()
[![framework](https://img.shields.io/badge/FastAPI-0.110+-009688)]()

## 项目状态

| Stage | 模块 | 状态 | 测试 |
|-------|------|------|------|
| 1 | 基础脚手架 (FastAPI + Postgres + Redis + Qdrant) | ✅ | — |
| 2 | 租户 + JWT 鉴权 + RBAC | ✅ | — |
| 3 | LLM 客户端 (Anthropic / OpenAI, retry + 用量记录) | ✅ | — |
| 4 | Channel Gateway (Feishu + Web Widget + WS) | ✅ | — |
| 5 | 会话 + 消息 (Conversation + Message + WS 推送) | ✅ | 289+ passed |
| 6 | 知识库 + RAG (解析 → 切片 → embedding → 检索 → RAG 服务 + eval) | ✅ | 480 passed |
| 7 | Agent Runtime (LangChain + LangGraph + escalate_to_human tool) | ✅ | 64 passed (52 unit + 12 integration) |
| 8 | 坐席工作台 API (me / 发送消息 / queue / claim / suggest-reply) | ✅ | 168+ passed |
| 9 | 前端 + Web Widget UI (agent SPA + 客户 widget SDK + iframe UI + CORS/origin + Playwright E2E + demo script) | ✅ | 116 backend + 98 frontend + 10 e2e |
| 10 | 集成 + 可观测性 | 🔧 进行中 (/metrics + 容器化 + CI 已落地) | — |

设计文档:`docs/superpowers/specs/2026-09-10-ai-customer-service-design.md`
M1 实施计划:`docs/superpowers/plans/2026-09-10-ai-customer-m1.md`

## 架构

```
┌────────────────────────────────────────────────────────────────────────────┐
│                          Channels (Inbound)                                 │
│   Web Widget (WS)  ·  Feishu Bot  ·  Email  ·  HTTP API                    │
└────────────┬───────────────────────────────────────────┬────────────────────┘
             │ process_inbound_envelope                 │
             ▼                                           ▼
   ┌─────────────────────┐                  ┌────────────────────────┐
   │  Channel Gateway    │                  │  WebSocket Broadcaster │
   │  (channel/)         │                  │  (widget/ws/)          │
   └─────────┬───────────┘                  └───────────▲────────────┘
             │ find_or_create_for_inbound                 │
             ▼                                           │
   ┌─────────────────────────────────────────────────────┴──────────────┐
   │                    Conversation / Message Layer                      │
   │   (conversation/)  ConversationService · MessageRepository         │
   │   State machine: OPEN ↔ PENDING → CLOSED  ·  ai_handling flag       │
   └─────────────────────────────────┬───────────────────────────────────┘
                                     │ record_message(CUSTOMER)
                                     ▼
                       ┌──────────────────────────┐
                       │    Agent Runtime (7.x)    │
                       │  LangChain + LangGraph    │
                       │  StateGraph(AgentState)   │
                       └──────────────┬───────────┘
                                      │
              ┌───────────────────────┼──────────────────────┐
              ▼                       ▼                      ▼
   ┌──────────────────┐    ┌────────────────────┐    ┌──────────────────┐
   │  retrieve_node   │    │     llm_node       │    │  tools (7.2+)    │
   │  RAGService →    │    │  LLMClient.chat    │    │  escalate_to_    │
   │  Qdrant MUST     │    │  tool_calls[:1]    │    │  human           │
   │  filter          │    │  CHAT_TEMP=0.7     │    │  (ContextVar-    │
   │                  │    │  MAX_TOK=512       │    │   bound tenant)  │
   └────────┬─────────┘    └─────────┬──────────┘    └────────┬─────────┘
            │                        │                         │
            ▼                        ▼                         ▼
   ┌──────────────────┐    ┌────────────────────┐    ┌──────────────────┐
   │  Knowledge Base  │    │   LLM Provider     │    │ Escalate →       │
   │  Qdrant +        │    │   Anthropic        │    │ ConversationSvc  │
   │  OpenAI embed    │    │   (Claude Haiku)   │    │ .escalate_to_    │
   │                  │    │                    │    │ human_queue       │
   └──────────────────┘    └────────────────────┘    └──────────────────┘
                                     │
                                     ▼
                       AI message persisted → WS broadcast
```

## 核心模块

| 包 | 职责 |
|----|------|
| `apps/api/src/auth/` | JWT 编解码、密码哈希、依赖注入 (require_admin / require_agent_or_admin) |
| `apps/api/src/tenant/` | 租户 CRUD + 配额 |
| `apps/api/src/channel/` | 渠道适配器 (Feishu / Web Widget / Email) + 入站信封处理 |
| `apps/api/src/conversation/` | 会话状态机 + 消息持久化 + WS 广播 |
| `apps/api/src/widget/` | Web Widget token 颁发 + WS 连接管理 + 出站广播 |
| `apps/api/src/knowledge/` | 知识库模型 + 文档解析 + 多模态 + 切片 + worker + RAG service + 检索 + eval |
| `apps/api/src/llm_client/` | 厂商无关的 LLM 客户端 (Anthropic / OpenAI / Ollama) + embedding 客户端 + 用量记录 |
| `apps/api/src/agent/` | LangGraph Agent Runtime + escalate_to_human 工具 |
| `apps/api/src/core/` | DB session / Redis / Qdrant 单例 + structlog 配置 + ID 生成 |

## 快速开始

### 环境要求

- Python 3.11+
- Docker (用于 Postgres + Redis + Qdrant 集成测试)
- Anthropic API key (必需,M1 默认 LLM)

### 本地开发

```bash
cd apps/api
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 启动基础设施 (Postgres / Redis / Qdrant)
docker compose up -d postgres redis qdrant

# 数据库迁移
alembic upgrade head

# 启动 API
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 测试

```bash
cd apps/api

# 单元测试
python -m pytest tests/agent/test_simple_responder.py tests/agent/test_agent_graph.py -q

# 集成测试 (需要 Postgres + Redis + Qdrant 运行)
python -m pytest tests/agent/integration -q -m integration

# 全部 agent 测试
python -m pytest tests/agent -q

# RAG 离线 eval (mock deterministic embeddings)
make eval-rag

# 真实 OpenAI 校准 (需要 OPENAI_API_KEY)
make eval-rag-real
```

### 环境变量

```bash
# 必需
export ANTHROPIC_API_KEY=sk-ant-...
export DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/lumen
export REDIS_URL=redis://localhost:6379/0
export QDRANT_URL=http://localhost:6333

# 可选 (M1 默认使用 Anthropic Claude Haiku 4.5)
export DEFAULT_LLM_MODEL=claude-haiku-4-5
export OPENAI_API_KEY=sk-...   # 用于 RAG eval 或 OpenAI provider
```

### 容器化 + CI (Stage 10)

```bash
# 一键拉起 基础设施 + API + 前端 (Web Widget 同源代理 + WS)
cd deploy
docker compose up --build

# 仅基础设施 (本地开发用)
docker compose up -d postgres redis qdrant

# API 容器: http://localhost:8000  Web 容器: http://localhost:8080
```

- **后端镜像** `apps/api/Dockerfile` — 多阶段构建 (wheel + 精简 runtime),含 `/health` HEALTHCHECK,`CMD uvicorn main:app`
- **前端镜像** `apps/web/Dockerfile` + `nginx.conf` — 静态 SPA + `/api/v1/` 同源代理 (含 WebSocket upgrade),`VITE_API_BASE_URL` 通过构建参数注入
- **CI** `.github/workflows/ci.yml` — 五个 job:`backend` (ruff + mypy + 单测)、`backend-integration` (真实 Postgres/Redis/Qdrant service)、`frontend` (lint + type-check + test + build)、`web-sdk` (type-check + test + build)

### 可观测性 (Stage 10)

- `GET /metrics` — Prometheus text 格式,暴露 `http_requests_total` (method/path/status) + `http_request_duration_seconds` 直方图;动态路径段 (ULID/数字 id) 自动折叠为 `/:id`,避免 label 基数爆炸
- 中间件 re-raise 异常,5xx 由 FastAPI 统一响应并计数,不影响错误处理链

## 关键设计

### 多租户隔离 (三层防御)
1. **DB 层**: 每个查询 `WHERE tenant_id = :tenant_id`
2. **Qdrant 层**: `MUST` filter on `tenant_id` + `knowledge_base_id`
3. **应用层**: 跨租户访问 → `None` → `404` (反枚举)

### PII 日志规范
- 所有日志 `core.logging.get_logger` (structlog kwargs)
- 仅记录不透明 ID (ULID) + 计数 + 错误类型
- **永不** 记录客户消息原文、邮箱、异常 repr、`exc_info=True`
- `error_message` 列只存 `type(exc).__name__`

### Agent Runtime (Stage 7)
- **LangGraph `StateGraph`** 线性流:`START → retrieve → llm → (escalation_node | END)`
- **闭包工厂模式** DI: `make_retrieve_node(rag_service)` / `make_llm_node(llm_client_factory, model)` / `make_escalate_tool(...)`
- **tool tenant 注入**:`module-level _escalation_ctx = ContextVar(...)`,`respond()` 在 `graph.ainvoke` 前 `set`,finally 复位
- **失败非致命**: RAG 失败 → empty context;LLM 失败 → `FALLBACK_MESSAGE`;tool 失败 → LLM 原文 fallback
- **Metrics**:`_wrap_with_metrics` 发 `agent.graph.invoke.{started,completed,failed}`,带 `duration_ms` + `turn_kind ∈ {rag_hit, no_rag, escalated}`

### LLM 流式输出 (LLM 层, Stage 10)
- `BaseProvider.stream` / `LLMClient.stream_chat` — SSE 流式：Anthropic `message_delta` 与 OpenAI `delta.content` chunk 分别解析，`yield` 文本增量，末尾 `yield` 一个携带累计文本 + token 用量 + finish reason 的 `ChatResponse`
- 流式中断不可续传，不重试；错误映射为既有 typed 异常（`RateLimited` / `InvalidRequest` / `ProviderUnavailable`）
- 流式不覆盖 tool-use 回合（走 `chat()`）；`stream_chat` 在末尾记录用量（复用 `UsageRecorder`）
- ⏳ 尚未接到客户会话 WS（`message.delta` 帧）—— 见「已知技术债」

### 知识库 + RAG (Stage 6)
- 文档解析 → 多模态增强 (code/image/table blocks) → 长度切片 + overlap
- OpenAI `text-embedding-3-small` (1536 维),semaphore cap 4 并发
- Qdrant point_id 用 `uuid.uuid5(NAMESPACE, chunk_id)`(确定性 + 跨进程幂等)
- Chunk.id 用 `SHA-256(article_version_id || chunk_index)[:26]`
- 上传 50 MiB 硬上限 + 1 MiB chunked read(溢出 413 BEFORE parser)
- 并发 reupload `SELECT FOR UPDATE` 防 UNIQUE 撞车

## API 端点 (M1)

### 系统
- `GET /health` — 依赖健康检查 (200 / 503)
- `GET /metrics` — Prometheus 指标 (text 格式)

### Auth
- `POST /api/v1/auth/login` — 颁发 JWT

### Conversations
- `GET /api/v1/conversations` — 管理员列表 (admin)
- `GET /api/v1/conversations/inbox` — 我的会话 (agent / admin)
- `GET /api/v1/conversations/{id}` — 详情 (admin)
- `GET /api/v1/conversations/{id}/messages` — 消息列表
- `POST /api/v1/conversations/{id}/assign` — 转人工 (admin)
- `POST /api/v1/conversations/{id}/return-to-ai` — 交还 AI (admin)
- `POST /api/v1/conversations/{id}/close` — 关闭 (admin)

### Channels (admin)
- `POST /api/v1/channels`
- `GET /api/v1/channels`
- `GET /api/v1/channels/{id}`
- `PATCH /api/v1/channels/{id}`
- `DELETE /api/v1/channels/{id}` (软删)

### Knowledge Base
- `POST /api/v1/knowledge/knowledge-bases` — 新建 KB
- `GET /api/v1/knowledge/knowledge-bases` — 列表
- `GET /api/v1/knowledge/knowledge-bases/{id}`
- `PATCH /api/v1/knowledge/knowledge-bases/{id}`
- `DELETE /api/v1/knowledge/knowledge-bases/{id}`
- `POST /api/v1/knowledge/knowledge-bases/{kb_id}/articles` — 新建文章
- `POST /api/v1/knowledge/knowledge-bases/{kb_id}/articles/upload` — 上传文档
- `POST /api/v1/knowledge/articles/{id}/upload` — reupload
- `GET /api/v1/knowledge/...` — 文章 CRUD (list / get / patch / delete)
- `POST /api/v1/knowledge/articles/{id}/reindex` — 强制重建索引

### Web Widget
- `POST /api/v1/widget/token` — 颁发匿名 token
- `GET /api/v1/widget/channels/{id}/ws` — WebSocket 长连

### Agent (Stage 8)
- `GET /api/v1/agents/me` — 当前用户身份
- `POST /api/v1/conversations/{id}/messages` — 坐席回复 (agent / admin)
- `GET /api/v1/agents/queue` — 队列 (PENDING 未分配会话,可按 `status` 过滤)
- `POST /api/v1/agents/conversations/{id}/claim` — 原子认领 (SELECT FOR UPDATE + status + assigned_agent_id 检查)
- `POST /api/v1/agents/conversations/{id}/suggest-reply` — AI 建议回复 (READ-ONLY, 返回 suggested_text + citations + turn_kind)

## 已知技术债 / Stage 10+ 关注点

1. **`_reset_db_singletons` autouse fixture** 在多个集成测试文件重复 — Stage 10 集中到 `apps/api/tests/conftest.py`
2. ~~**`require_admin` / `require_agent_or_admin` 历史 inline 副本**~~ — Stage 8.1 已整合到 `auth/dependencies.py`
3. **真实 OpenAI M1 阈值校准** — `make eval-rag-real` 路径待 Stage 10 实施
4. **多模态 RAG** — `_log_ocr_todo_once` 留待 Stage 7+ 接 vision model
5. **跨并发 ContextVar 测试** — 当前 asyncio 单 task 假设,worker pool 共享 task 时需加 `asyncio.gather` 回归
6. **第二 tool / 工具循环** — 当前 `tool_calls[:1]` first-wins,第二个 tool 时改 loop-until-no-tool-calls
7. **LLM 流式已到引擎层未接 WS** — provider/client 流式已实现 (`test_client_stream.py` 等),客户会话 `message.delta` 帧待接入

## 仓库信息

- 远程: `git@github.com:leiak/Lumen-AI-Support-Agent.git` (origin/main)
- 分支约定: `main` 是唯一长期分支,M1 阶段所有 commit 直接 linear 到 main
- 提交约定: `<type>(scope): subject` — 例 `feat(7.1)`, `fix(conversation)`, `test(knowledge)`

## 许可

(待定 — 内部项目)
