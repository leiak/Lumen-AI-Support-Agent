# 智能客服系统设计文档

**日期**: 2026-09-10
**项目代号**: ai-customer
**状态**: 设计稿,待用户审阅
**目标读者**: 技术评审、研发、运维

---

## 1. 背景与目标

为 SaaS/软件企业打造一套 AI 深度介入的智能客服系统,覆盖产品使用问题、技术支持、文档问答等场景。系统既要服务外部客户(通过 Web Widget、邮件、企业 IM),也要为内部坐席提供统一工作台。

**核心目标**:

- 降低人工坐席压力:高频问题 AI 自助解决,目标 60%+ 自助率
- 提升坐席效率:AI 实时辅助、统一收件箱、减少跨工具切换
- 沉淀知识资产:RAG 知识库 + 历史会话挖掘,越用越聪明
- 企业级能力:多租户隔离、合规审计、SLA 可控

---

## 2. 范围与约束

### 2.1 业务范围(本设计覆盖)

- 多渠道接入:Web Widget、邮件、企业 IM(Slack/Teams/飞书/钉钉/企业微信)
- AI 能力:知识库问答(RAG)、工单自动创建/分类/路由、智能体工具调用、坐席实时辅助
- 知识库:手动上传/编辑、历史会话挖掘、多模态内容(PDF/图片/代码块)
- 工单:自建模块,深度集成会话、AI、SLA
- 坐席工作台:统一会话收件箱、AI 辅助、高级搜索、监控/报表/质检
- 多租户:B2B SaaS 数据隔离

### 2.2 MVP 范围(M1,首个交付版本)

- 接入渠道:**Web Widget + 飞书(Lark)**(其他 IM、邮件在后续迭代)
- AI 能力:基础 RAG + 简单工单创建 + 人工转接
- 工作台:会话收件箱(不分渠道合并视图)、会话详情、AI 推荐回复
- 多租户:基础数据隔离 + 简单管理员配置
- 不含:历史会话挖掘(放 M2)、质检(放 M3)、SSO(放 M5)

### 2.3 规模假设

- 坐席团队 10-50 人
- 日活会话 1,000-10,000
- 单租户峰值 100 并发会话
- 知识库规模 10k-100k 文档,每文档 1k-50k token

### 2.4 非目标

- 不做电商/物流/金融等垂直场景
- 不做主动外呼营销
- 不替代专业工单系统的高级字段(自定义工作流引擎等)

---

## 3. 架构选型

### 3.1 总体架构:**模块化单体 + 异步 Worker**

理由:

- 规模"中等"+"10-50 人团队"匹配单体甜区
- Python + FastAPI + LangChain/LlamaIndex 生态对单体更友好
- AI 推理本身是异步重型任务,天然适合 Worker 拆分
- 模块边界画清后,后期可平滑拆微服务

### 3.2 技术栈

| 层 | 选型 | 理由 |
|----|------|------|
| Web 框架 | FastAPI | 异步、类型友好、生态成熟 |
| AI 编排 | LangChain + LlamaIndex | LangChain 偏 Agent/工具调用,LlamaIndex 偏 RAG |
| 数据库 | PostgreSQL 16 | 成熟、RLS、JSONB 灵活 |
| 缓存/队列 | Redis 7 | 缓存 + 队列 + Pub-Sub 一体 |
| 向量库 | Qdrant(主)、Milvus(备) | Qdrant 部署简单,租户过滤原生支持 |
| 异步任务 | **Arq**(M1 选定,生态轻量、与 FastAPI/Redis 原生契合) | 简单可靠,后期可换 Celery |
| 依赖管理 | **uv**(M1 选定,基于 Rust,快) | 统一用 `pyproject.toml` |
| 对象存储 | S3 兼容(MinIO/OSS) | 知识库原始文件、附件 |
| LLM | 混合:自托管(vLLM/Qwen) + 商业 API(Anthropic Claude / OpenAI) | 成本/性能/合规平衡 |
| 前端 | React + TypeScript + Vite | 现代 SPA,适合工作台复杂度 |
| 实时通信 | WebSocket(FastAPI 原生) | 坐席台消息流 |
| 部署 | **Kubernetes**(M1 即采用) | 多副本、滚动升级、HPA |
| 监控 | OpenTelemetry + Prometheus + Grafana | 标准化三件套 |
| CI/CD | GitHub Actions | 主流,镜像不可变 |

### 3.3 部署拓扑

```
┌──────────────────────────────────────────────────────────────────┐
│                       客户端/外部渠道                              │
│  Web Widget  ·  飞书 Bot  ·  邮件(IMAP/SMTP)  ·  其他 IM         │
└──────────────────────┬───────────────────────────────────────────┘
                       │ WebSocket / Webhook / 邮件轮询
┌──────────────────────▼───────────────────────────────────────────┐
│                  Channel Gateway (FastAPI)                       │
│  · 渠道适配器:统一消息模型(MessageEnvelope)                        │
│  · 鉴权 / 限流 / 租户路由                                          │
└──────────────────────┬───────────────────────────────────────────┘
                       │ 领域事件 (Outbox)
┌──────────────────────▼───────────────────────────────────────────┐
│              AI Customer Core (FastAPI 单体)                       │
│                                                                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌─────────┐ │
│  │Conversation│ │ Ticket  │ │   KB    │ │ Agent   │ │Workspace│ │
│  │  会话编排 │ │  工单  │ │ 知识库  │ │ Runtime │ │ 坐席台  │ │
│  └────┬─────┘ └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ │
│       │            │            │           │            │       │
│       └────────────┴────────────┴───────────┴────────────┘       │
│                  领域事件总线 (Redis Stream / 内部 Pub-Sub)         │
└────────┬─────────────┬────────────────┬───────────────┬──────────┘
         │             │                │               │
   ┌─────▼─────┐ ┌─────▼─────┐  ┌──────▼──────┐  ┌─────▼──────┐
   │PostgreSQL │ │  Redis    │  │ Vector DB   │  │ Object     │
   │  主业务   │ │ 队列/缓存 │  │ Qdrant       │  │ Storage    │
   │           │ │           │  │ (按租户分片) │  │ S3/OSS/MinIO│
   └───────────┘ └───────────┘  └─────────────┘  └────────────┘
                       ▲
                       │ async tasks
              ┌────────┴───────────────────────┐
              │  Worker (Celery / Arq)          │
              │  · RAG 索引/重索引              │
              │  · 邮件拉取                     │
              │  · LLM 调用重试                 │
              │  · 历史会话挖掘                  │
              │  · 工单 SLA 监控                 │
              └────────┬───────────────────────┘
                       │
              ┌────────▼───────────────────────┐
              │  LLM Gateway                   │
              │  · 路由自托管 / 商业 API        │
              │  · 用量配额 / 限流 / 成本统计    │
              │  · 缓存 / 降级                  │
              └────────────────────────────────┘
```

---

## 4. 模块划分

| 模块 | 职责 | 主要外部接口 | 依赖 |
|------|------|-------------|------|
| **tenant** | 租户、成员、配额、计费 | REST | - |
| **channel** | 渠道适配、消息归一化 | Webhook/WS/IMAP | tenant |
| **conversation** | 会话生命周期、上下文、人机协作 | WS(给坐席)、事件 | channel, agent_runtime |
| **ticket** | 工单 CRUD、SLA、状态机、路由 | REST、事件 | conversation, tenant |
| **kb** | 知识库文章、版本、多模态解析 | REST(给运营) | tenant |
| **rag** | 检索(混合召回 + 重排) | 内部 API | kb, vector_db |
| **agent_runtime** | Agent 编排、工具调用、人机切换 | 内部 API | rag, llm_gateway, tool_registry |
| **tool_registry** | 工具声明、调用执行 | 内部 API | tenant |
| **workspace** | 坐席工作台 API(收件箱、搜索、报表) | REST/WS | conversation, ticket, kb |
| **analytics** | 指标采集、报表、质检 | REST | conversation, ticket |
| **llm_gateway** | LLM 路由、配额、降级、缓存 | 内部 API | - |
| **audit** | 审计日志 | 内部 API | 所有模块 |

**依赖规则**:只允许向下依赖(workspace → conversation → agent/kb/ticket),不允许反向。跨模块用事件 + 抽象接口,不用直接读对方表。

---

## 5. 数据模型

### 5.1 多租户隔离策略

**共享数据库 + 租户列 + 行级安全(RLS)三层防护**:

1. **应用层**:ORM 模型带 `tenant_id`,Repository 通过 SQLAlchemy `before_compile` 事件自动注入过滤
2. **数据库层**:PostgreSQL RLS policy 兜底,防止应用漏过滤
3. **缓存/向量层**:Redis key 强制带 `tenant_id` 前缀;向量库按 `tenant_id` payload 过滤

不采用"每租户一库"(运维成本高);也不采用纯共享无隔离(合规风险大)。

### 5.2 核心实体

```
Tenant (id, name, plan, sso_config, llm_quota, status, created_at)
  └─ User (id, tenant_id, email, role, sso_subject, status)
  └─ LLMConfig (tenant_id, provider, api_key_encrypted, model, qps_limit, priority)

Channel (id, tenant_id, type[web/feishu/email/...], credentials_encrypted, status, config_json)
  └─ ChannelBinding (channel_id, external_conversation_id, internal_conversation_id)

Conversation (id, tenant_id, channel_id, customer_id, status[open/pending/closed],
              assigned_agent_id, ai_handling[bool], opened_at, last_activity_at, metadata_json)
  └─ Message (id, conversation_id, role[customer/agent/ai/system/tool], content_text,
              content_blocks_json, sender_id, tool_calls_json, tokens_used,
              feedback_json, created_at)
  └─ ConversationContext (conversation_id, summary, entities_json, key_points_json, updated_at)

Ticket (id, tenant_id, conversation_id, code, title, status, priority, category,
        assignee_id, sla_first_response_at, sla_resolve_at, customer_satisfaction_score,
        created_at, resolved_at)
  └─ TicketEvent (id, ticket_id, type, actor_id, payload_json, created_at)

KnowledgeBase (id, tenant_id, name, default_for_conversation[bool])
  └─ Article (id, kb_id, tenant_id, title, slug, status[draft/published/archived],
              current_version_id, tags[], created_at, updated_at)
       └─ ArticleVersion (id, article_id, content_md, content_html, parsed_chunks_json,
                          multimodal_refs, author_id, status[draft/processing/published/failed])
       └─ Chunk (id, version_id, ordinal, text, embedding_id, modality[text/image/code],
                 metadata_json, token_count)

Tool (id, tenant_id, name, description, schema_json, http_config_json,
      auth_config_json, enabled, rate_limit, requires_approval)

AgentPolicy (id, tenant_id, name, system_prompt, allowed_tool_ids[], allowed_kb_ids[],
             model_route_json, max_steps, guardrails_json, enabled)

RoutingRule (id, tenant_id, name, priority, conditions_json, action_json, enabled)
SLA (id, tenant_id, name, conditions_json, first_response_minutes, resolve_minutes, business_hours_json)

AuditLog (id, tenant_id, actor_id, action, resource_type, resource_id,
          before_json, after_json, ip, user_agent, created_at)

LLMUsage (id, tenant_id, provider, model, prompt_tokens, completion_tokens,
          cost_usd, request_id, cached[bool], created_at)
```

### 5.3 关键设计点

- `Message.content_blocks_json` 支持富文本/图片/代码块/附件引用,渲染层统一
- `Chunk` 与向量库记录一一对应,`embedding_id` 是向量库返回的 ID
- `Tool` 声明式(类似 OpenAPI schema),Agent runtime 动态加载
- `AgentPolicy` 与 `RoutingRule` 都是租户级配置,运营可编辑
- `LLMUsage` 是计费和配额的事实表,异步批量落库

---

## 6. 关键流程

### 6.1 客户消息进入 → AI 处理 / 转人工(M1 重点)

```
1. Channel Gateway 收到外部消息(WebSocket / 飞书 Webhook)
2. 渠道适配器归一化 → MessageEnvelope{
     tenant_id, channel_type, external_user, external_conversation_id,
     text, attachments, raw_payload
   }
3. 写 outbox 事件 INCOMING_MESSAGE(防止下游失败丢消息)
4. Conversation 模块:按 (channel, external_conversation_id) 查找/创建会话
5. 写 Message(role=customer)
6. 决策:
   a. AI 处理(默认)
      · RAG 召回:混合检索(向量 + BM25) → 重排 → top-k
      · LLM 决定:直接回答 / 调用 Tool / 询问澄清 / 创建工单 / 转人工
      · 工具调用循环(有步数上限和超时)
      · 流式输出通过 WS 推给前端
      · 写 Message(role=ai 或 tool)
   b. 路由规则命中 / 显式触发转人工 → TRANSFER_TO_HUMAN 事件
      · 推送会话到坐席收件箱
      · AI 暂停生成,坐席接管
7. 坐席输入时,workspace 调 agent_runtime.suggest() 实时推荐回复
8. 全程写 AuditLog + LLMUsage
```

### 6.2 RAG 知识库入库(支持多模态)

```
1. 运营上传文件(PDF/Word/MD/图片)或粘贴文本
2. kb 模块写 Article + ArticleVersion(status=processing)
3. Worker 派发 parse_document 任务
4. 解析:
   · 文本提取(Unstructured / MarkItDown)
   · 结构识别:标题/代码块/表格/图片
   · 图片 → OCR + 多模态模型生成描述
   · 代码块保留原始文本和语言标记
5. 切片:语义切片(按标题/段落) + 长度切片,带 overlap
6. 元数据标注:来源、章节、模态、更新时间
7. 调 embedding 服务(LLM Gateway 路由)→ 向量
8. 写入向量库,带 tenant_id 和 kb_id 过滤字段
9. 更新 ArticleVersion.status=published
10. 失败重试,死信入 DLQ,运营收到告警
```

### 6.3 工单 SLA 监控

```
1. Worker 定时(每分钟)扫未解决工单
2. 命中 SLA 时间线 → 发送通知(站内 + IM)
3. 升级:超过 N 次未响应 → 自动重派或升级优先级
4. 关闭工单 → 触发客户满意度调查
```

### 6.4 离线历史会话挖掘(M2)

```
1. 定期跑批:抽样已关闭会话
2. LLM 提取 Q&A 对
3. 运营审核 → 入知识库草稿
4. 发布流程与手动上传一致
```

---

## 7. LLM Gateway

> **演进说明**:M1 阶段先实现一个**最小 LLM 客户端**(只做 provider 适配 + 基础重试),所有 RAG/Agent 调用走这个客户端;M3 再升级为完整 LLM Gateway(增加配额、缓存、降级链、Tracing)。这样 M1 不被 Gateway 复杂度阻塞,后期也不需要大规模改造调用方。

### 7.1 核心职责

在所有上层模块与 LLM 提供方之间加一层中间件,统一管理路由、配额、成本、缓存、降级、追踪。

### 7.2 架构

```
┌─────────────────────────────────────────────┐
│  上层调用方 (RAG / Agent / Workspace)         │
│   ↓ LLMRequest(prompt, model_hint, tenant)   │
├─────────────────────────────────────────────┤
│  LLM Gateway                                │
│  · Router:决策 provider/模型                 │
│  · Quota:租户配额(模型级 token/分钟)          │
│  · Cache:精确哈希缓存(可扩展语义缓存)         │
│  · Provider Adapter:OpenAI/Anthropic/自托管    │
│  · Retry + Fallback:指数退避 + 降级链         │
│  · Trace:OpenTelemetry span                 │
└─────────────────────────────────────────────┘
       ↓
LLMUsage 异步落库(成本/配额统计)
```

### 7.3 关键能力

- **路由策略**:AgentPolicy.model_route 配置,租户级 LLMConfig 覆盖
- **精确缓存**:temperature=0 时 prompt 哈希命中,直接返回;目标命中 30-50%
- **配额控制**:租户级 token/分钟上限,超限 429
- **降级链**:商业 API 限流/超时 → 切自托管 → 切模板回复
- **Prompt 模板管理**:版本化模板,运营可调,代码不改

---

## 8. 错误处理

### 8.1 错误分类与策略

| 错误类型 | 例子 | 处理 |
|---------|------|------|
| 用户输入错误 | 鉴权失败、参数非法 | 4xx + 明确错误信息 |
| 渠道瞬时故障 | 飞书 API 5xx、邮件 SMTP 失败 | 重试 + 指数退避 |
| 渠道长时不可用 | 飞书 webhook 挂掉 | 入 DLQ,告警,运营手动重放 |
| LLM 超时/限流 | OpenAI 429、Anthropic timeout | LLM Gateway 重试 + 降级 |
| LLM 输出异常 | JSON 解析失败、Tool 非法 | 校验 + 重试 1-2 次,失败转人工 |
| 数据库/缓存不可用 | PG 连接失败、Redis 抖动 | 健康检查失败 → 摘流,Worker 重试 |
| 内部 bug | 未捕获异常 | 5xx + trace_id,告警,记审计 |

### 8.2 Outbox + 重放

所有领域事件写 outbox 表,后台 dispatcher 投递到 Redis Stream,消费失败按策略重试,达上限入 DLQ。DLQ 提供 UI 供运营查看和手动重放。

### 8.3 幂等性

- 所有外部副作用(发邮件、推 IM)走 `Idempotency-Key`
- 工具调用带 `request_id`,LLM 重试不重复执行
- 渠道回调去重(按 `external_message_id`)

---

## 9. 安全与合规

### 9.1 数据安全

- 静态加密:PG TDE / 透明加密;向量库加密存储
- 传输加密:全站 TLS 1.3,内网 mTLS
- 凭证加密:租户的 LLM API key、IM token、邮箱密码用 KMS 托管密钥加密(envelope encryption)
- 密钥管理:租户级 DEK 定期轮换,KEK 在 KMS

### 9.2 LLM 数据保护

- 商业 API 模式默认关闭"训练数据使用"选项
- 敏感字段(手机号/身份证/卡号)正则 + NER 检测,脱敏后再送 LLM,响应回来再回填
- LLM 交互日志按租户配置保留期(默认 30 天)

### 9.3 权限与合规

- **RBAC**:管理员 / 团队主管 / 坐席 / 只读
- **SSO**:OIDC / SAML 2.0(M5)
- **审计**:所有写操作 + 敏感读操作落 AuditLog,租户可导出
- **数据导出/删除**:GDPR/个保法,运营一键导出 / 软删 + 异步硬删
- **敏感词过滤**:进站出站双向,租户可配词库
- **Web Widget**:origin 校验、签名 token、CSP、链接 `rel="noopener noreferrer"`

### 9.4 租户隔离保障

- 任何 Repository 查询必须传 `tenant_id`,缺则抛 `MissingTenantContextError`
- SQLAlchemy `before_compile` 事件强制注入
- CI 跑"跨租户访问扫描"测试

---

## 10. 可观测性

### 10.1 三件套

1. **Logging**:结构化 JSON,贯穿 `request_id`/`trace_id`/`tenant_id`/`conversation_id`
2. **Metrics**(Prometheus):
   - 业务:会话数、解决率、转人工率、CSAT、响应时长
   - 系统:LLM QPS/P99/错误率(按 provider 拆分)、Worker 队列长度
3. **Tracing**(OpenTelemetry):关键路径端到端 span,接 Jaeger/Tempo

### 10.2 告警分级

- P0:核心服务不可用、所有租户受影响
- P1:关键路径错误率 > 5% 或 P99 延迟翻倍
- P2:LLM 成本异常、队列积压
- P3:单租户异常

### 10.3 质检(M2)

- 抽样 LLM-as-Judge 自动评分
- 敏感词检测、引用一致性(幻觉)
- 人工复核高风险会话

---

## 11. 测试策略

### 11.1 测试金字塔

| 层级 | 范围 | 工具 | 目标 |
|------|------|------|------|
| 单元 | 各模块 domain/service 纯逻辑 | pytest + pytest-asyncio | 80%+ |
| 集成 | 真实 DB/Redis/向量库,模块协作 | pytest + testcontainers | 关键路径全过 |
| 端到端 | 模拟完整客户旅程 | Playwright + 真实后端 | 核心场景 100% |
| LLM 评估 | RAG 召回质量/Agent 准确率 | 自建 eval + LLM judge | 基线 + 回归 |
| 渠道契约 | 飞书/邮件协议兼容 | SDK mock + sandbox | 每渠道覆盖 |

### 11.2 关键原则

- LLM 依赖隔离:Agent/RAG 单测用 mock,CI 默认不花 token
- 向量库可重放:RAG 评估用固定语料快照
- 时间注入:SLA、监控用 freezegun
- 租户隔离测试:跨租户访问必须 404/403
- 金丝雀用例:每发布跑"客户进站→AI 答→转人工→关单"全链路

### 11.3 离线 LLM Eval 集

- 构造 200+ 真实客户问题样本(M1 可 50+)
- 标注:期望来源文档、是否需转人工、是否需调工具
- CI 跑 eval,沙箱租户,最便宜模型
- 监控:Top-k 召回率、引用准确率、转人工率、CSAT 预测

---

## 12. 部署与运维

### 12.1 容器化

- 单镜像多入口:`api` / `worker` / `beat` / `migrate` 启动命令不同
- Python 用 `uv` 或 `poetry` 管理依赖
- 多阶段构建,最终镜像 slim

### 12.2 编排

- Kubernetes 三个 Deployment:
  - `api`:HPA,3+ 副本
  - `worker`:队列长度驱动
  - `beat`:单实例
- 资源限制 + 探针(liveness / readiness / startup)

### 12.3 CI/CD

- 流水线:lint → mypy → 单测 → 集成测试 → 构建镜像 → 推送 → 部署 staging → 跑 eval → 手动批准 prod
- 镜像不可变,标签 = 版本 + git SHA
- 蓝绿或金丝雀发布,秒回滚

### 12.4 环境分层

- dev → staging(数据脱敏)→ prod
- 每环境独立租户、配额、LLM 配额

### 12.5 灾备

- RPO ≤ 15 分钟(WAL 归档)
- RTO ≤ 1 小时(主从 + 自动故障转移)
- 向量库按租户可重建(原始文档可重索引)
- 季度恢复演练

### 12.6 运维面板

- Grafana 仪表板:核心指标 + 业务指标 + LLM 成本
- Admin UI:租户管理、配额调整、DLQ 查看、会话回放
- On-call 告警走 PagerDuty / 钉钉

---

## 13. 实施里程碑

| 里程碑 | 内容 | 交付 |
|--------|------|------|
| **M1:地基** | 租户 + 鉴权 + Channel(Web+飞书) + 单次 RAG + 基础坐席台 + **最小 LLM 客户端** | MVP 可演示,Web/飞书端到端 |
| **M2:AI 核心** | Agent 工具调用 + 工单 + 知识库多模态 + 邮件渠道 + 历史会话挖掘 | 全 AI 能力闭环 |
| **M3:生产化** | LLM Gateway(配额/缓存/降级) + 全量可观测性 + 离线 Eval | 可对外服务 |
| **M4:规模化** | 其他 IM 渠道(钉钉/企微/Slack/Teams) + 高级路由 + 质检 | 多渠道企业版 |
| **M5:企业能力** | SSO + 审计 + 高级权限 + 合规模板 | 面向中大型企业 |

---

## 14. 风险与开放问题

### 14.1 风险

| 风险 | 缓解 |
|------|------|
| LLM 成本失控 | 缓存、配额、租户级 cap、自托管优先 |
| RAG 召回不准 | 离线 eval 持续监控、重排、混合检索 |
| 多租户数据泄漏 | RLS + 自动化测试覆盖 |
| IM 协议差异大 | 渠道适配器隔离、各自单测 |
| LLM 幻觉 | 强制引用知识库来源 + LLM-as-Judge 质检 |

### 14.2 开放问题(实施时确认)

- 知识库编辑界面:独立前端 or 内嵌坐席台?
- 移动端坐席 App:是否做?(本设计不覆盖)
- 多区域部署:起步单区域够用,M4 再考虑
- 与公司现有 SSO 集成:具体协议?

---

## 15. 附录:目录结构建议

```
ai-customer/
├── apps/
│   ├── api/                  # FastAPI 主应用
│   │   ├── src/
│   │   │   ├── tenant/
│   │   │   ├── channel/
│   │   │   ├── conversation/
│   │   │   ├── ticket/
│   │   │   ├── kb/
│   │   │   ├── rag/
│   │   │   ├── agent_runtime/
│   │   │   ├── tool_registry/
│   │   │   ├── workspace/
│   │   │   ├── analytics/
│   │   │   ├── llm_gateway/
│   │   │   ├── audit/
│   │   │   └── core/         # 共享基础设施
│   │   ├── tests/
│   │   ├── pyproject.toml
│   │   └── Dockerfile
│   ├── worker/               # Celery/Arq worker
│   │   └── src/
│   │       ├── tasks/
│   │       └── ...
│   └── web/                  # 坐席台前端 (React)
│       ├── src/
│       └── package.json
├── deploy/
│   ├── k8s/
│   ├── helm/
│   └── docker-compose.yml
├── docs/
│   └── superpowers/
│       └── specs/
├── scripts/
└── README.md
```

---

**审阅要点**:

1. 整体架构(模块化单体 + Worker)是否符合预期?
2. MVP 范围(Web Widget + 飞书)是否合理?
3. 多租户隔离策略(共享库 + RLS)是否可接受?
4. LLM Gateway 设计是否够用?
5. 里程碑划分是否合理?
