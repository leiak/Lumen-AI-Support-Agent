# M4.A — LLM Gateway Core & Multi-Provider Routing

**日期**: 2026-09-20
**项目代号**: ai-customer
**状态**: 设计稿,待用户审阅
**目标读者**: 技术评审、研发
**系列**: M4 (LLM Gateway) — 子项目 A。M4.B (fallback) / M4.C (per-tenant BYOK) / M4.D (token budget) 在后续 spec 中设计,均以本 spec 的 resolver 为接入点。

---

## 1. 背景与目标

M1 阶段 `LLMClient` 是一次只持一个 provider 的薄壳(`provider: BaseProvider` 字段,retry + usage + 5-outcome metric)。该形态在 M3 收尾后,4 个生产场景已暴露瓶颈:

- **可靠性**:单一 provider 5xx → 全站拒绝服务,无可用兜底。
- **多模型**:不同调用点(QA Judge、suggest-reply、agent graph reasoning)需要不同性价比的模型。
- **多租户**:全部租户共享 project-wide key,无法 BYOK,无法按租户计费。
- **成本控制**:无任何 token 上限,一次异常对话可烧光整月预算。

M4 的整体目标是落地一个**生产可用的 LLM Gateway**。M4.A 是入口子项目:把现有的"单 provider 壳"重构为"可路由的多 provider 网关",为后续 M4.B/C/D 三个子项目提供稳定的接入面。

**M4.A 子项目目标**:
- 引入 `LLMGateway` 类,持有 provider registry,按 `ChatRequest.model` 前缀自动路由。
- `LLMClient` 改造为持有 `provider_resolver: Callable[[ChatRequest], BaseProvider]`,而非固定 provider。
- 用 `gateway.with_config(provider=, model=)` 替换 `LLMClient.with_config` 的 stub,真正落地 per-call 覆盖。
- 全部 5 个调用点迁移完成,既有测试零回归。

**非目标 (M4.A 不做)**:
- Fallback chain — M4.B
- Per-tenant BYOK / DB-backed provider config — M4.C
- Per-tenant token budget — M4.D
- 删除 `LLMClient` 类 — 后续 M4.Z 清理阶段(本次保留以减小迁移面)
- Caching / cost rollup dashboards — 后续阶段

---

## 2. 架构

```
+------------------------------+
|  Call site (agent, qa, etc.) |
|    client.chat(request)      |
+--------------+---------------+
               |
               v
+------------------------------+
|       LLMClient (existing)   |
|  provider_resolver injected  |
|  retry / usage / metrics     |
|  (LLM_CALLS_TOTAL,           |
|   LLM_TOKENS_TOTAL)          |
+--------------+---------------+
               | resolver(request)
               v
+------------------------------+
|       LLMGateway (NEW)       |
|  providers: dict[str, Base]  |
|  default_provider_name: str  |
|  resolve(request) -> Base    |
|  with_config(p, m) -> pinned |
+--------------+---------------+
               |
               v
+------------------------------+
|  BaseProvider                |
|  (Anthropic, OpenAI,         |
|   Ollama, MiniMax)           |
+------------------------------+
```

**关键设计点**:`LLMClient` 完全不感知"多 provider"概念。它只看到一个 resolver 调用。后续 M4.B/C/D 都通过替换/包装 resolver 实现,LLMClient 内部逻辑零修改。

---

## 3. 文件改动

### 新建

- `apps/api/src/llm_client/gateway.py` — `LLMGateway` 类 + `UnknownModelError` 异常。
- `apps/api/src/llm_client/provider_registry.py` — `build_provider_registry(settings: Settings) -> dict[str, BaseProvider]` 工厂。
- `apps/api/src/llm_client/resolvers.py` — 三个 resolver 实现: `_PrefixResolver` (auto)、`_PinnedResolver` (with_config)、`_DefaultResolver` (兜底)。
- `apps/api/tests/llm_client/test_gateway.py` — 单元测试 (8 项)。
- `apps/api/tests/llm_client/test_provider_registry.py` — 单元测试 (3 项)。
- `apps/api/tests/llm_client/integration/test_gateway_e2e.py` — 集成测试 (4 项)。

### 修改

- `apps/api/src/llm_client/client.py`:
  - `LLMClient.__init__(provider_resolver=...)` 替换 `default_provider` 字段。
  - 删除 `with_config` 类方法(逻辑迁到 `LLMGateway.with_config`)。
  - `chat()` / `stream_chat()` 内所有 `self.default_provider.X` 改为 `self.provider_resolver(request).X`。
  - `aclose()` 改为遍历 `provider_resolver` 暴露的所有 provider 并关闭(通过新增 `gateway.aclose_all()` 协助)。
  - `_record_metrics` 增加 `route_mode` label(`auto` / `pinned` / `unknown_model` / `resolver_error`)。

- `apps/api/src/agent/llm_factory.py`:
  - `_default_llm_client_factory(tenant_id)` → 返回 `LLMGateway`。
  - 新增 `default_resolver(gateway) -> Callable[[ChatRequest], BaseProvider]` 辅助(返回 `gateway.resolve`,纯转发,便于调用点注入)。

- 5 个调用点迁移(每个仅改 1-2 行):
  - `apps/api/src/agent/simple_responder.py`
  - `apps/api/src/agent/suggest.py`
  - `apps/api/src/agent/graph/nodes.py`
  - `apps/api/src/qa/judge.py` (替换 `LLMClient.with_config` 调用)
  - `apps/api/src/history_mining/worker.py`

### 不改

- `BaseProvider` 抽象 — 现有所有 provider 实现不变。
- 异常类 — `RateLimited` / `InvalidRequest` / `ProviderUnavailable` / `OutputInvalid` 类型不变。
- `ChatRequest` / `ChatResponse` / `ChatMessage` — 不在 request 上加 `provider` 字段(保持 provider-agnostic)。

---

## 4. 数据流

### 默认流 (auto routing)

```
client.chat(request)                          # request.model = "claude-sonnet-4-5"
  └─> provider_resolver(request)
        └─> gateway.resolve(request)
              ├─ model 前缀 "claude-"  →  providers["anthropic"]
              ├─ model 前缀 "minimax-" →  providers["minimax"]
              ├─ model 前缀 "gpt-"/"o1-"/"o3-" → providers["openai"]
              └─ 其他                →  providers[default_provider_name]
  └─> resolver 返回的 provider.chat(request)
  └─> 用量记录 + 指标 (route_mode="auto")
```

### Per-call 覆盖 (pinned routing)

QA Judge 调用:
```
gateway = build_default_gateway()
pinned_resolver = gateway.with_config(provider="minimax", model="MiniMax-M3")
client = LLMClient(provider_resolver=pinned_resolver, tenant_id="qa-judge")
resp = await client.chat(ChatRequest(model="ignored", messages=[...]))
# 实际请求: MiniMax provider,model="MiniMax-M3",route_mode="pinned"
```

`with_config` 返回的 pinned resolver 是闭包:忽略 `request.model`,直接返回预绑定的 provider 对象。但为了 metric label 中 `model` 字段准确,`LLMClient.chat()` 入口处会用 `isinstance(resolver, _PinnedResolver)` 检测;若是,先 `request = request.model_copy(update={"model": resolver._pinned_model})` 再传给 provider。`_PinnedResolver` 是 `Protocol` 类(PEP 544 结构化子类),不依赖私有属性嗅探。

---

## 5. 错误处理

| 异常来源 | 现有处理 | M4.A 改动 |
|---|---|---|
| `RateLimited` (429) | 不重试,propagate,`outcome="rate_limited"` | 不变 + `route_mode="auto"` |
| `InvalidRequest` (4xx) | 不重试,propagate,`outcome="invalid_request"` | 不变 + `route_mode="auto"` |
| `ProviderUnavailable` (5xx/网络) | 重试 3 次,expo backoff + jitter,`outcome="unavailable"` | 不变 + `route_mode="auto"` |
| `OutputInvalid` (不可解析) | 重试 3 次,同 Unavailable 路径,`outcome="output_invalid"` | 不变 + `route_mode="auto"` |
| **新增** `UnknownModelError` | — | resolver 抛 → 当作 `InvalidRequest`,`outcome="invalid_request"`,`route_mode="unknown_model"`。不重试,propagate。 |
| **新增** Resolver 内部异常 (非 UnknownModelError) | — | resolver 抛非预期异常 → 当作 `ProviderUnavailable`,`outcome="resolver_error"`,`route_mode="resolver_error"`。不重试,propagate。 |
| `LLMClient.__init__(provider_resolver=None)` | — | `TypeError("provider_resolver is required")` 在构造时抛出。 |

**PII 纪律**:error 日志只携带 provider name + model + outcome + route_mode。绝不记录 message content、prompt 文本、异常 repr。

---

## 6. Provider Registry 构建

`build_provider_registry(settings: Settings) -> dict[str, BaseProvider]`:
1. 若 `settings.minimax_api_key` 非空 → 注册 `"minimax"` = `OpenAIProvider(api_key=settings.minimax_api_key, model=settings.minimax_model or "MiniMax-M3", base_url=settings.minimax_base_url or "https://api.minimaxi.com/v1")`。
2. 若 `settings.anthropic_api_key` 非空 → 注册 `"anthropic"` = `AnthropicProvider(api_key=settings.anthropic_api_key, model=settings.default_llm_model)`。
3. 若 `settings.openai_api_key` 非空 → 注册 `"openai"` = `OpenAIProvider(api_key=settings.openai_api_key, model=settings.openai_model or "gpt-4o-mini")`。
4. 若 registry 为空 → `RuntimeError("No LLM provider configured: set MINIMAX_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY")`。
5. `default_provider_name` = `settings.default_llm_provider` (env 覆盖);若未设置 = registry 首个 key (插入序,即 MiniMax → Anthropic → OpenAI)。

注:目前 `_default_llm_client_factory` 中 MiniMax key 检测 + Anthropic fallback 的内联逻辑会被本函数替代。

---

## 7. 指标扩展

既有 metric 不变:
- `lumen_llm_calls_total{provider, model, outcome}` — Stage 11.3 已发布
- `lumen_llm_tokens_total{provider, model, direction}` — Stage 11.3 已发布

**新增 label**:在两个 metric 上加 `route_mode`,枚举值固定 4 个(`auto` / `pinned` / `unknown_model` / `resolver_error`),基数 ~4x。既有 labels 估算:provider ~5 × model ~30 × outcome ~5 = ~750 时间序列;加 route_mode 后 ~3000。Prometheus 完全可承受。

> 兼容性:既有 dashboard / alert 规则已按 `{provider, model, outcome}` 聚合。新增 label 是**安全的维度补充**,不会破坏既有查询(它们只是少了 route_mode 维度,聚合时 sum 不变)。

---

## 8. 测试策略

### 单元 (`tests/llm_client/test_gateway.py`, `test_provider_registry.py`)

1. `test_build_provider_registry_picks_minimax_when_minimax_key_set`
2. `test_build_provider_registry_picks_anthropic_when_only_anthropic_key_set`
3. `test_build_provider_registry_raises_when_no_keys`
4. `test_resolver_routes_minimax_prefix`
5. `test_resolver_routes_claude_prefix`
6. `test_resolver_routes_openai_prefix`
7. `test_resolver_falls_back_to_default_for_unknown_prefix`
8. `test_with_config_returns_pinned_resolver_ignoring_request_model`
9. `test_pinned_resolver_overrides_request_model_for_metric_label`
10. `test_unknown_model_raises_UnknownModelError`
11. `test_llm_client_requires_provider_resolver`
12. `test_resolver_internal_exception_surfaces_as_resolver_error_metric`

### 集成 (`tests/llm_client/integration/test_gateway_e2e.py`)

13. `test_minimax_chat_succeeds_via_resolver` — httpx mock,`request.model="MiniMax-M3"` → MiniMax 路径
14. `test_anthropic_chat_succeeds_via_resolver` — httpx mock,Anthropic 路径
15. `test_qa_judge_pinned_path_routes_to_minimax` — `gateway.with_config(provider="minimax", model=...)` → mock 收到 MiniMax 路径的请求体
16. `test_default_routing_unknown_model_returns_invalid_request` — `request.model="mystery"` → 抛 `UnknownModelError`,`InvalidRequest` 路径

### 既有测试零回归

- `tests/agent/test_simple_responder.py`、`test_agent_graph.py`、`api/test_agent_suggest.py`
- `tests/qa/unit/test_worker.py`、`test_judge.py`
- `tests/llm_client/test_client.py`、`test_client_stream.py`
- `tests/llm_client/test_openai_provider.py`、`test_anthropic_provider.py`

`LLMClient` 构造签名变更 + `with_config` 删除,需同步调整上述测试中的 mock 桩。每个调整 ≤ 3 行。

### 覆盖率

`gateway.py` / `provider_registry.py` / `resolvers.py` ≥ 90% line coverage(文件小,分支全可达)。

---

## 9. 迁移顺序与 PR 节奏

为减小 review 噪音,合并到一个 PR(单 spec 单 PR),子任务用 commit 切分:

1. `feat(llm-client): add LLMGateway + provider registry` — 新建 gateway.py / provider_registry.py / resolvers.py + 单元测试
2. `refactor(llm-client): LLMClient accepts provider_resolver` — client.py 改造 + 既有 client 测试同步
3. `feat(llm-client): with_config migrated to gateway` — 替换 stub
4. `refactor(agents): migrate 5 call sites to gateway`
5. `test(llm-client): gateway e2e integration tests`
6. `docs: README update — gateway section`

单 PR 即可,reviewer 一次看完。Squash merge。

---

## 10. 已知风险

| 风险 | 缓解 |
|---|---|
| 5 个调用点迁移漏改导致测试失败 | 既有测试集 + conftest fixtures 自动覆盖每个调用点 |
| `route_mode` label 与既有 alert 规则冲突 | 新 label 是**补充**维度,既有聚合查询不受影响 |
| `with_config` 删除破坏外部调用方(若有) | 全项目 grep `LLMClient.with_config` — 仅 `qa/judge.py` 使用,迁移到 `gateway.with_config` |
| Provider 模型名前缀变更(如 minimax-2 改名) | `_PrefixResolver` 用 startswith 匹配,只需改 prefix 字符串 |
| `default_provider_name` 与 registry 第一个 key 不一致 | 强制 `default_provider_name ∈ registry.keys()`,不匹配则抛 `RuntimeError` |

---

## 11. 不做 (YAGNI)

| 不做项 | 理由 |
|---|---|
| 删除 `LLMClient` 类 | M4.A 目标是落地 router,不是清理;保留可让后续子项目(B/C/D)在不动 LLMClient 的前提下迭代 |
| 自动 fallback | M4.B 专项 |
| Per-tenant 配置 | M4.C 专项 |
| Token budget | M4.D 专项 |
| Cost rollup dashboard / USD 计价 | 后续 M5+ 阶段;现阶段 token 计数已足够诊断 |
| Prompt caching | 复杂且 provider 差异大;非 M4 范围 |
| 删除 inline provider wiring in `_default_llm_client_factory` | 本 spec 已经替换为 `build_provider_registry`,但保留 factory 入口,call site 不变 |

---

## 12. 完成判据

- [ ] `apps/api/src/llm_client/gateway.py`、`provider_registry.py`、`resolvers.py` 提交
- [ ] `LLMClient.__init__` 接受 `provider_resolver`,无 resolver 时 `TypeError`
- [ ] `LLMClient.with_config` 移除,`LLMGateway.with_config` 实现
- [ ] 5 个调用点全部迁移,既有测试零回归
- [ ] 12 单元测试 + 4 集成测试通过
- [ ] `route_mode` label 上线,既有 metric 查询兼容
- [ ] `tests/llm_client/test_*.py` 90%+ coverage
- [ ] README 状态表新增 M4.A 行(可后续 close-out 时一并加)
- [ ] commit 全部推 origin/main