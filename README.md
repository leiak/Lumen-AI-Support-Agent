# Lumen AI Support Agent

> 智能化多租户 B2B SaaS 客服系统 — SaaS / 软件企业的 AI 深度介入客服平台,服务外部客户(Web Widget、邮件、企业 IM)的同时为内部坐席提供统一工作台。

[![status](https://img.shields.io/badge/M1-Stages_1--11_complete-brightgreen)]()
[![tests](https://img.shields.io/badge/agent_tests-440%2B_passed-brightgreen)]()
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
| 10 | 集成 + 可观测性 (LLM streaming → WS `message.delta` + 可配置 embedding + Doubao 路由 + 真实 RAG eval 阈值校准) | ✅ | 75 streaming + RAG eval |
| 11 | M1 收尾 (request_id 贯穿 + 业务指标 + /health 拆分 + demo 录屏 + CI 镜像构建) | ✅ | +20 core + 5 health |
| 12 | M2.A — `search_internal_kb` 工具 + `llm_node` tool loop(兑现 M1 tech-debt #6) | ✅ | 131 agent tests |
| 13 | M2.A — Ticket 领域 (独立 3 表 + 7 态状态机 + SLA 策略 + auto-create) | ✅ | 33 ticket tests |
| 14 | M2.A — 实时质检 (Arq `qa_judge_worker` + 独立小 Judge LLM + `lumen_qa_*` 指标) | ✅ | 36 qa tests |
| 15 | M2.A — 收尾 (README + demo-act4 + 已知技术债 + memory) | ✅ | 0 new tests, 3 demo screenshots |
| 16 | M2.B — Email 渠道 (SES webhook 路由 + 纯文本回复 + 自动回信 + thread/dedup) | ✅ | 4 parser + 5 outbound + 1 inbound integration |
| 17 | M2.B — 多模态 KB (PNG/PDF + Qdrant vision collection + RRF 检索 + 上传 API) | ✅ | ~20 multimodal unit + integration |
| 18 | M2.B — 历史会话挖掘 (HDBSCAN clusterer + KBDraftGenerator + Arq Sunday worker + admin approve/reject API) | ✅ | 10 clusterer/draft + 11 admin/worker integration |
| 19 | M2.B — 收尾 (README + demo-act5 + 已知技术债 #18-20 + memory) | ✅ | 0 new tests, 3 demo stub PNGs |

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
   │  Qdrant MUST     │    │  tool loop (max=5) │    │  human           │
   │  filter          │    │  CHAT_TEMP=0.7     │    │  search_internal_│
   │                  │    │  MAX_TOK=512       │    │  kb (Stage 12)   │
   │                  │    │                    │    │  (ContextVar-    │
   │                  │    │                    │    │   bound tenant)  │
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

# 真实 embedding 校准 (需要 OPENAI_API_KEY 或 DOUBAO_API_KEY)
# Stage 10.3 用 20 篇合成文章 + 50 查询算出 DEFAULT_SCORE_THRESHOLD=0.45
make eval-rag-real

# 录制 demo 截图(需 Postgres+Redis+Qdrant up,seed 已跑)
bash tests/e2e/scripts/record-demo.sh  # → 11 张 images/demo-act{N}-{step}.png
# PowerShell: .\tests\e2e\scripts\record-demo.ps1
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

### 可观测性 (Stage 10 + Stage 11)

- `GET /metrics` — Prometheus text 格式,暴露:
  - HTTP 层:`http_requests_total` (method/path/status) + `http_request_duration_seconds` 直方图;动态路径段 (ULID/数字 id) 自动折叠为 `/:id`,避免 label 基数爆炸
  - 业务层(Stage 11.3):`lumen_messages_total{role}` + `lumen_llm_calls_total{provider,model,outcome}` + `lumen_llm_tokens_total{provider,model,direction}`。**故意不加 tenant_id label** — 避免 Prometheus 基数爆炸,租户维度走 `llm_usage` 表
- `GET /health/live` — 进程存活,**无 IO**(用于 k8s livenessProbe)
- `GET /health/ready` — 依赖就绪,503 on degraded(用于 k8s readinessProbe / LB)
- `GET /health` — `/health/ready` 的别名,保留向后兼容
- 三个 health 端点都返回 `version` + `started_at` + `git_sha`(Stage 11.4,镜像 build 时通过 `--build-arg GIT_SHA=$(git rev-parse --short HEAD)` 注入)
- 中间件 re-raise 异常,5xx 由 FastAPI 统一响应并计数,不影响错误处理链
- 每个 HTTP 请求生成 `request_id`(`X-Request-ID` 透传或 ULID),出现在响应头 + 所有 structlog 日志 + 后续业务指标中(Stage 11.2)

### QA 质检指标 (M2.A / Stage 14)

- `lumen_qa_scores_total{dimension, bucket}` — Judge LLM 给 AI 回复的 4 维度评分计数(`dimension ∈ {relevance, safety, faithfulness, overall}`, `bucket ∈ {low, medium, high}`)
- `lumen_qa_flagged_total` — 任一维度 < 0.4 的 AI 回复数(M3 接 alerting)
- `lumen_qa_judge_failures_total{reason}` — Judge 调用失败计数(`reason ∈ {timeout, malformed, exception}`)
- `lumen_sla_breached_total{priority}` — 触发 SLA 超时未响应的工单计数(`priority ∈ {P0, P1, P2, P3}`;M3 接 PagerDuty)
- `lumen_qa_judge_latency_seconds` — Judge 调用耗时直方图(无 labels,~12 buckets)

QA worker 通过 `app.metrics_registry` 注册,`GET /metrics` 端点直接暴露。Judge LLM 与主 Agent LLM 解耦 — Agent 用 MiniMax Haiku 路由时,Judge 可独立配 MiniMax / DeepSeek / 本地小模型。

### Demo 录制

详细演示流程见 [`docs/demo-script.md`](docs/demo-script.md)。自动化截图见
[`tests/e2e/scripts/record-demo.sh`](tests/e2e/scripts/record-demo.sh)(或 Windows 的
`record-demo.ps1`)—— 产 11 张 PNG 到 `images/demo-act{N}-{step}.png`。

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

### Tickets (M2.A / Stage 13)
- `GET /api/v1/tickets/{ticket_id}` — 详情 (admin / agent;404 on 缺失或跨租户,反枚举)
- `POST /api/v1/tickets/{ticket_id}/transition` — 状态机迁移 (`200` 成功 / `404` 缺失 / `409 Conflict` 非法迁移)
- `GET /api/v1/tickets/{ticket_id}/events` — 工单审计事件日志 (按时间倒序;空列表 = `200 []`)

## 已知技术债 / Stage 10+ 关注点

1. **`_reset_db_singletons` autouse fixture** 在多个集成测试文件重复 — Stage 10 集中到 `apps/api/tests/conftest.py`
2. ~~**`require_admin` / `require_agent_or_admin` 历史 inline 副本**~~ — Stage 8.1 已整合到 `auth/dependencies.py`
3. ~~**真实 OpenAI M1 阈值校准** — `make eval-rag-real` 路径待 Stage 10 实施~~ — Stage 10.3 完成 (`eval-rag-real` 现在跑真实 Doubao embedding,校准 `DEFAULT_SCORE_THRESHOLD=0.45`)
4. **多模态 RAG** — `_log_ocr_todo_once` 留待 Stage 7+ 接 vision model
5. **跨并发 ContextVar 测试** — 当前 asyncio 单 task 假设,worker pool 共享 task 时需加 `asyncio.gather` 回归
6. ~~**第二 tool / 工具循环** — 当前 `tool_calls[:1]` first-wins,第二个 tool 时改 loop-until-no-tool-calls~~ — Stage 12 已替换为完整 tool loop(最多 5 轮)
7. ~~**LLM 流式已到引擎层未接 WS** — provider/client 流式已实现,客户会话 `message.delta` 帧待接入~~ — Stage 10.1 完成 (`src/channel/inbound.py` 已有 `_broadcast_ai_delta` / `_stream_delta`,2 个 E2E 测试覆盖)
8. **业务指标当前无 tenant_id label** — Stage 11.3 故意不加避免 Prometheus 基数爆炸;按需租户维度走 `llm_usage` 表(账单路径)
9. **demo mp4 留 presenter** — Stage 11.5 自动化脚本只产 11 张 PNG;mp4 需要 presenter + 音频,自动化做不出
10. **Plan 10.4 LLM/tracing spans 留 M3** — M1 close-out 只做 request_id 贯穿(Stage 11.2);OpenTelemetry 留到 M3 LLM Gateway
11. **Plan 10.7 K8s manifests 留 M3/M4** — M1 docker-compose 单区域部署够用
12. **`search_internal_kb` 只查 KB 文本** — Stage 12 只检索 Qdrant 文本块;Stage 12.5/M2.B 接 vision/多模态(图/PDF 图表)
13. **Ticket 工单当前无 UI** — 后端完整,前端 `/tickets/{id}` 页面待补(M2.A 之前用 `/inbox/{id}` 旁边 panel 占位,`tests/e2e/demo-act4-02.spec.ts` 已写契约 + `test.skip` 等前端就绪)
14. **Judge 模型假阳性** — QA Judge 阈值默认 0.3,首批用真实 AI 回复跑 spot-check 后调;`lumen_qa_flagged_total` 曲线要人工盯
15. **M2.B 占位** — 邮件渠道 + 多模态 + 历史会话挖掘;预计 4-6 周
16. **Stage 16 SES inbound webhook 用 `X-Tenant-ID` header 路由** — header 可被伪造;生产应改 SNS 签名 / SigV4 / 收件人反查 (`Channel.config_json.tenant_id`) 任一方式
17. **Stage 18 admin KB drafts API 未鉴权** — `tenant_id` / `reviewer_id` 当前是 query 参数,生产必须改 JWT `Depends(get_current_user)`,从 claims 读 tenant + reviewer(`apps/api/src/admin/api.py`)
18. **PDF 文本切片未索引到 `kb_vectors`** — Stage 17 仅图片向量入 `kb_image_vectors`;PDF 的 text chunks 当前只记录计数,不留 Qdrant 向量(待 M3 接 text + image 双索引)
19. **Vision embedding 仅 Doubao** — `DoubaoVisionEmbedder` 是唯一视觉编码器;OpenAI CLIP / Anthropic Claude Vision / 开源 SigLIP 适配器待 M3 多模型路由
20. **KB 草稿 admin SPA UI 推迟到 M3** — Stage 18 后端 API + 数据模型完整(`/admin/kb-drafts` list/detail/approve/reject),admin 前端页面留 M3 实施(现阶段 admin 用 DB / curl 复核)

## 仓库信息

- 远程: `git@github.com:leiak/Lumen-AI-Support-Agent.git` (origin/main)
- 分支约定: `main` 是唯一长期分支,M1 阶段所有 commit 直接 linear 到 main
- 提交约定: `<type>(scope): subject` — 例 `feat(7.1)`, `fix(conversation)`, `test(knowledge)`

## 许可

(待定 — 内部项目)
