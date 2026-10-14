# Lumen AI Support Agent

> 智能化多租户 B2B SaaS 客服系统 — SaaS / 软件企业的 AI 深度介入客服平台,服务外部客户(Web Widget、邮件、企业 IM)的同时为内部坐席提供统一工作台。

[![status](https://img.shields.io/badge/M1-Stages_1--11_complete-brightgreen)]()
[![tests](https://img.shields.io/badge/agent_tests-440%2B_passed-brightgreen)]()
[![python](https://img.shields.io/badge/python-3.11+-blue)]()
[![framework](https://img.shields.io/badge/FastAPI-0.110+-009688)]()

## 项目状态

按功能模块组织;每行是**已 ship 的能力**,括号内是实现该能力的 stage 编号 + 关键 commit(用于回溯),右侧是**该模块累计的测试数**(非 commit 单数)。每个模块细节在 §关键设计 / §已知技术债 / memory 文件里有完整 ADR;图片集中在 §Admin 工作台截图 / §Demo 录制。

| 模块 | 能力 | 状态 | 测试 |
|------|------|------|------|
| **基础设施** | FastAPI + Postgres + Redis + Qdrant 容器化(Stage 1);租户 + JWT 鉴权 + RBAC(Stage 2);CI 镜像构建(Stage 11) | ✅ | — |
| **多渠道接入** | Channel Gateway(Stage 4: Feishu + Web Widget + WS);Email SES webhook + 自动回信 + thread/dedup(Stage 16);多模态 KB 接入 KB 上传 API(Stage 17) | ✅ | Channel 4 + SES 10 + Multimodal 20 |
| **会话 + 实时通讯** | Conversation + Message + WS 推送(Stage 5);LLM streaming → WS `message.delta` + 可配置 embedding + Doubao 路由(Stage 10);AI 失败自动 escalate 人工 + admin 全会话视图(Stage 29) | ✅ | 289+ + 75 + 8 |
| **知识库 + RAG** | 解析 → 切片 → embedding → 检索 → RAG 服务 + 真实 RAG eval 阈值校准(Stage 6 / 10);多模态 KB PNG/PDF + Qdrant vision collection + RRF 检索(Stage 17);`search_internal_kb` 工具 + LangGraph tool loop(Stage 12);retriever stale-version 过滤 + UI 引用可见(`abf7361` 修复) | ✅ | 480 + multimodal ~20 + 131 agent + 0 (M3 fix) |
| **历史会话挖掘 (KB 草稿)** | HDBSCAN clusterer + KBDraftGenerator + Arq Sunday worker + admin approve/reject API(Stage 18);**clusterer 加固** L2-normalize + euclidean + `min_samples=1` 修 Doubao 2048-dim 必产 0 cluster 阻塞(commit `8aaa767`,见 [§关键设计](#hdbscan-多-embedding-model-注意事项));"立即执行" 按钮 + 端到端 live verify | ✅ | 10 clusterer + 11 admin/worker |
| **Agent Runtime** | LangChain + LangGraph + `escalate_to_human` tool(Stage 7);LLM 流式输出(Stage 10);streaming async seam 修 budget 旁路(Bug #3) | ✅ | 64 + 75 + 0 (Bug #3 fix) |
| **坐席工作台** | REST API: me / 发送消息 / queue / claim / suggest-reply(Stage 8);前端 SPA + Web Widget SDK + iframe UI + CORS + Playwright E2E + demo script(Stage 9);admin `/inbox` 全会话视图(Stage 29) | ✅ | 168+ + 116 backend + 98 frontend + 10 e2e |
| **Ticket / SLA** | 独立 3 表 + 7 态状态机 + SLA 策略 + auto-create(Stage 13);admin SPA UI `admin-ticket-detail.tsx` + demo-act4-02 spec 启用(Stage 20 #20) | ✅ | 33 ticket + 17 admin + 13 web vitest |
| **QA 实时质检** | Arq `qa_judge_worker` + 独立小 Judge LLM + `lumen_qa_*` 指标(Stage 14) | ✅ | 36 qa |
| **LLM Gateway (resolver seam)** | **M4.A** provider registry + multi-provider routing + `with_config()` per-call pinning + resolver 抽象(Stage 21);**M4.B** Fallback Resolver + `FallbackChainExhausted` + per-step `lumen_llm_fallback_attempts_total`(Stage 22);**M4.C** Tenant Resolver (BYOK, Fernet + LRU/TTL 缓存 + 严格模式)(Stage 23);**M4.D** Budget Layer (per-tenant 月度 token hard cap + snapshot 缓存 + 软警告 + 配额 grant + PerModel breakdown + 429 gate + Docker 部署)(Stage 24);**M5.1** per-tenant fallback chain (Stage 27);**M5.2** per-tenant model override (Stage 28);**M5.3** config 审计日志(append-only 表 + 原子性保证 + GET audit endpoint)(Stage 31);**M5.3** per-tenant 自动分配策略(manual / least_busy,commit `85ff2f5` 时代,SPA `/admin/tenant-settings` 卡片) | ✅ | 8+12+5+5+6+6+4 + 10+5+12+6+e2e+3 + 4+5+9+7+9+3 + 1+5+4+6+2+7+3+(Pack A 18)+(Pack B 27) + 47 + 23 + 41 + 20 = **~280 Gateway tests** |
| **可观测性** | request_id 贯穿 + 业务指标 + `/health` 拆分(Stage 11);QA `lumen_qa_*` 指标(Stage 14);**M6** OpenTelemetry tracing (SDK + `@traced` + W3C `traceparent` + Arq 传播 + OTel Collector + Tempo + PII 白名单 + 端到端 e2e) | ✅ | 20 core + 5 health + 36 qa + 73 unique / 89 cases + 1 e2e |
| **运维 + 测试基础设施** | 端到端 demo 录屏(Stage 11/15/19);Admin SPA 17 张截图(`shot_all_admin_pages.py`);Tier 1/2/3 14 任务 gap-fill(admin tickets / channel CRUD / LLM Config / budget mutation / i18n / Playwright CI / cross-domain search);Quick Wins A/B/C/D(HttpClientPool 故障注入 / ruff sweep / demo-act4 启用 / 一键全栈脚本);`scripts/bring-up.sh` + `teardown.sh` + `wait-ready.sh`;ruff sweep 41 → 0;`seed_mining_demo_data.py` 多主题 seed | ✅ | 272 (Tier 1+2+3) + 11 (QW) + 0 (seed);**总测试 ~1700+ passed** |
| **探索性测试 (8 轮)** | `tests/e2e/scripts/explore.py` 端到端冒烟:多轮上下文 / 边界输入 / 并发 / Budget / KB RAG / suggest-reply / Ticket / 流式;发现 3 个产品级 bug(widget external_user_id `9b450be` / KB seed indexing `c729f19` / stream_chat 旁路 budget `cdb90ed`),3 个全部已修;Round 4 验证 cap fires | ✅ | 28 OK / 2 WARN / 0 FAIL / 13 INSIGHT |

**总测试规模: ~1700+ passed** (含 480 KB RAG / 289+ session / 280 LLM Gateway / 131 agent / 100+ admin / 73 OTel / 36 QA / 33 ticket / 20 misc modules)。

阶段 anchor: `Stage 1-11` M1 基础设施 / `Stage 12-15` M2.A 客服运营 / `Stage 16-20` M2.B 多模态 + 历史挖掘 + M3 技术债 / `Stage 21-24` M4 核心 LLM Gateway / `Stage 25-26` Tier + Quick Wins / `Stage 27-31` M5 per-tenant 定制 / `Stage 32` M6 tracing。阶段编号不再一一对应 commit 数 — 同一个 stage 可能跨多 commit(M4.D 4 个 pack + Docker 验证 / M5.3 5 commit / M6 13 commit / Tier 1+2+3 14 任务)。

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

# 单 spec 跑通(无需 webServer 启动,默认已 docker stack 在跑)
DEMO_WEB_URL=http://localhost:8080 \
  pnpm exec playwright test --config=tests/e2e/playwright.docker.config.ts \
    tests/e2e/demo-act4-02.spec.ts
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

#### 一键全栈 bring-up / teardown

```bash
# 完整拉起:docker compose + wait + 4 脚本种子链 + web-sdk 构建
scripts/bring-up.sh                  # 首次或干净环境
scripts/bring-up.sh --no-build       # 跳过 docker build(用缓存镜像)
scripts/bring-up.sh --no-seeds       # 只拉服务,不跑种子
scripts/bring-up.sh --seed-only      # 跳过 docker,只跑种子链
scripts/bring-up.sh --help           # 看所有 flag

# 干净关停
scripts/teardown.sh                  # docker compose down,保留 volume
scripts/teardown.sh --volumes        # 同时清掉 named volume(DATA LOSS)
scripts/teardown.sh --images         # 同时 prune dangling 镜像
```

`bring-up.sh` 会按顺序跑 4 个种子脚本(`seed.py → seed_budget.py →
seed_demo_rich.py → extend-seed.py`),每个都支持 `--help` 单独使用:

```bash
cd apps/api && uv run python ../../tests/e2e/scripts/seed.py --help
cd apps/api && uv run python ../../tests/e2e/scripts/seed_budget.py --help
cd apps/api && uv run python ../../tests/e2e/scripts/seed_demo_rich.py --help
apps/api/.venv/Scripts/python.exe apps/example-integration/scripts/extend-seed.py --help
```

退出码:`0` 成功 · `1` 未捕获异常 · `2` prereq 缺失(种子链上游未跑)
· `64` 未知 CLI flag。

#### 手动步骤(等价的细粒度版本)

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

### 可观测性 (Stage 10 + Stage 11 + M6)

- `GET /metrics` — Prometheus text 格式,暴露:
  - HTTP 层:`http_requests_total` (method/path/status) + `http_request_duration_seconds` 直方图;动态路径段 (ULID/数字 id) 自动折叠为 `/:id`,避免 label 基数爆炸
  - 业务层(Stage 11.3):`lumen_messages_total{role}` + `lumen_llm_calls_total{provider,model,outcome}` + `lumen_llm_tokens_total{provider,model,direction}`。**故意不加 tenant_id label** — 避免 Prometheus 基数爆炸,租户维度走 `llm_usage` 表
- `GET /health/live` — 进程存活,**无 IO**(用于 k8s livenessProbe)
- `GET /health/ready` — 依赖就绪,503 on degraded(用于 k8s readinessProbe / LB)
- `GET /health` — `/health/ready` 的别名,保留向后兼容
- 三个 health 端点都返回 `version` + `started_at` + `git_sha`(Stage 11.4,镜像 build 时通过 `--build-arg GIT_SHA=$(git rev-parse --short HEAD)` 注入)
- 中间件 re-raise 异常,5xx 由 FastAPI 统一响应并计数,不影响错误处理链
- 每个 HTTP 请求生成 `trace_id`(M6 升级自 request_id;`X-Request-ID` 透传或 ULID,出现在响应头 + 所有 structlog 日志 + 后续业务指标中)。M6 起响应头新增 `X-Trace-Id`(W3C 32-char hex),与 `X-Request-ID` 同值向后兼容
- **OpenTelemetry distributed tracing (M6)** — `apps/api/src/observability/` package:
  - **Hybrid instrumentation**:`httpx` auto (M6.1) 给 LLM provider outbound HTTP 免费发射;`tracer.start_as_current_span(...)` 手动控制 PII-safe attribute 集合。FastAPI / SQLAlchemy / asyncpg 故意**不**自动 instrument(噪音)
  - **SDK init** — `OTEL_*` env vars(`OTEL_SERVICE_NAME` / `_RESOURCE_ATTRIBUTES` / `_EXPORTER_OTLP_ENDPOINT` / `_EXPORTER_OTLP_PROTOCOL` / `_TRACES_SAMPLER` / `_TRACES_SAMPLER_ARG` / `_PYTHON_LOG_CORRELATION`)。`OTEL_SDK_DISABLED=true` / `OTEL_TRACES_SAMPLER=always_off` 是官方 escape hatch
  - **本地 dev** — docker-compose `otel-collector` :4317/:4318 + `tempo` :3100 (host :33000) + `tempo-data` volume。`http://localhost:33000` 看 trace UI
  - **生产** — 只改 `OTEL_EXPORTER_OTLP_ENDPOINT` 指向 managed backend(Honeycomb / Grafana Cloud / Tempo Cloud / Datadog APM 等),代码不动。采样率默认 1.0(dev),生产建议 0.1
  - **Span hierarchy** — `http.request` (根) → `llm.chat` / `llm.chat.stream` → outbound httpx child / `agent.node.<name>` → `agent.tool.<name>` / `channel.inbound` → `channel.outbound` / `ticket.<verb>` / `admin.llm_config.<verb>` / `worker.<job_name>` (Arq 跨进程 trace 传播)
  - **`@traced` decorator** (M6.2) — 装饰 TicketService CRUD + admin repo mutations + 任何 future hot-path 函数。Static scan 守门
  - **`TraceContextMiddleware`** (M6.6) — 替代 M1 `request_context_middleware`,读取 W3C `traceparent` 头,emit `http.request` 根 span,绑定 `trace_id` 进 structlog contextvars
  - **Arq propagation** (M6.5) — `enqueue_with_trace` 在 HTTP 端把 `traceparent` 注入 `job_ctx`,`job_span` 在 worker 端用 `extract_trace_from_job` 恢复,worker span 是 HTTP 请求的子 span
  - **PII 三层防御**:
    1. `PII_WHITELIST` (`apps/api/src/observability/tracing.py:40-62`) — 12 个允许的 attribute key(`tenant_id` / `provider_name` / `model` / `llm.tokens.{input,output,cost_usd,latency_ms}` / `http.{method,route,status_code,latency_ms}` / `channel.name` / `conversation.id` / `worker.{job_name,attempt}` / `agent.id` / `ticket.id` / `outcome`)。新增 attribute 必须先加这里
    2. **Static scan** `tests/observability/test_pii_whitelist.py` — whole-key-boundary regex(`(?:^|[."])(token)(?:$|[."])`)扫 9 个 subdir。子串扫描(如 `if token in line`)会在 `llm.tokens.input` 上误报,已被 M6.2 P1 #2 修复
    3. **Runtime filter** — `@traced` 装饰器在 emit 前 drop 非白名单 key + log warning(不是只 log)。`tracer.start_as_current_span` 手动调用需自查
    4. **E2E PII scan** `tests/e2e/scripts/test_otel_e2e.py` — 驱动 widget 流量后查 Tempo,逐 attribute key 跑同一 whole-key regex
  - **Forbidden tokens**(永远不能当 attribute,无论 white/black):`api_key` / `message.content` / `customer.email` / `customer.name` / `customer.ip` / `customer.phone` / `password` / `secret` / `token`(作为 bare token)

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

### Admin 工作台截图 (M5.3)

`images/admin_pages/` 下 17 张 SPA 截图,以 admin (`admin@example.com` / `Demo123!`) 登录后抓取,
覆盖全部 12 个 admin 页面 + 5 个通用页面。Playwright 脚本:[`images/shot_all_admin_pages.py`](images/shot_all_admin_pages.py)。

**主要功能预览** — 核心 6 张直接渲染在 README:

#### 收件箱 — AI 失败后自动进入待分配队列
![收件箱](images/admin_pages/01_inbox.png)

#### 租户设置 — 自动分配策略(least-busy,见 Row 30)
![租户设置](images/admin_pages/19_admin_tenant_settings.png)

#### LLM 链路 — per-tenant fallback chain (M5.1)
![LLM 链路](images/admin_pages/16_admin_llm_chain.png)

#### LLM 配置 — per-tenant model override (M5.2) + audit log
![LLM 配置](images/admin_pages/17_admin_llm_configs.png)

#### 预算 — token 硬上限 + 软告警 (M4.D)
![预算](images/admin_pages/12_admin_budget.png)

#### 工单详情 — SLA timer + 状态机 (M2.A / Stage 13)
![工单详情](images/admin_pages/14_admin_ticket_detail.png)

<details>
<summary>完整 17 张索引(展开)</summary>

| 截图 | 说明 |
|---|---|
| [`admin_pages/01_inbox.png`](images/admin_pages/01_inbox.png) | 收件箱 — 6 条会话,3 条 "未分配"(auto-assign 策略待启用) |
| [`admin_pages/02_inbox_detail.png`](images/admin_pages/02_inbox_detail.png) | 会话详情 — 历史消息 / 建议回复 / assign 按钮 |
| [`admin_pages/03_kb.png`](images/admin_pages/03_kb.png) | 知识库列表 |
| [`admin_pages/04_kb_detail.png`](images/admin_pages/04_kb_detail.png) | KB 详情 — 文章列表 |
| [`admin_pages/05_settings.png`](images/admin_pages/05_settings.png) | 个人设置 |
| [`admin_pages/10_admin_dashboard.png`](images/admin_pages/10_admin_dashboard.png) | 仪表盘 — 业务指标 |
| [`admin_pages/11_admin_sla_policies.png`](images/admin_pages/11_admin_sla_policies.png) | SLA 策略 — 4 条策略 (P0/P1/P2) |
| [`admin_pages/12_admin_budget.png`](images/admin_pages/12_admin_budget.png) | 预算 — 20000 hard cap / 10000 soft warn |
| [`admin_pages/13_admin_tickets.png`](images/admin_pages/13_admin_tickets.png) | 工单列表 — in_progress / waiting_customer / resolved |
| [`admin_pages/14_admin_ticket_detail.png`](images/admin_pages/14_admin_ticket_detail.png) | 工单详情 — SLA timer / 状态机 |
| [`admin_pages/15_admin_kb_drafts.png`](images/admin_pages/15_admin_kb_drafts.png) | KB 草稿 (history mining 输出) |
| [`admin_pages/16_admin_llm_chain.png`](images/admin_pages/16_admin_llm_chain.png) | LLM 链路 — per-tenant fallback chain (M5.1) |
| [`admin_pages/17_admin_llm_configs.png`](images/admin_pages/17_admin_llm_configs.png) | LLM 配置 — per-tenant model override (M5.2) + audit log |
| [`admin_pages/18_admin_users.png`](images/admin_pages/18_admin_users.png) | 用户管理 — admin / agent / supervisor / viewer |
| [`admin_pages/19_admin_tenant_settings.png`](images/admin_pages/19_admin_tenant_settings.png) | **租户设置** — 含「自动分配策略」卡片(见 Row 30) |
| [`admin_pages/20_admin_widget_config.png`](images/admin_pages/20_admin_widget_config.png) | Widget 配置 — 颜色 / CORS / 浮窗位置 |
| [`admin_pages/21_admin_search.png`](images/admin_pages/21_admin_search.png) | 跨租户搜索 — 全文检索 KB / 工单 / 会话 |

复现:登录后跑 `python images/shot_all_admin_pages.py` 即可重新生成全部 17 张。
</details>

## 故障排查

### 登录 401 / "Invalid credentials" — 找不到 admin 用户

多个 Claude session 共用同一个 docker compose stack + 同一个 postgres volume。
别人跑 BYOK / Pipeline Tenant 测试时会清表,`Acme Demo` (`01HZDEMO00000000000000000`) 跟它的
admin/agent 也会被一起带走,你这边就登录不上。

**1 行 fix**(只动 demo tenant + 用户,不动其他租户 / BYOK fixture / Pipeline Tenant):

```bash
bash scripts/reset-demo.sh
```

它会:
1. 跑 `bring-up.sh --seed-only` 重灌 4 个 seed(全部 idempotent,固定主键,重复跑安全)
2. tolerate Windows 的 `python3` vs `python` 差异(`extend-seed.py` 那步 feishu 集成不影响登录)
3. 跑完 verify 一遍:`SELECT count(*) FROM tenants WHERE id='01HZDEMO...'` + `users WHERE email='admin@example.com'`
4. 打印 "OK — Acme Demo tenant + admin user are back"

**不要**用以下 fix — 会清掉别人的数据:
- ❌ `docker compose down -v`(清所有 volume,BYOK / Pipeline tenant 全没)
- ❌ `TRUNCATE users CASCADE`(同上,级联全清)
- ❌ `bash scripts/teardown.sh --volumes`(同 down -v)

### `pnpm install --frozen-lockfile` 失败 / web image build 失败

`apps/web/Dockerfile` 把 web 当 standalone project 用,但本仓库是 monorepo — 真 lock 在根目录。
重生成 web 本地 lock:

```bash
cd apps/web
pnpm install --ignore-workspace   # 绕开 monorepo,在 web/ 目录生成本地 lock
cd ../..
docker compose -f deploy/docker-compose.yml build web
docker compose -f deploy/docker-compose.yml up -d web
```

### `relation "tenants" does not exist` / `seed.py` 直接崩

另一个 session 正在跑 `alembic down` + `alembic upgrade head`(他们的 fixture setup)。
等 5-10 秒重跑 `bash scripts/reset-demo.sh` 即可,不需要手动 migrate。

### "登录页 lookupTenant 不返回 tenant_id"

SPA 拿不到 tenant_id → 登录按钮一直 disabled。检查:
1. 输入的 email 是不是 `admin@example.com`(如果换过种子,先看 `tests/e2e/scripts/seed.py` 的 output)
2. `GET /api/v1/auth/lookup-tenant?email=...` 是否 200
3. 后端日志: `docker logs aicustomer-api-1 --tail=50 | grep lookup`

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
- **失败非致命**: RAG 失败 → empty context;LLM 失败 → `FALLBACK_MESSAGE` + 自动 escalate 进人工队列(`status=PENDING, ai_handling=False`,见 Row 29);tool 失败 → LLM 原文 fallback
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

### 历史会话挖掘 (Stage 18 / 2026-10-14 加固)
- 流程: `Arq Sunday worker → embed_texts → HdbscanClusterer → KBDraftGenerator (LLM) → KbArticleDraft (status=DRAFT) → admin approve/reject`
- **clusterer 关键不变量** (commit `8aaa767`):
  - **L2-normalize 每行到单位球 + `metric='euclidean'`** — 单位向量上 `euclidean = sqrt(2(1-cos))` 是 cosine 的严格单调变换,聚类决策数学等价。**为什么不用 `metric='precomputed'`**: sklearn 1.x 删了 BallTree 的 `metric='cosine'`;precomputed 走 pdist 内存 O(n²) ≈ 400MB @ 2048-dim × 1k points,L2-normalize + euclidean O(n·d) ≈ 80MB;且 BallTree 在 2048-dim 本来就退化成 brute-force,两路径速度持平。
  - **`min_samples=1` 默认** — 旧值 5 是为 OpenAI `text-embedding-3-small` (1536-dim 文本嵌入) raw euclidean 调的,搬到 Doubao `doubao-embedding-vision` (2048-dim 视觉嵌入) 永远找不到 core point → 0 cluster。`min_samples` 故意不暴露成 config setting,提示 operator 调 `min_cluster_size`(行业惯例: `min_samples = min_cluster_size // 2` 或 1)。
  - **零向量过滤** + **1-point 短路** + **`< min_cluster_size` 短路** — 防 NaN/Inf 污染 BallTree + 防 HDBSCAN kdtree `k must be <= n_points` 崩溃。

#### <a id="hdbscan-多-embedding-model-注意事项"></a>HDBSCAN 多 embedding-model 注意事项
- **任何 density-based clusterer 的默认参数都跟具体 embedding model 强耦合**,换 embedding 必须重测。`min_samples=5` 在 OpenAI 1536-dim 文本嵌入上能跑,在 Doubao 2048-dim 视觉嵌入上必产 0 cluster,而 0 cluster 不报错只 `processed=N, drafts_created=0` — **silent failure**。
- **生产端到端 4 步验证** (任何走 embedding 的 worker 都得做): embed 成功(无 batch limit / auth 错) → cluster 形成(HDBSCAN 至少产 1 cluster,看 `tenant_processed` 日志或直接调 clusterer) → draft 入库(LLM 调通) → API 可见。**任何一步 silent 失败都会让 `drafts_created: 0` 出现,但没有 error log**。
- **demo seed 必须是多主题** — 单主题(24 条 password-reset)是单一均匀 dense 区,HDBSCAN 正确地返回 0 cluster(不是 bug 是算法本质)。`scripts/seed_mining_demo_data.py` 现在写 45 条(15 password-reset + 15 invoice/billing + 6 misc one-off)模拟真实租户长尾。
- **embedding API batch cap 按 model 校准** — `_OPENAI_BATCH_LIMIT=2048` 对 OpenAI 成立,Doubao Ark `plan/v3` 实际 cap=10 inputs/call,超了 `Embeddings API input limit exceeded: max 10, got N`(commit `4e677b1` 修)。

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

## 已知技术债 — 分类索引

我们把已知技术债分成两类,目的不同:
- **Known limitations** — 主动选择 / 推迟,暂无具体使用场景或依赖后续 milestone
- **Tech debt follow-ups** — 已经识别 + 可执行,排在某个 milestone 的 backlog

### Known limitations (intentional deferrals)

1. **`_reset_db_singletons` autouse fixture** 在多个集成测试文件重复 — 集中到 `apps/api/tests/conftest.py` 待 follow-up
2. **多模态 RAG** — `_log_ocr_todo_once` 留待 vision model 接入
3. **跨并发 ContextVar 测试** — 当前 asyncio 单 task 假设,worker pool 共享 task 时需加 `asyncio.gather` 回归
4. **业务指标当前无 tenant_id label** — 故意不加避免 Prometheus 基数爆炸;按需租户维度走 `llm_usage` 表(账单路径)
5. **demo mp4 留 presenter** — 自动化脚本只产 PNG;mp4 需要 presenter + 音频,自动化做不出
6. **Plan 10.4 LLM/tracing spans 留 M3** — M1 close-out 只做 request_id 贯穿;OpenTelemetry 留到 M3 LLM Gateway — DONE (M6; OTel SDK + Collector + Tempo + hybrid instrumentation + W3C `traceparent` + `@traced` + PII whitelist; 42 new tests; closes known limitation line 415)
7. **Plan 10.7 K8s manifests 留 M3/M4** — docker-compose 单区域部署够用
8. **`search_internal_kb` 只查 KB 文本** — 当前只检索 Qdrant 文本块;vision/多模态(图/PDF 图表)待 M2.B
9. ~~Ticket 工单当前无 UI~~ — Stage 20 #20 admin SPA UI 已 ship `/admin/tickets/{id}` (`admin-ticket-detail.tsx`),demo-act4-02 spec 已启用(`1713c8e`),PNG 已捕获到 `images/demo-act4-02.png`
10. **Judge 模型假阳性** — QA Judge 阈值默认 0.3,首批 spot-check 后调;`lumen_qa_flagged_total` 曲线要人工盯
11. **`stream_chat()` skips fallback** — 设计选择 (mid-stream switch 不可靠);M4.B+ 视客户用例再考虑
12. **No per-step budget** — `attempt_timeout_s` 单一值应用到所有 step;Deferred
13. **No chain-warm metrics** — 只 success/failure 计数,无滚动延迟 / 错误率 — DONE (M6; chain-warm latency / error rate now visible in `llm.chat` span attributes per-call; per-step histograms still a follow-up)
14. **No explicit cache invalidation on admin write** — ≤ 60s TTL,操作员在 TTL 窗口内看到旧配置
15. **Fernet key rotation** — master key 启动时加载一次;rotation 需要 restart + re-encrypt + update env,无 zero-downtime rotation
16. ~~**No audit log** — `tenant_llm_configs` 更新无审计~~ — DONE (M5.3; `tenant_llm_config_audit_log` append-only table + `GET /api/v1/admin/tenants/{id}/llm-configs/audit` endpoint + atomicity-guaranteed wrapper; 41 new tests + 5 commit; closes tech debt line 372)
17. **No per-tenant fallback chain** — chain 是 project-wide
18. **`enabled=FALSE` semantics** — 当前 "skip in resolver"
19. **No per-tenant model override** — model 是 project-default per provider
20. **Demo / staging seeding** — demo tenant 必须通过 admin API
21. **Post-mortem gate observes after chain exhausted** — 不 preempt mid-chain retries;pre-flight rejection 需更深的 M4.B `FallbackResolver` 耦合
22. **Non-UTC anchored tenants see breakdown drift on month-boundary days** — `PerModelBreakdown` 按 UTC month 聚合,snapshot 按 anchored month;修需要 `tz_name` 穿透到 `PerModelService`,出 Pack B scope
23. **No non-LLM cost budget** — SES / SMS / KB retrieval / embedding 未计入 monthly cap
24. **`apps/api/src/workers/` 目录是死的** — M6.5 时确认实际 worker code 都在 `src/qa/worker.py`(`WorkerSettings` 仍在 `src/workers/__init__.py` re-export);PII static scan 把 `workers/` 当一个子目录扫但里面没 span code,纯浪费;应改成扫 `qa/` 或 drop `workers/`
25. **Legacy `TenantResolver` class** (`apps/api/src/llm_client/tenant_resolver.py:511`) — M6.3 fix 后用 `TenantResolverSpan` 替代,但旧 class 仍 export(`src/llm_client/__init__.py:26`)。标了 deprecation 但还在跑,应移到 `__all__ = []` 或 M7 删
26. **`OtelAttrsException` Protocol 未被使用** (`apps/api/src/observability/exceptions.py:22`) — M6.2 定义了一个 `__otel_attrs__` 协议但没 exception class 实现它,纯 dead code。Plan 提的 follow-up 还没做
27. **`worker_context.py` 有未用 `trace` import** — 用 `_ = trace` workaround,cosmetic,直接删 import 即可
28. **No unit test for `http.request → llm.chat` parent-child** — E2E + Round 12 + 中间件单独测都覆盖,但 unit-level `InMemorySpanExporter` 配 `client.py:chat` 验证父子关系的 fast test 缺失。Follow-up test in `tests/llm_client/test_llm_gateway_creates_span.py`
29. **`provider_name` (underscored) vs `channel.name` / `worker.job_name` / `conversation.id` (dotted) 一致性** — 同概念("entity name")用了两种分隔符;有意为之(单概念 IDs 用 flat,namespace 字段用 dotted),但 reader 要知道。后续可全统一到 dotted form

### Tech debt follow-ups (actionable, scheduled)

- [ ] **M4.A #24** — `_default_llm_client_factory` per-call `httpx.AsyncClient` 构造 (Task 7 of this plan: 改 per-`(base_url, headers)` 缓存)
- [x] **M4.C #4** — per-tenant fallback chain (chain 升级为 tenant-scoped) — DONE (M5.1, commit `6ca2e1b`/`3941a02`/`6612b05`)
- [x] **M4.C #6** — per-tenant model override — DONE (M5.2; column + resolver wrapper + admin schemas + e2e + explore Round 10)
- [x] **M5.3 known #16** — no audit log for `tenant_llm_configs` mutations — DONE (M5.3; `tenant_llm_config_audit_log` table + `GET audit` endpoint + atomicity-guaranteed wrapper; 41 new tests + 5 commit)
- [ ] **M4.D known #6** — Non-LLM cost budget (SES / SMS / KB / embedding 计入 cap)
- [x] **M4.D known #3** (closed in Pack A) — snapshot inconsistency window — DONE
- [x] **M4.D known #8** (closed in Pack B) — provider 429 integration — DONE
- [ ] **Nitpick S1** — JWT secret validation hardening (Task 1 of this plan)
- [ ] **Nitpick S2** — rate-limit fail-open observability (Task 3 of this plan)
- [ ] **Nitpick S3** — X-Forwarded-For trust gate (Task 2 of this plan)
- [ ] **Nitpick S4** — httpx connection pool caching (Task 7 of this plan)
- [ ] **Nitpick S5** — budget pre-check short-circuit (Task 8 of this plan)
- [x] **Explore #1** — widget `WebWidgetAdapter.parse_inbound` 没用 JWT `sub` 作为 `external_user_id`,所有访客共享同一空字符串会话(Round 3 DB 验证:5 个 widget 用户 → 1 个 conversation / 10 messages);隐私 + 数据污染;`apps/api/src/widget/adapter.py:39` 修法 = signature 加 `external_user_id` 参数,`apps/api/src/widget/ws/router.py:175` 调 adapter 时把 JWT sub 传过去 — FIXED (commit `9b450be`)
- [x] **Explore #2** — KB 种子后没跑 chunking/embedding pipeline → `articles` 有行但 `chunks` 永远空,RAG `search_internal_kb` 返回 0 citations(Round 5 实测);`extend-seed.py` 末尾 await `index_article()`(inline,不是 fire-and-forget — 让 seed 退出码反映 KB 状态);KB 的 `embedding_model` 现在从 `get_settings().default_embedding_model` 取(部署感知:DOUBAO-only 容器不再撞 EmbeddingError) — FIXED
- [x] **Explore #3** — `LLMClient.stream_chat` 走 sync `__call__` 路径时绕过 budget 跟踪:`_pre_check_sync` 跳过(ac276d async guard)后无对应 `_post_record`(Round 4 实测:hard_cap=200 / soft_warn=160 → 2 次 PONG 都成功但 `llm_usage` + `tenant_budget_snapshots` 0 行);修复 = `BudgetResolver.ainvoke_stream` 异步流式 seam(完整 pre-check + 末尾 `_post_record`,失败不打断流);`LLMClient.stream_chat` 通过 `hasattr(provider_resolver, "ainvoke_stream")` 检测并走新 seam;Round 4 实测:turn 1 跑通(1029 tokens → snapshot,远超过软警告阈值)、turn 2 在 pre-check 处 `TenantBudgetExceeded`(已超过硬限);6 个回归测试覆盖 pre-check / post-record / 异常旁路 — FIXED (`apps/api/src/budget/resolver.py:170-232`, `apps/api/src/llm_client/client.py:379-428`,4 new tests)

### M4.C — Tenant Resolver (BYOK)

Per-tenant provider API keys (BYOK), encrypted at rest with Fernet,
cached with in-process LRU + TTL. See
`docs/superpowers/specs/2026-10-05-m4-c-tenant-resolver-design`.

| Component | Status |
|---|---|
| `TenantLLMConfigCipher` (Fernet) | shipped |
| `tenant_llm_configs` table + repo | shipped |
| `TenantResolver` (LRU + TTL cache) | shipped |
| `_default_llm_client_factory` async + strict mode | shipped |
| `POST /admin/tenants/{id}/llm-configs` + `GET` | shipped |
| `LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL` metric | shipped |

### M4.D — Budget Layer

Per-tenant monthly token hard-cap enforcement. `BudgetResolver` wraps the
M4.C `TenantResolver` to reject calls that would exceed the configured
monthly cap. See `docs/superpowers/specs/2026-10-05-m4-d-budget-layer-design`.

| Component | Status |
|---|---|
| `tenant_budgets` + `tenant_budget_snapshots` tables | shipped |
| `TenantBudgetRepository` + `TenantBudgetSnapshotRepository` | shipped |
| `TenantBudgetSnapshotCache` (LRU + TTL) | shipped (Pack A: dead code in default config — soft escape hatch only) |
| `BudgetResolver` (pre-check + post-record) | shipped |
| `TenantBudgetExceeded` exception | shipped |
| Factory wiring (opt-in per tenant) | shipped |
| `POST /admin/tenants/{id}/budget` + GET + usage endpoints | shipped |
| `LLM_TENANT_BUDGET_EXCEEDED_TOTAL` + `SOFT_WARN_TOTAL` metrics | shipped |
| **Pack A** — `tenant_budget_snapshots.soft_warn_fired_at` (sticky soft-warn) | shipped |
| **Pack A** — DB-direct pre-check (`tenant_budget_pre_check_use_db`, default True) | shipped |
| **Pack A** — `pg_advisory_xact_lock` in `refresh()` | shipped |
| **Pack A** — `run_budget_cleanup()` (13-month retention, LIMIT 10000) | shipped |
| **Pack A** — arq cron `budget_cleanup_task` at 02:00 UTC daily | shipped |
| **Pack A** — `POST /admin/budget/cleanup` super-admin manual trigger | shipped |
| **Pack B** — `tenant_budget_credits` table (immutable audit log) | shipped |
| **Pack B** — `CreditService` (grant + sum_for_period, UTC-month-bound) | shipped |
| **Pack B** — `effective_cap = hard_cap_tokens + sum(credits for period)` | shipped |
| **Pack B** — `POST/GET /admin/tenants/{tid}/credits` super-admin endpoints | shipped |
| **Pack B** — `PerModelBreakdownCache` (in-process 30s TTL) + `PerModelService` | shipped |
| **Pack B** — `?breakdown=true` query on snapshot endpoint | shipped |
| **Pack B** — `TenantBudgetRateLimited` exception + post-mortem gate | shipped |
| **Pack B** — `LLM_BUDGET_GATE_TOTAL` metric + FastAPI handler → HTTP 429 | shipped |

### M5.1 — Per-tenant LLM Fallback Chain

Closes **M4.C tech-debt #4**. Each tenant can override the global
`LLM_FALLBACK_CHAIN` env chain with its own ordered `[{provider, model,
enabled}]` list. Resolver precedence on each call:

1. Per-tenant row from `tenant_fallback_chains` (cache → DB on miss)
2. Env chain (`LLM_FALLBACK_CHAIN`)
3. Strict mode (`LLM_TENANT_FALLBACK_CHAIN_REQUIRED=true`) → `TenantFallbackChainNotConfigured`

| Component | Status |
|---|---|
| `tenant_fallback_chains` table (JSONB steps + GIN index) | shipped |
| `TenantFallbackChainRepository` (atomic upsert via `INSERT ... ON CONFLICT`) | shipped |
| `TenantFallbackChainCache` (LRU + TTL singleton in `_build_fallback_chain_cache()`) | shipped |
| Resolver wiring (`TenantLLMConfigCache._resolve_chain_steps` + `build_tenant_resolver`) | shipped |
| `POST/GET/DELETE /api/v1/admin/tenants/{id}/fallback-chain` (cache invalidation on write) | shipped |
| Strict-mode `TenantFallbackChainNotConfigured` exception | shipped |
| `lumen_llm_tenant_fallback_chain_total{outcome}` zero-label counter (`hit`/`miss`/`disabled`/`unconfigured`) | shipped |
| Settings: `LLM_TENANT_FALLBACK_CHAIN_REQUIRED` / `_TTL_S` / `_CACHE_MAXSIZE` | shipped |

**Container default:** `LLM_TENANT_FALLBACK_CHAIN_REQUIRED=false` —
tenants without an explicit chain row keep falling back to the env
chain (back-compat). Flip to `true` only when every tenant is known to
have its own chain configured. See `apps/api/.env.example` for the
exact variables.

## 仓库信息

- 远程: `git@github.com:leiak/Lumen-AI-Support-Agent.git` (origin/main)
- 分支约定: `main` 是唯一长期分支,M1 阶段所有 commit 直接 linear 到 main
- 提交约定: `<type>(scope): subject` — 例 `feat(7.1)`, `fix(conversation)`, `test(knowledge)`

## 许可

(待定 — 内部项目)
