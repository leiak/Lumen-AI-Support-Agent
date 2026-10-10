# Lumen Example Integration

Three demo entry points for the [Lumen AI Support Agent](../../) — a Web
Widget embed, a REST API client, and a Feishu webhook sender — all in one
Node + TypeScript workspace package, all talking to the real Lumen backend
with seeded fake data.

> ⚠️ **Not for production.** This example reuses M1 stubs (see
> [Known Limitations](#known-limitations)). It is intended for local demos,
> tutorials, and onboarding new contributors.

## Prerequisites

- Node.js 20+
- pnpm 9+
- Docker + Docker Compose
- The repo cloned with `apps/web-sdk` already built (`pnpm -F @lumen/web-sdk build`)

## Quickstart

```bash
cd apps/example-integration
cp .env.example .env
./scripts/bring-up.sh        # docker compose up + seed + extend-seed
```

Then in three separate terminals:

```bash
# Terminal 1 — widget
pnpm widget
# open http://localhost:5173 in your browser; chat bubble appears bottom-right

# Terminal 2 — REST API
pnpm api-client:login
pnpm api-client:queue
pnpm api-client:reply 01HZDEMO00000000000000004 "I can help with that."
pnpm api-client:suggest 01HZDEMO00000000000000004

# Terminal 3 — webhook
pnpm webhook:start
# in another terminal: curl -X POST localhost:4567/simulate-feishu
# OR: pnpm webhook:simulate
```

## Demo walkthroughs

### Widget (`pnpm widget`)

Open <http://localhost:5173>. The chat bubble loads via the SDK's IIFE
script (`lumen-widget.js` mirrored from `apps/web-sdk/dist/`). Try:

> "How do I reset my password?"

The backend's LangGraph agent retrieves the seeded KB article and returns
a streamed reply (the SDK polls `GET /conversations/{id}/messages`).

### REST API

```bash
pnpm api-client:login      # caches agent JWT
pnpm api-client:queue      # lists PENDING conversations
pnpm api-client:claim 01HZDEMO00000000000000004
pnpm api-client:reply 01HZDEMO00000000000000004 "Got it, working on it."
pnpm api-client:suggest 01HZDEMO00000000000000004
```

The token cache is `apps/example-integration/.api-token-cache.json`
(gitignored). Delete it to force re-login.

### Webhook

```bash
pnpm webhook:start                                   # Fastify :4567
curl -X POST localhost:4567/simulate-feishu          # default text
# or with a custom message:
curl -X POST localhost:4567/simulate-feishu -H 'Content-Type: application/json' \
  -d '{"text":"What is the refund policy?"}'
```

The local server computes a Feishu SHA-256 signature using the M1 stub
encrypt key and forwards the event to
`http://localhost:8000/api/v1/channel/feishu/webhook/demo-feishu-app-001`.
The backend creates a new conversation and (when AI handling is enabled)
auto-generates a reply.

## Architecture

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
                          Postgres + Redis + Qdrant (seeded by tests/e2e/scripts/seed.py + scripts/extend-seed.ts)
```

See [`docs/superpowers/specs/2026-10-10-example-integration-design.md`](../../docs/superpowers/specs/2026-10-10-example-integration-design.md)
for the full design.

## Known limitations

1. **M1 stub Feishu encrypt key.** The webhook sender uses the literal
   string `M1_STUB_ENCRYPT_KEY_REPLACE_IN_TASK_4_13` because M1 does not
   look up per-tenant keys (see `apps/api/src/channel/feishu/webhook.py:31`).
   When Stage 4.13 ships per-tenant lookup, replace `FEISHU_STUB_ENCRYPT_KEY`
   in `src/webhook-sender/server.ts` with a `Channel.config_json["encrypt_key"]`
   read.
2. **No email inbound demo.** `POST /api/v1/email/inbound` requires AWS SES
   credentials. Out of scope for this example.
3. **No Playwright CI for the widget.** Manual smoke only.

## Tests

```bash
pnpm test                                              # unit tests (no backend needed)
LUMEN_E2E=1 pnpm test webhook-sender.test.ts          # integration (backend must be up)
```
