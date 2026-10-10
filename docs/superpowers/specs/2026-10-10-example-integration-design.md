# Example Integration Project — Design

**Date**: 2026-10-10
**Status**: Draft (awaiting user review)
**Owner**: Lumen AI Support Agent monorepo (`d:\work-ai\0401-ai-customer`)
**Stage**: Standalone, no stage numbering — ships as `apps/example-integration/`

---

## 1. Purpose

Provide a self-contained TypeScript example project that demonstrates all three external integration surfaces of the Lumen AI Support Agent platform, using **fake / seed data** so a new contributor can stand up the full stack and exercise every integration path without writing any Lumen internals code.

Three integration surfaces covered:

1. **Web Widget embed** — drop the chat bubble into a third-party website.
2. **REST API client** — programmatically log in as an agent, list the queue, claim a conversation, post a reply, request an AI suggestion.
3. **Webhook delivery** — simulate an upstream channel (Feishu) posting an inbound message into `/api/v1/channel/feishu/webhook/{app_id}` with a valid SHA-256 signature.

The example is **not a mock**. It targets the real running Lumen backend. The "fake data" comes from the existing `tests/e2e/scripts/seed.py` plus a small extension script that adds the Feishu channel row that the seed does not currently create.

---

## 2. Architecture Overview

```
┌────────────────────────────────────────────────────────────────────┐
│  Example Integration Project (apps/example-integration/)           │
│                                                                    │
│  ┌──────────────────┐ ┌──────────────────┐ ┌────────────────────┐ │
│  │  widget-demo     │ │  api-client      │ │  webhook-sender    │ │
│  │  (Vite static)   │ │  (TS CLI scripts)│ │  (Fastify :4567)   │ │
│  │  port 5173       │ │  exits after run │ │  forwards to :8000 │ │
│  └────────┬─────────┘ └────────┬─────────┘ └─────────┬──────────┘ │
└───────────┼────────────────────┼─────────────────────┼────────────┘
            │                    │                     │
            │ POST /widget/token │ POST /auth/login    │ POST /channel/feishu/webhook/:app_id
            │ WS   /widget/ws    │ GET  /agents/queue  │   (with X-Lark-Signature)
            │                    │ POST /agents/claim  │
            │                    │ POST /conversations/.../messages
            ▼                    ▼                     ▼
┌────────────────────────────────────────────────────────────────────┐
│  Lumen API (docker compose up)                                     │
│  http://localhost:8000                                            │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌──────────────┐    │
│  │ widget/    │ │ auth/      │ │ agent/     │ │ channel/     │    │
│  │ api.py     │ │ api.py     │ │ api.py     │ │ feishu/      │    │
│  │ widget/ws/ │ │            │ │            │ │ webhook.py   │    │
│  └────────────┘ └────────────┘ └────────────┘ └──────────────┘    │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ Postgres (port 25432) │ Redis (26379) │ Qdrant (26333)      │   │
│  │ Seed: tests/e2e/scripts/seed.py + extension in this project │   │
│  └─────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────┘
```

All three demo entry points share the same backend process and the same seeded data — the demo is a **frontend** to Lumen, not a parallel implementation.

---

## 3. Components

### 3.1 Repository Layout

```
apps/example-integration/
├── package.json
├── tsconfig.json
├── vite.config.ts
├── README.md
├── .env.example
├── src/
│   ├── shared/
│   │   ├── config.ts
│   │   └── seed-extension.ts
│   ├── widget-demo/
│   │   ├── index.html
│   │   └── main.ts
│   ├── api-client/
│   │   ├── login.ts
│   │   ├── conversations.ts
│   │   ├── suggest.ts
│   │   └── cli.ts
│   └── webhook-sender/
│       ├── server.ts
│       ├── sign.ts
│       └── feishu-event.ts
├── scripts/
│   ├── bring-up.sh
│   ├── wait-ready.sh
│   └── extend-seed.ts
└── tests/
    ├── api-client.test.ts
    └── webhook-sender.test.ts
```

### 3.2 Widget Demo (`widget-demo/`)

A single static `index.html` that emulates a customer-facing landing page. Vite serves it on `localhost:5173` (already in `WIDGET_ALLOWED_ORIGINS_GLOBAL`). The page:

- Sets `window.LumenAICustomerConfig` with the seeded `tenant_id` and `channel_id` from `.env`.
- Loads the SDK's pre-built IIFE via `vite-plugin-static-copy`, which mirrors `apps/web-sdk/dist/lumen-widget.js` into `public/` so `<script src="/lumen-widget.js" async>` resolves at dev time. No SDK rebuild is required — the example reuses whatever the workspace produces.
- Clicking the chat bubble triggers the SDK's normal flow: token mint → WS connect → AI reply with RAG retrieval against the seeded KB articles.

**Configuration constants** (from `apps/api/src/widget/tokens.py` + `apps/web-sdk/src/config.ts`):
- `apiBaseUrl` — `http://localhost:8000`
- `channelId` — `01HZDEMO00000000000000003` (from `tests/e2e/scripts/seed.py`)
- `tenantId` — `01HZDEMO00000000000000000`
- `externalUserId` — generated UUID, persisted in `localStorage` by the SDK

**Contract enforced by backend** (confirmed in `apps/api/src/widget/api.py:55-75`):
- `POST /api/v1/widget/token` body `{channel_id, external_user_id}` → `{token, expires_at, expires_in}`
- Channel must exist and be `ACTIVE`, else 404 / 403.
- JWT payload: `{sub, channel_id, tenant_id, typ:"widget", iat, exp}`, TTL 30 min (`WIDGET_TOKEN_TTL_SECONDS`).
- WS handshake reads `?token=…`, verifies origin against allowlist, decodes token, checks `channel.status == ACTIVE` and `channel.tenant_id == token.tenant_id`. Failure → `WS 1008 POLICY_VIOLATION`.

### 3.3 REST API Client (`api-client/`)

A thin command-line wrapper around `fetch`. Each command is a one-shot process — no daemon, no state — so it doubles as documentation of the protocol.

**Subcommands** (driven by `src/api-client/cli.ts`):

| Subcommand | HTTP call | Notes |
|---|---|---|
| `login` | `POST /api/v1/auth/login` `{email, password}` | Writes JWT to `.api-token-cache.json` (gitignored). |
| `queue` | `GET /api/v1/agents/queue` `Authorization: Bearer …` | Lists PENDING conversations. |
| `claim <conv_id>` | `POST /api/v1/agents/claim` `{conversation_id}` | Takes ownership. |
| `reply <conv_id> <text>` | `POST /api/v1/conversations/{id}/messages` `{content_text}` | Posts agent message. |
| `suggest <conv_id>` | `POST /api/v1/agents/suggest-reply` `{conversation_id}` | Returns AI-suggested reply or `{detail: "no_customer_message"}`. |

**Demo credentials** (from `tests/e2e/scripts/seed.py`):
- `agent@example.com` / `Demo123!` → role `AGENT`
- `admin@example.com` / `Demo123!` → role `ADMIN`

### 3.4 Webhook Sender (`webhook-sender/`)

A local Fastify server on `:4567` that accepts a single `POST /simulate-feishu` call and forwards a forged Feishu message event to the Lumen backend with a valid signature.

**Signature scheme** (from `apps/api/src/channel/feishu/signature.py:47-61`):
```
signature = SHA-256(timestamp + nonce + encrypt_key + body).hex()
```
where `encrypt_key` is the literal string `"M1_STUB_ENCRYPT_KEY_REPLACE_IN_TASK_4_13"` (the M1 stub constant at `apps/api/src/channel/feishu/webhook.py:31`). This is a known limitation — M1 does not look up per-channel keys. The example project mirrors this stub and documents the limitation in its README.

**Event payload shape** (must satisfy `FeishuAdapter.parse_inbound`):
- Wrapped in the Feishu event envelope (`header.event_type == "im.message.receive_v1"`).
- Sender `sender_id.open_id` and `chat_id` are opaque strings; the backend does not validate them against Feishu's API.
- `message.message_type == "text"`, `message.content.text` carries the message body.
- A complete fixture lives in `src/webhook-sender/feishu-event.ts` so the demo is self-contained.

**Feishu webhook contract** (from `apps/api/src/channel/feishu/webhook.py:40-137`):
- `POST /api/v1/channel/feishu/webhook/{app_id}` requires headers `X-Lark-Request-Timestamp`, `X-Lark-Request-Nonce`, `X-Lark-Signature`.
- Backend rejects stale timestamp (>5 min) → 401, bad signature → 401, malformed JSON → 400, unknown `app_id` → 404.
- On success the backend persists the inbound envelope via `process_inbound_envelope`, which creates/updates a conversation and (when `ai_handling == true`) auto-generates an AI reply using the LangGraph tool loop.

### 3.5 Seed Extension (`shared/seed-extension.ts`)

The existing `tests/e2e/scripts/seed.py` only seeds a `WEB` channel. To exercise the webhook path, the example project needs a `FEISHU` channel with `app_id = "demo-feishu-app-001"` and an empty `config_json` (signature uses the stub key regardless). It also seeds one KB article so the widget demo and the webhook-triggered AI reply both have something to retrieve.

The extension reuses the same DB session pattern as `seed.py`: it imports from `apps/api/src` by manipulating `sys.path` and uses `core.database.get_session()`. It runs **once** after `seed.py`, not as part of every demo boot. It seeds three things:

1. A `FEISHU` channel with `app_id = "demo-feishu-app-001"` and an empty `config_json` (signature uses the M1 stub key regardless).
2. A knowledge base `demo-kb` belonging to the demo tenant.
3. Two `Article` rows (`How do I reset my password?` / `What is the refund policy?`) so the widget demo and the webhook-triggered AI reply both have real content to retrieve via RAG.

**Why not amend `seed.py`?** Because that script is shared with the Playwright E2E suite (`tests/e2e/global-setup.ts`). Adding Feishu plumbing to it would couple unrelated tests. The extension lives entirely inside `apps/example-integration/` and only touches data this project needs.

---

## 4. Data Flow

### 4.1 Widget Embed

The widget WS only ACKs inbound messages; AI replies are delivered via REST polling by the chat UI. This matches `apps/api/src/widget/ws/router.py:155-184`, which handles `message` frames by persisting them and returning an `ack`, with no broadcast path defined in M1.

```
Browser (localhost:5173/index.html)
  │
  ├─ POST /api/v1/widget/token
  │    { channel_id: "01HZ..003", external_user_id: "demo-visitor-…" }
  │  ← 200 { token: "<jwt>", expires_in: 1800, expires_at: "…" }
  │
  ├─ WebSocket Upgrade /api/v1/widget/ws?token=<jwt>
  │    Origin: http://localhost:5173  (in WIDGET_ALLOWED_ORIGINS_GLOBAL ✓)
  │    Backend: token decode ✓, channel ACTIVE ✓, tenant match ✓
  │
  ├─ WS frame { "type": "message",
  │             "text": "How do I reset my password?",
  │             "external_message_id": "<ulid>" }
  │  ← WS frame { "type": "ack", "external_message_id": "<ulid>" }
  │
  │   ... LangGraph (async, in worker): retrieve_node → llm_node → RAG ...
  │
  ├─ Poll loop (chat UI, every ~1s while waiting):
  │   GET /api/v1/conversations/{id}/messages  Authorization: Bearer <widget_jwt>
  │   ← [ { role: "assistant", content_text: "To reset your password…", … } ]
  │
  │ When the assistant message appears, the chat UI renders it.
```

The example project does not change this protocol. If a future stage introduces streaming back over the widget WS, the demo will pick it up transparently — the SDK update will be picked up by the static-copy step.

### 4.2 REST API

```
$ pnpm api-client:queue
  POST /api/v1/auth/login  { email: "agent@example.com", password: "Demo123!" }
  ← { access_token: "<jwt>", … }
  GET /api/v1/agents/queue  Authorization: Bearer <jwt>
  ← [ { conversation_id: "01HZ…004", … }, … ]

$ pnpm api-client:reply 01HZ…004 "I can help with that."
  POST /api/v1/agents/claim          { conversation_id: "01HZ…004" }
  POST /api/v1/conversations/01HZ…004/messages  { content_text: "…" }
  ← { message_id: "<ulid>", created_at: "…" }

$ pnpm api-client:suggest 01HZ…004
  POST /api/v1/agents/suggest-reply { conversation_id: "01HZ…004" }
  ← { suggestion: "First, open the password reset page…" }
```

### 4.3 Webhook

```
$ pnpm webhook:start        # Fastify listens on :4567
$ pnpm webhook:simulate
  POST localhost:4567/simulate-feishu
    → server.ts: build Feishu message event JSON
                  compute ts = floor(now / 1000), nonce = randomUUID()
                  body_utf8 = event JSON
                  signature = SHA-256(ts + nonce + STUB_KEY + body_utf8).hex()
    POST http://localhost:8000/api/v1/channel/feishu/webhook/demo-feishu-app-001
         X-Lark-Request-Timestamp: ts
         X-Lark-Request-Nonce:     nonce
         X-Lark-Signature:         signature
         Content-Type:             application/json
         Body:                     <event JSON>
    ← 200 { ok: true }
    ... backend: FeishuAdapter.parse_inbound → process_inbound_envelope
        → Conversation created, Message persisted, AI auto-reply generated

$ pnpm api-client:queue   # verify the inbound conversation shows up
```

---

## 5. Error Handling

| Scenario | Detection point | Demo behavior |
|---|---|---|
| Backend not running | `fetch` throws `ECONNREFUSED` | Demo process exits with code 2 and prints `Lumen API unreachable at http://localhost:8000 — run \`cd deploy && docker compose up\`` |
| Widget JWT expired (>30 min) | SDK triggers `POST /widget/token/refresh` automatically | Demo does not need to handle — refresh is the SDK's responsibility |
| Feishu signature mismatch | Backend returns 401 (`invalid signature`) | Demo prints the expected signature alongside the request body for debugging |
| Feishu `app_id` not seeded | Backend returns 404 (`unknown app_id …`) | Demo README explains `pnpm seed` step is mandatory before webhook demo |
| Widget origin not allowed | WS handshake closes with `1008` | Demo README warns that custom ports must be added to `WIDGET_ALLOWED_ORIGINS_GLOBAL` in `deploy/docker-compose.yml` |
| M4.D budget exceeded | Backend returns 429 `{"error":"budget_rate_limited", …}` | Demo surfaces the structured body verbatim — this is a real production signal worth showing |
| Channel INACTIVE | `/widget/token` returns 403 (`channel is not active`) | Seed script always sets `status=ACTIVE`; demo README points to `ChannelStatus` enum if a user wants to flip it |

All error paths log the `X-Request-ID` header from the response (Lumen emits it on every HTTP response — see `apps/api/src/main.py:175`) so users can `grep` it in the API logs for a full end-to-end trace.

---

## 6. Testing

| Surface | Test layer | What is verified |
|---|---|---|
| Widget | Optional Playwright smoke (not run in CI by default) | `localhost:5173` loads, chat bubble renders, clicking opens a panel — visual confirmation |
| REST API client | Vitest unit, `tests/api-client.test.ts` | Mocks `fetch`, asserts request path + body shape for login / queue / claim / reply / suggest |
| Webhook sender | Vitest unit + integration, `tests/webhook-sender.test.ts` | Unit: signature helper produces the digest Lumen expects. Integration: with the real backend running, `POST /simulate-feishu` returns 200 and a subsequent `GET /conversations` shows the inbound message |

Test matrix is deliberately minimal — the example project is documentation, not a production codebase. CI integration is **out of scope** for this design.

---

## 7. Dependencies

**Runtime**: Node.js 20+, pnpm.

**npm packages**:
- `fastify` — webhook sender's local HTTP server.
- `tsx` — TypeScript execution for the CLI scripts.
- `vite` — dev server for the widget demo.
- `vite-plugin-static-copy` — mirrors `apps/web-sdk/dist/lumen-widget.js` into `public/` so the static `<script>` tag works.
- `vitest` — test runner.
- `typescript` — type-check.

The SDK is **not** imported as a workspace package. It is consumed as a pre-built IIFE script (consistent with how real customer sites integrate — `<script src="…/lumen-widget.js">`), and the static-copy plugin handles the dev-server resolution. The example project depends on `apps/web-sdk` having been built at least once; a `pnpm -F @lumen/web-sdk build` step is part of `bring-up.sh`.

**No new Python dependencies** — the example project talks to Lumen only over HTTP / WebSocket. The seed extension imports from `apps/api/src` via `sys.path` injection, same trick `seed.py` already uses.

---

## 8. Bring-up Sequence

```bash
# one-time
cd apps/example-integration && pnpm install

# every demo session
cd deploy && docker compose up -d                    # API + 6 deps
./scripts/wait-ready.sh                              # polls /health/ready until 200
cd ../apps/api && uv run python ../../tests/e2e/scripts/seed.py
cd ../example-integration && pnpm seed               # FEISHU channel + KB stub

# widget
pnpm widget                                          # http://localhost:5173

# REST API (separate terminal)
pnpm api-client:login
pnpm api-client:queue
pnpm api-client:reply 01HZDEMO00000000000000004 "Got it."

# webhook (separate terminal)
pnpm webhook:start                                   # :4567
pnpm webhook:simulate                                # one-shot POST
pnpm api-client:queue                                # confirm new inbound conversation
```

The README will document this sequence verbatim with screenshots of expected output.

---

## 9. Out of Scope

- **Production hardening** — the example must not be deployed as-is. The README will carry a banner warning.
- **Real Feishu app registration** — the demo uses the M1 stub signature key.
- **Real SES / email inbound** — `POST /api/v1/email/inbound` requires AWS credentials and is documented in README as "see `apps/api/src/main.py:278` for the endpoint contract".
- **Admin SPA integration** — out of scope; the example exercises API + Widget + Webhook, not the agent SPA login flow.
- **CI / CD** — the example is local-only.
- **Multilingual SDK strings** — the demo hardcodes zh-CN titles.

---

## 10. Known Tech Debt Acknowledged

1. **M1 stub Feishu encrypt key** (`apps/api/src/channel/feishu/webhook.py:31`) — the example propagates this stub. When Stage 4.13 ships per-tenant key lookup, the example needs to read `Channel.config_json["encrypt_key"]` instead. Documented in webhook-sender README.
2. **Email inbound requires AWS** — out of scope per §9; if a future iteration adds it, AWS credentials will be needed in `.env`.
3. **No automated end-to-end test for the widget** — Playwright integration deferred (would require pulling browser binaries into the monorepo).
