# M2.A — AI 核心:Agent 工具扩展 + Ticket 领域 + 实时质检

> **设计阶段**:M2 milestone(业务扩展版)的 A 阶段
> **父设计**: [2026-09-10-ai-customer-service-design.md](./2026-09-10-ai-customer-service-design.md) §13 (M2 = "Agent 工具调用 + 工单 + 知识库多模态 + 邮件渠道 + 历史会话挖掘")
> **目标**:让 AI 坐席从"只查 + 转人工"升级为"能主动调业务工具 + 创建/管理工单 + 实时被质检",把 M1 demo 升级到真实产品能力闭环
> **状态**:Brainstorm 完成,待用户评审 + writing-plans

---

## 1. 范围与非目标

### 1.1 做

| ID | 子模块 | 描述 | 估计 |
|----|-------|------|------|
| A1 | **Agent 工具扩展** | 第一个新工具 `search_internal_kb`(LangChain `RetrieverTool` 风格);llm_node 升级为 tool 循环(全 loop,直到 LLM 不再调 tool) | 1-2 周 |
| A2 | **Ticket 领域** | 独立 3 表 `tickets` / `ticket_events` / `sla_policies`,与 `conversations` 1:1;状态机 7 个状态;SLA 策略 | 2-3 周 |
| A3 | **实时质检** | 每条 AI 消息发出后入 Arq 队列,小 Judge LLM 评 3 维度(relevance / safety / faithfulness)→ `message_qa_scores` 表 + `lumen_qa_*` 指标;**不阻塞主路径** | 2-3 周 |
| A4 | **Demo-act4** | 3 张新截图(工具调用 / Ticket 详情 / QA 评分 chip) | 0.5 周 |
| A5 | **README + 状态表** | Stage 12 / 13 / 14 三行 ✅,技术债追加 3-4 条 | 0.5 周 |

**总估**:6-9 周,3 个实现 Stage(Stage 12 → 13 → 14)+ 1 个 README/recap Stage(Stage 15)串行。

### 1.2 不做(显式 skip)

| 计划项 | 推到 | 理由 |
|--------|------|------|
| 邮件渠道(SMTP/IMAP) | M2.B | M2.B 集中做渠道扩展 |
| 知识库多模态(vision) | M2.B | 与邮件一起做"内容采集与辅助"主题 |
| 历史会话挖掘(批处理 Q&A 提取) | M2.B | 同上 |
| LLM Gateway(配额/缓存/降级/Tracing) | M3 | M1 design line 296 划归 M3 |
| SSO / 高级权限 / 合规 | M5 | M1 design line 493 |
| 其它 IM 渠道(钉钉/企微/Slack/Teams) | M4 | M1 design line 492 |
| Agent 第二个工具(查订单 / 查客户 / 改工单) | M2.B / M3 | M2.A 验证工具集机制后再扩 |
| 工单 SLA 自动告警 → PagerDuty | M3 | M2.A 只入 metric,M3 接外部告警 |

### 1.3 M2.B 占位(本文档不展开)

```
M2.B 内容(预计 4-6 周):
  B1 邮件渠道 — SMTP/IMAP adapter,ChannelType.EMAIL 枚举已存在
  B2 知识库多模态 — Stage 6 _log_ocr_todo_once,接 vision model
  B3 历史会话挖掘 Arq pipeline — M1 design 6.4
  B4 Agent 工具集第二轮 — 查订单 / 查客户 / 改工单
```

---

## 2. 架构总览

```
                  ┌────────────────────────────────────────────────────────────┐
                  │   M1 已存在(改动最小)                                        │
                  │   Conversation / Message / Channel Gateway / RAGService /   │
                  │   LLMClient (Anthropic 主, MiniMax SSE 流式) / Agent Runtime │
                  │   (LangGraph StateGraph) / 坐席工作台 API / Widget SDK      │
                  └─────────────────┬──────────────────────────────────────────┘
                                    │
                ┌───────────────────┼───────────────────┐
                ▼                   ▼                   ▼
      ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
      │  A1 Tool 层      │  │  A2 Ticket 领域  │  │  A3 QA Worker   │
      │                 │  │                 │  │                 │
      │ search_internal │  │ Ticket +        │  │ qa_judge_worker │
      │  _kb RetrieverTool│ │ TicketEvent +  │  │ (Arq background) │
      │ ↑ 挂在 AgentStateGraph │ SlaPolicy  │  │                 │
      │   llm_node 后,  │  │ Conversation   │  │ lumen_qa_*      │
      │   tool 循环     │  │  .ticket_id FK │  │ Prometheus      │
      │                 │  │                 │  │                 │
      │ ↓ 同 escalate_to│  │ ↓ State machine│  │ ↓ message_qa_   │
      │   _human 用     │  │   audit trail   │  │   scores 表     │
      │   ContextVar    │  │                 │  │                 │
      │   绑 tenant     │  │                 │  │                 │
      └─────────────────┘  └─────────────────┘  └─────────────────┘
                │                   │                   │
                └───────────────────┼───────────────────┘
                                    ▼
                          ┌──────────────────┐
                          │  Postgres(同库) │
                          │  Redis(同实例)  │
                          │  Qdrant(同实例) │
                          └──────────────────┘
```

### 2.1 关键边界

- **A1** 不新建包,扩 `apps/api/src/agent/graph/tools.py`(现有 1 个 tool → 变 2 个)
- **A2** 新建 `apps/api/src/ticket/` 包(`model` / `repository` / `service` / `api` / `schemas` / `state_machine`)
- **A3** 新建 `apps/api/src/qa/` 包(`judge` / `worker` / `schemas` / `metrics`)+ 在 `conversation/service.py:record_message` 触发 enqueue
- 共享 `core/` 包:复用 `request_context`(11.2)/ `business_metrics`(11.3)/ `health`(11.4)/ `logging`
- **不引入新服务**(不用 Postgres 之外的 DB / 不用 RabbitMQ)— 沿用 M1 全栈

### 2.2 已有 M1 模块的最小改动

| 文件 | 改动 | 来源 |
|------|------|------|
| `agent/graph/nodes.py:llm_node` | `tool_calls[:1]` first-wins → 全 loop,直到 LLM 不再调 tool | M1 技术债 #6 |
| `conversation/models.py` | 加 `ticket_id: Mapped[str \| None]` 字段 + 索引 | A2 |
| `channel/inbound.py:_on_ai_message_persisted` | 钩子 enqueue `qa_judge_task` | A3 |
| `core/config.py` | 加 `qa_judge_model`、`qa_judge_provider`、`qa_score_threshold_alert`(默认 0.3,任一维度 < 0.3 即 flagged) | A3 |
| `deploy/docker-compose.yml` | 加 judge provider env vars | A3 |

---

## 3. 数据模型 + 状态机

### 3.1 Ticket 表(A2)

```sql
-- alembic migration: 12_ticket_tables.py
CREATE TYPE ticket_priority AS ENUM ('P0', 'P1', 'P2', 'P3');
CREATE TYPE ticket_status AS ENUM (
    'new', 'triaged', 'in_progress', 'waiting_customer',
    'resolved', 'closed', 'cancelled'
);

CREATE TABLE tickets (
    id              TEXT PRIMARY KEY,              -- ULID
    tenant_id       TEXT NOT NULL,                 -- multi-tenant
    conversation_id TEXT NOT NULL UNIQUE,          -- 1:1 with conversations
    subject         TEXT NOT NULL,                 -- 简短标题
    category        TEXT,                          -- 主分类 (e.g. 'billing')
    priority        ticket_priority NOT NULL DEFAULT 'P2',
    status          ticket_status  NOT NULL DEFAULT 'new',
    assignee_agent_id TEXT REFERENCES users(id),
    sla_policy_id   TEXT REFERENCES sla_policies(id),
    sla_deadline_at TIMESTAMPTZ,
    first_response_at TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX idx_tickets_tenant_status ON tickets(tenant_id, status);
CREATE INDEX idx_tickets_assignee ON tickets(tenant_id, assignee_agent_id)
    WHERE assignee_agent_id IS NOT NULL;
CREATE INDEX idx_tickets_sla ON tickets(sla_deadline_at)
    WHERE status NOT IN ('resolved', 'closed', 'cancelled');

CREATE TABLE ticket_events (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    ticket_id   TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    actor_type  TEXT NOT NULL,         -- 'system' / 'agent' / 'ai' / 'customer'
    actor_id    TEXT,
    event_type  TEXT NOT NULL,         -- 'created' / 'status_changed' / 'assigned' / 'priority_changed' / 'comment' / 'sla_breached'
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_ticket_events_ticket ON ticket_events(tenant_id, ticket_id, created_at DESC);

CREATE TABLE sla_policies (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    name            TEXT NOT NULL,
    priority        ticket_priority NOT NULL,
    first_response_minutes INT NOT NULL,
    resolution_minutes     INT NOT NULL,
    business_hours_only    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);
```

### 3.2 Ticket 状态机

```
NEW (创建, AI/客户触发)
  ├─→ TRIAGED    (运营/AI 标 priority/category)
  │     ├─→ IN_PROGRESS   (坐席认领)
  │     │     ├─→ WAITING_CUSTOMER  (等待客户回复)
  │     │     │     └─→ IN_PROGRESS  (客户回复了)
  │     │     └─→ RESOLVED
  │     │           └─→ CLOSED
  │     └─→ CANCELLED
  ├─→ CANCELLED
```

合法转移由 `TicketService.transition(ticket, to_status)` 集中校验。非法转移 → `InvalidTransition` 异常(400)。

### 3.3 message_qa_scores 表(A3)

```sql
CREATE TABLE message_qa_scores (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    message_id      TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    judge_model     TEXT NOT NULL,
    relevance_score     NUMERIC(3,2) NOT NULL,
    safety_score        NUMERIC(3,2) NOT NULL,
    faithfulness_score  NUMERIC(3,2) NOT NULL,
    overall_score       NUMERIC(3,2) NOT NULL,
    rationale       TEXT,
    flagged         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (message_id)
);
CREATE INDEX idx_qa_scores_flagged ON message_qa_scores(tenant_id, flagged)
    WHERE flagged = TRUE;
```

`message_qa_scores.message_id` 唯一约束保证幂等(同一消息不重复评分)。

---

## 4. 关键流程

### 4.1 search_internal_kb 工具调用(A1)

```
┌─────────────────────────────────────────────────────────────────┐
│  LangGraph StateGraph(改 M1 llm_node + 加 tool 循环)              │
│                                                                 │
│  START → retrieve_node (M1 不变,自动跑 RAG)                      │
│           │                                                     │
│           ▼                                                     │
│        llm_node_v2:                                             │
│          response = await llm.chat(messages + tools=[           │
│              escalate_to_human,    (M1 已有)                     │
│              search_internal_kb    (新)                          │
│          ])                                                     │
│          while response.tool_calls:                             │
│              for tc in response.tool_calls:                     │
│                  result = await dispatch(tc)                    │
│                  messages.append(tool_message(tc.id, result))   │
│              response = await llm.chat(messages)                │
│          return response                                        │
└─────────────────────────────────────────────────────────────────┘
```

```python
@tool("search_internal_kb", args_schema=SearchInternalKbArgs)
async def search_internal_kb(
    query: str,
    kb_slug: str | None = None,
    top_k: int = 5,
) -> str:
    """查询租户私有知识库。kb_slug 为空搜全租户所有 KB。"""
    ctx = _current_escalation_ids()      # 复用 M1 ContextVar
    kb = await kb_repository.find_by_slug(ctx.tenant_id, kb_slug) if kb_slug else None
    results = await rag_service.retrieve(
        tenant_id=ctx.tenant_id,
        conversation_id=ctx.conversation_id,
        query=query.strip()[:500],        # 500 字符 = M1 RAGService 上限(对齐)
        knowledge_base_id=kb.id if kb else None,
        top_k=min(top_k, 20),
    )
    return _format_kb_results(results)
```

**关键点**:
- 与 `escalate_to_human` 共享同一 `_current_escalation_ids` ContextVar
- 与 M1 `retrieve_node` 互补:retrieve_node 自动跑固定一次,这个 tool 让 LLM 主动决定"再查一次或换一个角度"
- LangChain `@tool` 自动生成 JSON Schema → LLM 知道何时/怎么调
- `query[:500]` 截断与 M1 `RAGService.retrieve` 已有的输入上限对齐,避免下游 Qdrant 一次性打爆 batch

### 4.2 Ticket 创建(A2)

**触发源(3 个)**:
- a) AI 主动 escalate_to_human(M1 已有)→ 自动创建 Ticket
- b) 客户首次进入对话(Stage 5 `find_or_create_for_inbound`)→ 自动创建 Ticket
- c) 坐席手动 "创建工单" 按钮 → 显式 API

**事件流**:
```
TicketService.create(conversation_id, subject, ...):
  INSERT tickets (status=NEW, priority=P2 default) +
  INSERT ticket_events (event_type='created', actor_type=...)

  apply default SLA policy(tenant settings):
    - 查 sla_policies WHERE priority = ticket.priority
    - 写 sla_deadline_at = now() + first_response_minutes

  enqueue ticket_created event → outbox(M3 接邮件通知)
```

**状态转移**:
```
TicketService.transition(ticket_id, to_status, actor):
  - SELECT FOR UPDATE ticket
  - validate transition against state machine map
  - UPDATE tickets SET status = ?, updated_at = now()
    [, resolved_at/closed_at if terminal]
  - INSERT ticket_events (event_type='status_changed', payload={from, to})
  - WS broadcast('ticket.updated')
```

### 4.3 实时质检(A3)

```
触发源:
  channel/inbound.py:_on_ai_message_persisted(message):
    await arq_pool.enqueue('qa_judge_task', message_id=message.id)

Worker(qa_judge_worker.py):
  async def qa_judge_task(ctx, message_id):
    msg = await message_repo.get_by_id(message_id)
    if msg.role != MessageRole.AI: return
    if await qa_scores_repo.exists_for_message(message_id): return  # 幂等

    judge_input = JudgeInput(
        question=msg.conversation.last_customer_message.content,
        answer=msg.content,
        citations=msg.citations or [],
    )
    scores = await judge_client.score(judge_input)
    # judge_client = LLMClient(provider=judge_provider, model=settings.qa_judge_model)

    await qa_scores_repo.insert(MessageQaScore(...))
    LUMEN_QA_SCORES.labels(dimension='overall', bucket=_bucket(scores.overall)).inc()
    if scores.flagged:
        LUMEN_QA_FLAGGED.inc()
        # M3 接 PagerDuty,M2.A 仅 metric

Judge prompt:
  system: 你是 AI 客服回复质检员。给定客户问题 + AI 答案 + 引用文章,
          评 3 个维度 0-1:relevance / safety / faithfulness
          输出 JSON: {relevance, safety, faithfulness, rationale}
```

**Judge 模型选型**: 独立小 Judge LLM(如 `minimax-m2.7-highspeed`,1-3B 参数)。质量刚及格,成本极低(约主 LLM 1/50)。适合 metrics / 敏感词 / 引用一致性三类任务。

**成本估算**: 假设每日 10k AI 消息,小 Judge $0.0001/1k tokens × 500 tokens = **$0.50/天**。

---

## 5. 错误处理 + 隔离 + 安全

### 5.1 错误处理

| 场景 | 策略 | 用户可见 |
|------|------|---------|
| `search_internal_kb` Qdrant 失败 | `QdrantUnavailable`,Agent 当作"无结果"继续 | 客服回答"暂无相关文章" |
| `search_internal_kb` query 注入 | `query.strip()[:500]` + 走 RAG 现有 sanitizer | 静默截断 |
| Ticket 非法状态转移 | `InvalidTransition` 异常(400) | 前端 toast |
| 工单超 SLA 未响应 | `qa_sla_alert_worker` 每分钟扫,`lumen_qa_sla_breached_total` inc | dashboard 红条(M3 接 PagerDuty) |
| Judge LLM 失败 | 重试 1 次仍失败 → `qa_judge_failures_total` inc + 不写 score | metric 暴露 |
| Judge 超时(>10s) | Arq 重试 1 次后丢弃 | metric |
| DB migration 失败 | Alembic 失败 → uvicorn 启动失败,k8s readiness 不就绪(沿用 11.4) | 部署 pause |

### 5.2 租户隔离

- Ticket 域所有查询强制 `WHERE tenant_id = :tenant_id`
- `search_internal_kb` 内部强制 `knowledge_base_id` 限定在当前 tenant(RAGService 已做)
- `message_qa_scores` 查询只允许同租户管理员拉 — 与 message 同 tenant scope

### 5.3 PII 纪律(继承 M1)

- Ticket subject / category / events payload:日志只记 ID + 类型,不记 subject
- Judge rationale:持久化到 DB(受 access control),**不写 structlog 日志**
- 工具返回字符串:`search_internal_kb` 返回的 Markdown 不进日志(M1 PII 规范)

### 5.4 Judge 提示词注入

- Judge system prompt **只读**,从 settings/code 注入,不让 message content 拼到 system prompt
- 客户问题用 `<user_question>{...}</user_question>` XML 标签包,XML escape 防注入

### 5.5 Judge 输出一致性

- `with_structured_output(JsonSchema)` 强制 JSON Schema,失败重试 1 次(主 LLM 重试 3 次;Judge 失败代价小,让 metric 暴露 — M1 design §8.1 "Outbox + 重放"策略的轻量版本)
- **超时 10s**:Judge 模型是 1-3B 参数小模型,正常 <2s;10s 已是异常。Arq task 默认 timeout 是 60s,我们专门覆盖到 10s,失败立即 metric 暴露 + 不阻塞 worker 队列
- 分数钳位 `[0,1]`,超界视为 Judge 失败,丢弃

---

## 6. 测试策略 + 验收

### 6.1 测试金字塔

| 层级 | 类型 | M2.A 覆盖 | 数量目标 |
|------|------|-----------|---------|
| Unit | 纯函数 / mock LLM | Ticket state machine / Judge 解析 / tool 签名 | 25+ |
| Integration | 真实 Postgres + Redis + Qdrant | Ticket API CRUD + QA worker E2E + tool 走真实 RAG | 15+ |
| E2E (Playwright) | 全栈 demo 脚本 | demo-act4 — 3 个新截图 | 3 |
| LLM Eval | 离线 harness | Judge 准确率 + tool 调用合理性 + Ticket 自动化 | 见 A1/Eval |

### 6.2 关键测试 case

**A1 search_internal_kb**:
- `test_search_internal_kb_returns_top_k_chunks`: mock RAGService.retrieve → 验证 Markdown 输出含 article_title + chunk_id + score
- `test_search_internal_kb_clamps_top_k_to_20`: top_k=100 → 实际调 retrieve(top_k=20)
- `test_search_internal_kb_filters_by_kb_slug`: mock kb_repository → 验证 knowledge_base_id 传入 retrieve
- `test_search_internal_kb_requires_tenant_context`: 没绑 ctx → 抛 ContextError
- `test_tool_loop_handles_multiple_tool_calls`: mock LLM 返回 2 个 tool_calls → 验证两次 dispatch + 第二次 LLM.chat
- `test_tool_loop_stops_when_no_tool_calls`: 0 tool_calls → 验证不再 chat
- `test_tool_loop_handles_tool_error`: mock dispatch 抛异常 → 写 tool_message 含 error,LLM 收到并继续

**A2 Ticket**:
- `test_ticket_create_default_priority_p2`: 创建 → priority=P2 status=NEW
- `test_ticket_create_applies_sla_policy`: priority=P0 → sla_deadline = now + 15min
- `test_ticket_transition_new_to_triaged`: 合法 → ticket_events 写 status_changed
- `test_ticket_transition_invalid_raises`: NEW → CLOSED 跳级 → InvalidTransition
- `test_ticket_conversation_one_to_one`: 同 conv 创建 2 次 → UNIQUE 撞车
- `test_ticket_api_requires_admin_or_agent`: 普通 user → 403
- `test_ticket_events_query_ordered_desc`: 拉 events → created_at DESC

**A3 QA**:
- `test_qa_worker_scores_ai_message`: enqueue → worker → message_qa_scores 落
- `test_qa_worker_skips_non_ai_message`: human message → 不评分
- `test_qa_worker_idempotent`: 重复 enqueue 同一 message → UNIQUE 防重,只一行
- `test_qa_worker_increments_metrics`: 评分完 → lumen_qa_scores_total +1,flagged=true 时 +flagged
- `test_qa_judge_llm_handles_malformed_output`: judge 返回非 JSON → 重试 1 次再失败 → qa_judge_failures_total +1
- `test_qa_worker_uses_isolated_judge_client`: 验证 LLMClient model = settings.qa_judge_model
- `test_qa_api_endpoint_returns_scores_for_message`: 拉评分 → 200 + JSON

### 6.3 Demo-act4 截图

| 文件 | 内容 |
|------|------|
| `demo-act4-01-tool-call.png` | 客户问"怎么换绑手机号?" → AI 回复显示"我查了 KB: account-management" + 工具调用日志 |
| `demo-act4-02-ticket-detail.png` | 工单详情页:subject / priority / P2 / status / NEW / assignee / SLA 时间 |
| `demo-act4-03-qa-dashboard.png` | 质检面板或 message 上的 QA 评分 chip |

### 6.4 验收判据

M2.A 完成时:
- [ ] 4 个 Phase 都 commit + push 到 main
- [ ] 单元 + 集成测试全过(目标 547 → 600+ passed)
- [ ] `pytest --collect-only` 0 errors
- [ ] `lumen_qa_*` 指标在 `/metrics` 可见
- [ ] demo-act4 三张截图存在 `images/`
- [ ] README 状态表新增 Stage 12 / 13 / 14 / 15 四行 ✅
- [ ] Judge LLM 测试用 mock,生产用 `minimax-m2.7-highspeed`
- [ ] 1 个真实工单端到端跑通:客户发问 → AI 回复 + tool 调用 + Ticket 自动创建 → QA 评分异步落
- [ ] M2.B 仍占位,设计上不阻塞

---

## 7. 实施计划占位

**Stage 12 — search_internal_kb 工具 + llm_node tool loop**(A1)
- 新工具 + 改 llm_node
- 7 个单测 + 2 个集成测试
- demo-act4-01 截图

**Stage 13 — Ticket 领域**(A2)
- alembic migration 12_ticket_tables
- `apps/api/src/ticket/` 全包
- 7 个单测 + 5 个集成测试
- 2 个 admin/agent API 端点
- demo-act4-02 截图

**Stage 14 — 实时质检**(A3)
- `apps/api/src/qa/` 包 + Arq worker 注册
- `channel/inbound.py` 钩子
- `lumen_qa_*` 指标 + Judge 配置
- 7 个单测 + 3 个集成测试
- demo-act4-03 截图
- README + 状态表更新(A5)

**总估**:6-9 周,每 Stage 1-3 周。

---

## 8. 风险与开放问题

### 8.1 风险

| 风险 | 严重度 | 缓解 |
|------|-------|------|
| Tool 循环引发 LLM 无限循环 | 中 | 加 max_iterations=5 + `tool_calls` 计数器,超限走 FALLBACK_MESSAGE |
| Judge 模型质量差导致 false flag | 中 | 阈值默认 0.3,首周人工 spot-check 50 条,calibration 后再调 |
| Ticket 创建与 Conversation 创建双写不一致 | 中 | ConversationService.create 内嵌 TicketService.create,同事务 |
| 工具调用增加 LLM 延迟(2x~3x chat) | 低 | 可接受;tool 循环通常 1-2 次 |
| Qdrant 故障让 search_internal_kb 全失败 | 低 | RAGService 已有兜底,Agent 继续 |

### 8.2 开放问题

| 问题 | 决策点 |
|------|--------|
| Ticket 工单是否需要 SLA 倒计时 WebSocket 推送给坐席? | M3 dashboard 决定 |
| 质检 Judge 模型要不要在租户 settings 覆盖? | M2.A 不做,M3 接 LLM Gateway 时统一 |
| Ticket 事件要不要 outbox 到外部系统(Email/Slack)? | M3 接 LLM Gateway 时统一 |
| 工具调用结果是否需要流式回传? | 不需要(M2.A);LLM 全部生成完再回,流式只在主回复 |

---

## 9. 与 M1 既有决策的一致性

- **PII 纪律**:继承 M1,不引入新规则
- **租户隔离三层**:继承 M1(DB WHERE / Qdrant MUST filter / 跨租户 404)
- **Structlog request_id**:继承 11.2,所有新代码用 `get_logger(__name__)` + bound contextvars
- **业务 metric 无 tenant_id label**:继承 11.3,`lumen_qa_*` 同此原则
- **Health / liveness / readiness**:继承 11.4,新增 `lumen_qa_*` 暴露在 `/metrics`,**不暴露在 /health/ready**(QA worker 挂了不算"服务不可用",只是质量下降)
- **容器化 + CI**:沿用 Stage 11.7,新 worker 镜像随 API 同一 Dockerfile
- **Demo-act4 与原 3 幕并列**:不修改原脚本(M1 demo 基线不动),新增独立 Act 4

---

## 10. 文档元数据

- **设计阶段**:M2 milestone → A 阶段(Agent 工具 + Ticket + QA)
- **后续设计**:M2.B(邮件/多模态/历史挖掘)、M3(LLM Gateway)、M4(更多 IM)、M5(企业能力)
- **关联文档**:
  - [M1 design §13 里程碑](../specs/2026-09-10-ai-customer-service-design.md)
  - [[stage-11-progress]] — M1 close-out,本设计的起点
  - [[langchain-decision]] — 工具集为何用 LangChain `@tool` 装饰器
- **状态**:Brainstorm 完成 → 用户评审 → writing-plans → Stage 12-14 实现
