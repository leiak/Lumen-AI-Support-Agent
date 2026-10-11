# Lumen 示例集成

[Lumen AI 客服 Agent](../../) 的三个演示入口 —— Web Widget 嵌入、REST API 客户端和飞书 webhook 发送器 —— 全部集中在一个 Node + TypeScript workspace 包中,均与真实的 Lumen 后端通信,并使用预置的假数据。

> ⚠️ **请勿用于生产环境。** 本示例复用了 M1 阶段的 stub(参见 [已知限制](#已知限制))。仅用于本地演示、教程以及新贡献者入门。

## 前置条件

- Node.js 20+
- pnpm 9+
- Docker + Docker Compose
- 已克隆本仓库,并且 `apps/web-sdk` 已经构建完成(`pnpm -F @lumen/web-sdk build`)

## 快速开始

```bash
cd apps/example-integration
cp .env.example .env
./scripts/bring-up.sh        # docker compose up + seed + extend-seed
```

然后在三个独立的终端中分别执行:

```bash
# 终端 1 — widget
pnpm widget
# 在浏览器中打开 http://localhost:5173;聊天气泡会出现在右下角

# 终端 2 — REST API
pnpm api-client:login
pnpm api-client:queue
pnpm api-client:reply 01HZDEMO00000000000000004 "I can help with that."
pnpm api-client:suggest 01HZDEMO00000000000000004

# 终端 3 — webhook
pnpm webhook:start
# 在另一个终端中:curl -X POST localhost:4567/simulate-feishu
# 或者:pnpm webhook:simulate
```

## 演示演练

### Widget(`pnpm widget`)

打开 <http://localhost:5173>。聊天气泡通过 SDK 的 IIFE 脚本(`lumen-widget.js`,从 `apps/web-sdk/dist/` 镜像)加载。可以尝试输入:

> "How do I reset my password?"

后端的 LangGraph agent 会检索预置的 KB 文章,并以流式方式返回回复(SDK 通过轮询 `GET /conversations/{id}/messages` 获取)。

### REST API

```bash
pnpm api-client:login      # 缓存 agent JWT
pnpm api-client:queue      # 列出 PENDING 状态的会话
pnpm api-client:claim 01HZDEMO00000000000000004
pnpm api-client:reply 01HZDEMO00000000000000004 "Got it, working on it."
pnpm api-client:suggest 01HZDEMO00000000000000004
```

token 缓存位于 `apps/example-integration/.api-token-cache.json`(已在 `.gitignore` 中)。删除该文件即可强制重新登录。

### Webhook

```bash
pnpm webhook:start                                   # Fastify :4567
curl -X POST localhost:4567/simulate-feishu          # 默认文本
# 或者自定义消息:
curl -X POST localhost:4567/simulate-feishu -H 'Content-Type: application/json' \
  -d '{"text":"What is the refund policy?"}'
```

本地服务器使用 M1 stub 加密密钥计算飞书 SHA-256 签名,然后将事件转发到
`http://localhost:8000/api/v1/channel/feishu/webhook/demo-feishu-app-001`。
后端会创建一条新会话,并在启用 AI 处理时自动生成回复。

## 架构

```
Browser (5173)                CLI                          Fastify (4567)
  Widget SDK      →     api-client/cli.ts           →   webhook-sender/server.ts
  POST /widget/token      POST /auth/login                POST /channel/feishu/webhook/:app_id
  WS   /widget/ws         GET  /agents/queue              (X-Lark-Signature)
                          POST /agents/conversations/:id/claim
                          POST /agents/conversations/:id/suggest-reply
                          POST /conversations/:id/messages
                                  ↓
                          Lumen API at :8000
                                  ↓
                          Postgres + Redis + Qdrant (由 tests/e2e/scripts/seed.py + scripts/extend-seed.ts 预置)
```

完整设计文档请参见 [`docs/superpowers/specs/2026-10-10-example-integration-design.md`](../../docs/superpowers/specs/2026-10-10-example-integration-design.md)。

## 已知限制

1. **M1 stub 飞书加密密钥。** webhook 发送器直接使用字符串常量 `M1_STUB_ENCRYPT_KEY_REPLACE_IN_TASK_4_13`,因为 M1 阶段并未实现按租户查询密钥(参见 `apps/api/src/channel/feishu/webhook.py:31`)。当 Stage 4.13 上线按租户查询能力后,需要将 `src/webhook-sender/server.ts` 中的 `FEISHU_STUB_ENCRYPT_KEY` 替换为从 `Channel.config_json["encrypt_key"]` 读取的值。
2. **暂无邮件入站 demo。** `POST /api/v1/email/inbound` 需要 AWS SES 凭据,不在本示例范围内。
3. **widget 未接入 Playwright CI。** 仅支持手动冒烟测试。

## 测试

```bash
pnpm test                                              # 单元测试(无需后端)
LUMEN_E2E=1 pnpm test webhook-sender.test.ts          # 集成测试(需要后端运行)
```