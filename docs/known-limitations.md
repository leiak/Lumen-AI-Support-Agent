# Known Limitations (intentional deferrals)

> **这个文件从主 [README](../README.md) §"已知技术债 — 分类索引" 抽出来。**
> 主 README 只留 Tech debt follow-ups(actionable, scheduled)+ 已 ship 的
> M4.C / M4.D / M5.1 组件表;本文件汇总**主动选择 / 推迟**的 29 条
> known limitations。Tech debt follow-ups 见主 README 同步行。

我们把已知技术债分成两类,目的不同:
- **Known limitations** — 主动选择 / 推迟,暂无具体使用场景或依赖后续 milestone(**本文件**)
- **Tech debt follow-ups** — 已经识别 + 可执行,排在某个 milestone 的 backlog(主 README)

DONE 折线(~~strikethrough~~)表示该项在后续 milestone 已修,但**作为
技术决策记录**保留,方便读者了解 trade-off 是怎么被解决的。

## 1. `_reset_db_singletons` autouse fixture` 在多个集成测试文件重复 — 集中到 `apps/api/tests/conftest.py` 待 follow-up

## 2. **多模态 RAG** — `_log_ocr_todo_once` 留待 vision model 接入

## 3. **跨并发 ContextVar 测试** — 当前 asyncio 单 task 假设,worker pool 共享 task 时需加 `asyncio.gather` 回归

## 4. **业务指标当前无 `tenant_id` label** — 故意不加避免 Prometheus 基数爆炸;按需租户维度走 `llm_usage` 表(账单路径)

## 5. **demo mp4 留 presenter** — 自动化脚本只产 PNG;mp4 需要 presenter + 音频,自动化做不出

## 6. ~~**Plan 10.4 LLM/tracing spans 留 M3** — M1 close-out 只做 request_id 贯穿;OpenTelemetry 留到 M3 LLM Gateway~~ — DONE (M6; OTel SDK + Collector + Tempo + hybrid instrumentation + W3C `traceparent` + `@traced` + PII whitelist; 42 new tests; closes known limitation line 415)

## 7. **Plan 10.7 K8s manifests 留 M3/M4** — docker-compose 单区域部署够用

## 8. **`search_internal_kb` 只查 KB 文本** — 当前只检索 Qdrant 文本块;vision/多模态(图/PDF 图表)待 M2.B

## 9. ~~**Ticket 工单当前无 UI**~~ — Stage 20 #20 admin SPA UI 已 ship `/admin/tickets/{id}` (`admin-ticket-detail.tsx`),demo-act4-02 spec 已启用(`1713c8e`),PNG 已捕获到 `images/demo-act4-02.png`

## 10. **Judge 模型假阳性** — QA Judge 阈值默认 0.3,首批 spot-check 后调;`lumen_qa_flagged_total` 曲线要人工盯

## 11. **`stream_chat()` skips fallback** — 设计选择 (mid-stream switch 不可靠);M4.B+ 视客户用例再考虑

## 12. **No per-step budget** — `attempt_timeout_s` 单一值应用到所有 step;Deferred

## 13. ~~**No chain-warm metrics** — 只 success/failure 计数,无滚动延迟 / 错误率~~ — DONE (M6; chain-warm latency / error rate now visible in `llm.chat` span attributes per-call; per-step histograms still a follow-up)

## 14. **No explicit cache invalidation on admin write** — ≤ 60s TTL,操作员在 TTL 窗口内看到旧配置

## 15. **Fernet key rotation** — master key 启动时加载一次;rotation 需要 restart + re-encrypt + update env,无 zero-downtime rotation

## 16. ~~**No audit log** — `tenant_llm_configs` 更新无审计~~ — DONE (M5.3; `tenant_llm_config_audit_log` append-only table + `GET /api/v1/admin/tenants/{id}/llm-configs/audit` endpoint + atomicity-guaranteed wrapper; 41 new tests + 5 commit; closes tech debt line 372)

## 17. **No per-tenant fallback chain** — chain 是 project-wide

## 18. **`enabled=FALSE` semantics** — 当前 "skip in resolver"

## 19. **No per-tenant model override** — model 是 project-default per provider

## 20. **Demo / staging seeding** — demo tenant 必须通过 admin API

## 21. **Post-mortem gate observes after chain exhausted** — 不 preempt mid-chain retries;pre-flight rejection 需更深的 M4.B `FallbackResolver` 耦合

## 22. **Non-UTC anchored tenants see breakdown drift on month-boundary days** — `PerModelBreakdown` 按 UTC month 聚合,snapshot 按 anchored month;修需要 `tz_name` 穿透到 `PerModelService`,出 Pack B scope

## 23. **No non-LLM cost budget** — SES / SMS / KB retrieval / embedding 未计入 monthly cap

## 24. **`apps/api/src/workers/` 目录是死的** — M6.5 时确认实际 worker code 都在 `src/qa/worker.py`(`WorkerSettings` 仍在 `src/workers/__init__.py` re-export);PII static scan 把 `workers/` 当一个子目录扫但里面没 span code,纯浪费;应改成扫 `qa/` 或 drop `workers/`

## 25. **Legacy `TenantResolver` class** (`apps/api/src/llm_client/tenant_resolver.py:511`) — M6.3 fix 后用 `TenantResolverSpan` 替代,但旧 class 仍 export(`src/llm_client/__init__.py:26`)。标了 deprecation 但还在跑,应移到 `__all__ = []` 或 M7 删

## 26. **`OtelAttrsException` Protocol 未被使用** (`apps/api/src/observability/exceptions.py:22`) — M6.2 定义了一个 `__otel_attrs__` 协议但没 exception class 实现它,纯 dead code。Plan 提的 follow-up 还没做

## 27. **`worker_context.py` 有未用 `trace` import** — 用 `_ = trace` workaround,cosmetic,直接删 import 即可

## 28. **No unit test for `http.request → llm.chat` parent-child** — E2E + Round 12 + 中间件单独测都覆盖,但 unit-level `InMemorySpanExporter` 配 `client.py:chat` 验证父子关系的 fast test 缺失。Follow-up test in `tests/llm_client/test_llm_gateway_creates_span.py`

## 29. **`provider_name` (underscored) vs `channel.name` / `worker.job_name` / `conversation.id` (dotted) 一致性** — 同概念("entity name")用了两种分隔符;有意为之(单概念 IDs 用 flat,namespace 字段用 dotted),但 reader 要知道。后续可全统一到 dotted form

---

**索引** — 这 29 条按 milestone 出现顺序编号;Tech debt follow-ups(commit-bound,已 ship)见 [README §"已知技术债 — 分类索引"](../README.md#已知技术债--分类索引)。
