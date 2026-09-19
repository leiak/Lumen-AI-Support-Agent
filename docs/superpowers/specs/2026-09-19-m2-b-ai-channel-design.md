# M2.B — Email Channel + Multimodal KB + History Mining 设计

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Date:** 2026-09-19
**Status:** 设计已批准 (用户 "ok" 确认),等待 plan 撰写
**Scope:** M2.B — 渠道扩展 + KB 富化 + 离线挖掘
**Predecessor:** M2.A (✅ Stage 12-15, commit `4ecd579`)

---

## Context

M2.A 已在 main 分支完整交付 (Stage 12 search_internal_kb / Stage 13 Ticket / Stage 14 QA Judge / Stage 15 README)。当前 AI 客户支持能力局限于：

1. **入口单一**：只支持 widget (iframe) + admin SPA,无邮件渠道
2. **KB 单模态**：只支持文本 KB article + text embedding,无法处理客服场景常见的截图、错误照片、PDF 账单/合同
3. **数据未挖掘**：历史会话只用于 QA 实时评分,没有离线分析,知识盲点无法系统化发现

M2.B 解决这三点,闭环产品价值。

---

## 范围

| Stage | 子系统 | 范围 |
|-------|----------|------|
| 16 | **Email 渠道** | webhook 入站 + 纯文本 + AI 自动回复外发 (SES SendEmail API) |
| 17 | **Multimodal KB** | 图片 (PNG/JPG/WebP) + PDF (pdfplumber 文本 + 关键页 vision 截图) |
| 18 | **History Mining** | HDBSCAN 聚类 + LLM 生成 KB 草稿 + admin 人工复核 |

**顺序**：Stage 16 → Stage 17 → Stage 18 (线性推进,Stage 18 复用 17 的 embedding 抽象)

---

## Stage 16 — Email 渠道

### 目标
让客户可以通过邮件与 AI 客服对话,AI 自动回复邮件外发。Agent 仍走 admin SPA UI (不走邮件)。

### 架构
```
[AWS SES Inbound] --HTTP POST--> /api/v1/email/inbound
                                   ↓
                  channel.inbound_email.process_inbound_envelope
                                   ↓
                  ConversationService.find_or_create_for_inbound
                  (key: tenant_id + email_thread_id)
                                   ↓
                  ConversationService.record_message (CUSTOMER role)
                                   ↓
                  SimpleResponder.respond → AI 回复
                                   ↓
                  channel.outbound_email.send_reply (SES SendEmail API)
                                   ↓
                  ConversationService.record_message (AI role, 标记 source=email)
```

### 模块

**`apps/api/src/channel/inbound_email.py`**
- 复用 `process_inbound_envelope` 模式
- 接收 SES inbound POST payload (JSON,含 message_id + from + to + subject + body + In-Reply-To + References)
- `email_thread_id` = `References` header 第一项 (thread leader),若无则用 SES message_id

**`apps/api/src/channel/outbound_email.py`**
- `EmailOutbound.send_reply(*, tenant_id, conversation_id, to_email, subject, body_text, in_reply_to_message_id)`
- HTTP POST to SES SendEmail API: `https://email.{region}.amazonaws.com/v2/email/outbound-emails`
- 用 `httpx.AsyncClient` + AWS Signature V4 (手写或 `botocore` 签名)
- 选择 `botocore` 是因为已经在生产用 S3/MinIO,docker image 已有 boto3

**`apps/api/src/core/email_parser.py`**
- `parse_ses_inbound(payload: bytes) -> ParsedEmail`
- 提取 from / to / subject / body (text/plain) / In-Reply-To / References
- 失败 → log + 200 OK 给 SES (避免 SES retry 风暴)

**数据库变更** (alembic migration):
```sql
ALTER TABLE conversations
  ADD COLUMN email_thread_id VARCHAR(128) NULL,
  ADD COLUMN email_message_id_header VARCHAR(128) NULL;
CREATE INDEX idx_conversations_email_thread ON conversations(tenant_id, email_thread_id);
```

### 范围切分 (M2.B 不做)
- ❌ Agent 通过邮件回复 (admin SPA 仍是 agent 主入口)
- ❌ HTML 邮件 (只用 plain text)
- ❌ IMAP polling (只用 SES webhook)
- ❌ 附件 (图片/PDF 附件只存元数据,不下载、不传给 AI;留给 Stage 17 KB 上传场景)

### 风险与缓解

| 风险 | 缓解 |
|------|------|
| SES 区域多样性 | 配置项 `aws_region` 默认 `us-east-1` |
| AWS Signature V4 实现复杂度 | 复用 boto3 `client.send_email()` 高层 API,不手写签名 |
| Inbound 邮件重复 (SES retry) | 用 `email_message_id_header` 做幂等键,UPSERT on conflict |
| 大邮件 body | 截断到 100 KB (LLM context 限制),只持久化截断版 |

### 测试

- **Unit**: `test_email_parser.py` (3 tests) — SES payload 解析 + multipart fallback + 异常 payload 不崩
- **Unit**: `test_outbound_email.py` (5 tests, mocked httpx) — SendEmail 调用签名 + 失败重试 + thread headers 正确
- **Integration**: `test_email_inbound_e2e.py` (4 tests, 真实 Postgres) — 完整 inbound → AI reply → outbound 端到端
- **Playwright** (手动录屏): `tests/e2e/demo-act5-01-email.spec.ts`

---

## Stage 17 — Multimodal KB

### 目标
让 KB 能存图片和 PDF,客户提问"账单怎么看"或"这个错误怎么修"时,AI 能找到相关截图/PDF 内容并引用。

### 架构
```
[Admin 上传 PNG/JPG/WebP/PDF] → POST /api/v1/kb-articles/multimodal
                                          ↓
                  knowledge.multimodal.processor.process_upload(file)
                                          ↓
                  类型分发：
                  - 图片 → vision_embedder.encode(image_bytes)
                  - PDF → pdf_processor.extract(file):
                      (a) pdfplumber 提取文本 → text_chunks (走现有 text_embedder)
                      (b) 关键页检测 → page_screenshots (PNG bytes)
                          → vision_embedder.encode(screenshot)
                                          ↓
                  入库：
                  - text_chunks → Qdrant (现有 collection)
                  - image_vectors → Qdrant (新 collection: kb_image_vectors)
                  - 原始文件 → 对象存储 (S3/MinIO via boto3)
                  - DB row: kb_multimodal_articles (新表)
```

### 模块

**`apps/api/src/knowledge/multimodal/embedder.py`**
- `VisionEmbedder` 抽象接口：`async encode(image_bytes: bytes) -> list[float]`
- 实现：`DoubaoVisionEmbedder` (Doubao `embedding-vision-v1` 或 `multimodal-embedding-v1`)
- 与 Stage 14 `LLMClient.with_config(provider="doubao_vision", model=...)` 同模式

**`apps/api/src/knowledge/multimodal/pdf_processor.py`**
- `PdfProcessor.extract(file_bytes: bytes) -> ExtractedPdf`
- 文本提取：pdfplumber 逐页,空页跳过
- 关键页检测：页内 `page.images` 数 ≥ 1 或 `page.charts` (chart 检测用 page.find_tables() 启发式)
- 截图：pdf2image (`poppler` 二进制依赖) 渲染关键页为 PNG
- `ExtractedPdf = {text_chunks: list[str], screenshot_pages: list[(page_num, bytes)]}`

**`apps/api/src/knowledge/multimodal/storage.py`**
- `ObjectStore` 抽象接口：`put(key, bytes) -> url`, `get(url) -> bytes`
- 实现：`S3ObjectStore` (生产,AWS S3) + `MinIOObjectStore` (dev,M2.A 已有 MinIO docker)
- 两者都通过 boto3,差异只在 endpoint_url

**`search_internal_kb` 工具扩展** (Stage 17 复用 Stage 12 工具):
- 检索时同时查 text collection + image collection
- 融合排序：用 Reciprocal Rank Fusion (RRF, k=60) 合并两路排序
- 引用返回：`{source_type: "text" | "image" | "pdf", article_id, chunk_id, page_num?}`
- 客户只能看到 text/image 标签,不暴露内部 ID

**数据库变更** (alembic migration):
```sql
CREATE TABLE kb_multimodal_articles (
    id VARCHAR(26) PRIMARY KEY,
    tenant_id VARCHAR(26) NOT NULL,
    kb_slug VARCHAR(64) NOT NULL,
    title TEXT NOT NULL,
    file_url TEXT NOT NULL,  -- S3/MinIO URL
    mime_type VARCHAR(64) NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    text_chunks_count INT NOT NULL DEFAULT 0,
    image_chunks_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    FOREIGN KEY (tenant_id, kb_slug) REFERENCES kb_definitions(tenant_id, slug)
);
CREATE INDEX idx_kb_mm_articles_tenant ON kb_multimodal_articles(tenant_id, created_at);

-- Qdrant: 新 collection 'kb_image_vectors'
-- 维度: doubao-vision-embedding = 1024
-- 距离: Cosine
-- payload: {tenant_id, kb_slug, article_id, page_num?, mime_type}
-- filter: MUST {tenant_id} (继承 M1 多租户规则)
```

### 范围切分 (M2.B 不做)
- ❌ 音频 / 视频 (只 PNG/JPG/WebP/PDF)
- ❌ 多模态 AI 回复 (AI 回复仍纯文本,只 KB 检索时用 vision embedding)
- ❌ OCR (PDF 文本提取用 pdfplumber,不调 Vision LLM 做 OCR;图表页才走 vision)
- ❌ Live preview 编辑 (admin 上传后不能编辑,只能删除重建)

### 风险与缓解

| 风险 | 缓解 |
|------|------|
| Doubao vision API 未配置 | 配置项 `doubao_vision_api_key`,缺失时降级到 text-only (search_internal_kb 返回空) |
| Poppler 二进制依赖 (pdf2image 需要) | Dockerfile 加 `apt-get install -y poppler-utils` |
| S3 凭证泄露 | 不存明文,走 K8s secret (M2.B 不部署,先在 docker-compose 用 env) |
| 大图片内存爆 | 上传前 resize 到 max 2048x2048 (PIL),降低 vision embedding 成本 |
| 检索相关性下降 (混合检索) | RRF 简单稳定,不引入 learned re-ranker |

### 测试

- **Unit**: `test_pdf_processor.py` (6 tests) — 文本提取 + 关键页检测 + 空 PDF + 大 PDF (50 页) + 损坏 PDF
- **Unit**: `test_vision_embedder.py` (4 tests, mocked httpx) — Doubao 调用签名 + retry + 降级
- **Unit**: `test_search_internal_kb_multimodal.py` (5 tests) — 双路检索 + RRF 融合 + 租户隔离
- **Integration**: `test_multimodal_e2e.py` (5 tests, 真实 Postgres + Qdrant + MinIO) — 上传 → 检索 → 引用端到端
- **Playwright**: `tests/e2e/demo-act5-02-multimodal.spec.ts`

---

## Stage 18 — History Mining

### 目标
每周一次离线分析过去所有 AI 已解决的会话,聚类发现高频问题,自动生成 KB 草稿,admin 复核后入库。闭环产品价值:挖掘 → 建议 → 发布。

### 架构
```
[Arq cron: 每周日 03:00 UTC] → history_mining_worker(ctx)
                                          ↓
                  1. 拉取过去 30 天所有 RESOLVED 状态 conversation
                     (只取 customer question 文本,过滤掉 personal info)
                  2. TextEmbedder.encode(question) for each
                  3. HDBSCAN 聚类 (min_cluster_size=10, min_samples=5)
                  4. 丢弃噪声 (-1 簇) 和超大簇 (> 1000 question)
                  5. 对每个有效 cluster:
                     (a) 取 centroid 最近的 5 条 representative questions
                     (b) LLM 生成 KB 草稿 (structured output):
                         {title, body, suggested_tags}
                     (c) 写入 kb_article_drafts (status=DRAFT)
                                          ↓
                  6. ArqRedis publish "history_mining.complete"
                     (admin SPA 监听,有新草稿时弹通知)
```

### 模块

**`apps/api/src/history_mining/clusterer.py`**
- `HdbscanClusterer.cluster(embeddings: np.ndarray) -> list[Cluster]`
- HDBSCAN lib (scikit-learn-contrib/hdbscan)
- `Cluster = {id: int, centroid_idx: int, question_ids: list[str], size: int}`
- 输入要求：min_cluster_size 可配置 (M2.B 起步值 10,可在 admin 配置)

**`apps/api/src/history_mining/draft_generator.py`**
- `KBDraftGenerator.generate(cluster_questions: list[str]) -> KBDraft`
- 调 LLM (复用 LLMClient) 用 structured output
- Prompt 包含：5 个 representative questions + "生成一个 KB article title + body"
- `KBDraft = {title: str, body: str, suggested_tags: list[str]}`

**`apps/api/src/history_mining/worker.py`**
- `history_mining_worker(ctx)` Arq 任务
- `WorkerSettings.cron_jobs` 加 `cron(history_mining_worker, hour=3, minute=0, weekday=6)` (UTC 周日 03:00)
- 进度日志：每 1000 条会话打 INFO log
- 失败隔离：单个 cluster 失败不影响其他 cluster

**数据库变更** (alembic migration):
```sql
CREATE TABLE kb_article_drafts (
    id VARCHAR(26) PRIMARY KEY,
    tenant_id VARCHAR(26) NOT NULL,
    cluster_id INT NOT NULL,  -- 每次 worker run 重新从 0 编号;无跨 run 关联,纯 reference 用途 (UI 显示 "Cluster #42" 用)
    source_questions JSONB NOT NULL,  -- 5 个 representative question IDs
    suggested_title TEXT NOT NULL,
    suggested_body TEXT NOT NULL,
    suggested_tags JSONB NOT NULL DEFAULT '[]',
    status VARCHAR(16) NOT NULL DEFAULT 'DRAFT',  -- DRAFT / APPROVED / REJECTED
    published_article_id VARCHAR(26) NULL,  -- 复核发布后填,关联 kb_articles.id
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    reviewed_at TIMESTAMP WITH TIME ZONE NULL,
    reviewed_by VARCHAR(26) NULL
);
CREATE INDEX idx_kb_drafts_tenant_status ON kb_article_drafts(tenant_id, status, created_at DESC);
```

**Admin API** (`apps/api/src/admin/api.py` 新增):
- `GET /api/v1/admin/kb-drafts?status=DRAFT` — 列出草稿
- `POST /api/v1/admin/kb-drafts/{id}/approve` — 复核通过 → 创建 `kb_articles` 行
- `POST /api/v1/admin/kb-drafts/{id}/reject` — 拒绝 → 标记 REJECTED
- 复用 M2.A Ticket API 的 `transition` 模式做状态机 (DRAFT → APPROVED → 发布 / DRAFT → REJECTED)

**Admin SPA** (Stage 18.5,推迟到 M3):
- `/kb-drafts` 页面,表格 + 详情抽屉
- Approve / Reject 按钮 + 二次确认
- 复用 M2.A Stage 9 admin SPA 的 drawer pattern
- **M2.B 范围不包含 admin SPA 页面**,只交付后端 API + 草稿数据 + curl 测试命令。Admin SPA 推到 M3 早期 stage。

### 范围切分 (M2.B 不做)
- ❌ 实时流式聚类 (只每周一次离线)
- ❌ 自动发布 (必须 admin 复核)
- ❌ KB 草稿编辑 (admin 只能 approve/reject,不能编辑 body 后发布)
- ❌ 多语言 (只支持单一语言,默认中文)
- ❌ 跨租户挖掘 (每租户独立挖掘,不聚合)

### 风险与缓解

| 风险 | 缓解 |
|------|------|
| HDBSCAN 慢 (10K+ embeddings) | 每周一次,offline,允许 30 分钟跑完 |
| LLM 幻觉 (生成的 KB body 不准确) | admin 人工复核是最后一道关;草稿只进独立表,未发布不入主 KB |
| 重复聚类 (同一问题周周跑) | 跑前检查:与已发布 KB article 相似度 > 0.9 的 cluster 自动标记为 DEDUP,不入草稿 |
| 隐私 (挖掘涉及真实客户问题) | 复用 M1 PII 规则:只存 question ID,生成 body 时 LLM 不接收原始 customer 文本 (只接收 5 个 ID + 嵌入式 context) |

### 测试

- **Unit**: `test_hdbscan_clusterer.py` (5 tests) — 全噪声 / 单大簇 / 多簇 + 边界 size
- **Unit**: `test_draft_generator.py` (4 tests, mocked LLMClient) — structured output 解析 + 异常 fallback
- **Unit**: `test_admin_kb_drafts_api.py` (5 tests) — list / approve / reject + 跨租户 404
- **Integration**: `test_history_mining_e2e.py` (4 tests, 真实 Postgres) — 完整 worker 跑通 + 草稿入库
- **Playwright**: `tests/e2e/demo-act5-03-history-mining.spec.ts`

---

## 跨 Stage 共享

### 复用 M2.A 模式

| 模式 | 复用点 |
|------|--------|
| **LLMClient** | Stage 17 vision embedder + Stage 18 draft generator 都通过 `LLMClient.with_config` 选 model |
| **Structured Output** | Stage 17 PDF 关键页检测 + Stage 18 KB 草稿都用 Stage 14 JudgeClient 的 `chat_with_structured_output` 模式 |
| **租户隔离** | M1 多租户规则全继承 (DB WHERE / Qdrant MUST filter / cross-tenant 404) |
| **Arq Worker** | Stage 18 复用 Stage 14 worker 模式 (`qa/worker.py` → `history_mining/worker.py`) |
| **PII 纪律** | Stage 18 mining 涉及真实客户问题,严格遵守 M1 PII 规则 (只存 ID,日志无文本) |

### 新增依赖

```toml
# Stage 16
boto3 = ">=1.34"  # SES SendEmail 已有 boto3 链 (S3/MinIO 同源)

# Stage 17
pdfplumber = ">=0.10"
pdf2image = ">=1.17"  # 需要 poppler-utils 系统依赖
Pillow = ">=10.0"
hdbscan = ">=0.8"  # Stage 18 也用
```

### 配置项 (core/config.py)

```python
# Stage 16
aws_region: str = "us-east-1"
ses_inbound_topic_arn: str  # SES 通知 SNS topic
ses_from_address: str  # 默认 from address

# Stage 17
doubao_vision_api_key: str  # 可空,空时降级到 text-only
doubao_vision_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
doubao_vision_model: str = "doubao-embedding-vision"
object_store_endpoint: str  # MinIO/S3 endpoint
object_store_bucket: str
object_store_access_key: str
object_store_secret_key: str

# Stage 18
history_mining_enabled: bool = True
history_mining_lookback_days: int = 30
history_mining_min_cluster_size: int = 10
history_mining_max_cluster_size: int = 1000
```

---

## 不做的 (M2.B 显式跳过)

| Plan | 理由 |
|------|------|
| Agent 通过邮件回复 | admin SPA 仍是 agent 主入口;邮件外发只用于 AI 自动回复 |
| HTML 邮件 | 引入 HTML sanitizer 风险;客户体验纯文本够用 |
| IMAP polling | 与现有 webhook 架构不一致;polling 延迟高 |
| 多模态 AI 回复 | AI 回复仍纯文本;只 KB 检索时用 vision |
| 实时聚类 | 离线批处理够用;实时聚类需 streaming 算法 |
| KB 草稿自动发布 | admin 复核是最后一道质量关 |
| 跨租户挖掘 | 隐私 + 监管风险,先不做 |

---

## 验证判据 (M2.B 全部完成时)

### Stage 16
- [ ] SES webhook 端到端: 客户发邮件 → AI 自动回复邮件
- [ ] Email thread 自动合并 (In-Reply-To 路由到同一 conversation)
- [ ] SES 失败重试幂等 (重复 webhook 不创建重复 conversation)
- [ ] 9 个新单元测试 + 4 个集成测试全过
- [ ] `images/demo-act5-01-email.png` 存在

### Stage 17
- [ ] PNG 上传 → KB 检索能找到 (text-only query)
- [ ] PDF 上传 → 文本 + 关键页截图都入 Qdrant
- [ ] search_internal_kb 工具返回 multimodal 引用 (text + image)
- [ ] RRF 融合排序稳定 (regression test)
- [ ] 20 个新单元测试 + 5 个集成测试全过
- [ ] `images/demo-act5-02-multimodal.png` 存在

### Stage 18
- [ ] Weekly worker 跑通,生成 KB 草稿
- [ ] Admin SPA `/kb-drafts` 列表 + Approve/Reject 工作
- [ ] Approve 后 `kb_articles` 有新行,草稿 status=APPROVED
- [ ] HDBSCAN 边界 size 测试覆盖
- [ ] 14 个新单元测试 + 4 个集成测试全过
- [ ] `images/demo-act5-03-history-mining.png` 存在

### 整体
- [ ] 所有 Stage commit + push 到 main
- [ ] `pytest --collect-only` 0 errors
- [ ] `pytest -m "not integration"` 全过 (M2.A 608 + M2.B ~50 新测试 = 658+)
- [ ] M1 PII 纪律 + 多租户隔离全部继承
- [ ] README 状态表新增 3 行 ✅
- [ ] `docs/demo-script.md` 新增 Act 5 章节

---

## 衔接 M3

M2.B 完成后,以下 seam 为 M3 准备:

1. **邮件渠道可扩展**: SES 之外的 SendGrid/Postmark/Mailgun,只换 `channel/outbound_email.py` 实现
2. **vision embedding 可换**: Doubao 之外的 OpenAI CLIP / Anthropic Claude Vision,只换 `VisionEmbedder` 实现
3. **history mining 触发可换**: cron 之外的实时 trigger (新 conversation 入库时触发增量聚类)
4. **LLM Gateway 接入点**: 所有 LLM 调用已通过 `LLMClient`,M3 直接做 wrapper 拦截 + 限流

---

## 下一步

用户批准此 design doc 后,调用 `superpowers:writing-plans` skill 把 Stage 16-18 拆成可执行的 task plan,然后用 `subagent-driven-development` 串行执行。
