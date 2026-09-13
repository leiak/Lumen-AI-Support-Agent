# M1 End-to-End Demo Script

> **Audience**: Stakeholders, new engineers, sales engineers
> **Duration**: 12-15 minutes
> **Pre-reqs**: see "Environment setup" below

This script walks a presenter through the entire Lumen AI Support Agent M1
flow in three acts:

1. A customer reaches out via the embedded **Web Widget**.
2. The on-duty **agent** claims the conversation, reads the AI suggestion,
   and replies.
3. The **admin** adds a knowledge-base article that the AI will cite next
   time around.

All URLs, credentials, and commands below are verified against the
codebase (September 2026). If something drifts after Stage 10+ lands,
update this doc — do not silently let it rot.

## Environment setup

The demo runs locally against the same stack the Playwright E2E suite
uses (`tests/e2e/README.md`). Bring everything up before you start.

```bash
# 1. Clone + monorepo install
git clone git@github.com:leiak/Lumen-AI-Support-Agent.git
cd Lumen-AI-Support-Agent
pnpm install

# 2. Infrastructure (Postgres + Redis + Qdrant)
docker compose -f deploy/docker-compose.yml up -d postgres redis qdrant

# 3. Backend deps + migrations + .env
cd apps/api
uv sync
cp .env.example .env
# Edit .env — at minimum set:
#   ANTHROPIC_API_KEY=sk-ant-...
#   WIDGET_ALLOWED_ORIGINS_GLOBAL=http://localhost:5173,http://localhost:3000,http://localhost:8080
uv run alembic upgrade head

# 4. Seed deterministic demo tenant + agent + admin + channel + 1 PENDING
#    conversation (idempotent — re-run any time):
uv run python ../../tests/e2e/scripts/seed.py

# 5. Build the widget SDK once (the static host serves the bundle):
cd ../../apps/web-sdk
pnpm install
pnpm build      # produces dist/lumen-widget.js

# 6. Start the three web servers (separate terminals):
cd ../api && uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
cd ../web && pnpm dev                                    # Vite on :5173
cd ../..  && python -m http.server 8080 --directory tests/e2e/static
```

Sanity check before the demo: open
[`http://localhost:8000/health`](http://localhost:8000/health) and expect
`{"status":"ok"}`.

## Demo credentials

Seeded by `tests/e2e/scripts/seed.py`. **Treat as ephemeral test data —
never use in production.**

| Role  | Email             | Password   |
| ----- | ----------------- | ---------- |
| agent | `agent@demo.test` | `Demo123!` |
| admin | `admin@demo.test` | `Demo123!` |

Tenant: `Acme Demo` (`01HZDEMO00000000000000000`).
Web channel id: `01HZDEMO00000000000000003`.
Seeded conversation id: `01HZDEMO00000000000000004` (status: `pending`,
unassigned).

---

## Act 1: Customer asks via Web Widget (3 minutes)

**Story**: Sarah is a customer of "Acme SaaS". She visits the marketing
site and wants to know how to reset her password.

**Steps**

1. Open `http://localhost:8080/widget-host.html` in a fresh browser
   tab. The page embeds the built `lumen-widget.js` from the static
   host — same origin, so the iframe `srcdoc` + `postMessage`
   handshake stays intact.
2. Click the floating chat bubble in the bottom-right corner. The
   `LumenAICustomer` global opens an iframe chat window.
3. Type `How do I reset my password?` and press **Send**.
4. An AI bubble appears within ~3 seconds. The text is grounded in
   Acme's KB article "Password reset" — point at the answer and
   mention that RAG retrieval filters on `tenant_id` + KB id in
   Qdrant (three-layer isolation).
5. Click the **需要人工** button at the bottom of the composer. The
   widget sends the literal `[需要人工服务]` text over WS, flips to
   the "已转人工,客服稍后会联系您" hint banner, and on the backend
   the LangGraph agent's `escalate_to_human` tool moves the
   conversation into the PENDING unassigned queue.

**What to point out**

- Single-file SDK (`dist/lumen-widget.js`, < 50 KB IIFE). No React,
  no framework runtime on the host site — just one `<script>` tag.
- WebSocket transport (no polling). The customer gets sub-second
  acks + AI turns.
- RAG actually retrieves the KB article — open the dev tools
  Network tab and point out the WS frames carrying `message.complete`
  with the cited article id.
- `[需要人工服务]` is the wire-level escalation trigger consumed by
  the `EscalationService` and `escalate_to_human` LangGraph tool.
  Tenant context flows through a `ContextVar` so the tool can never
  cross tenants.

## Act 2: Agent claims and replies (5 minutes)

**Story**: Mike is the on-duty agent at Acme. He sees Sarah's
escalation land in the queue.

**Steps**

1. Open [`http://localhost:5173/login`](http://localhost:5173/login)
   and sign in as `agent@demo.test` / `Demo123!`.
   - The form runs a debounced `POST /api/v1/auth/lookup-tenant`
     300 ms after the email field stops changing. A successful
     lookup sets an internal `tenantId` that the subsequent
     `POST /auth/login` sends as `X-Tenant-Id`. This is the
     anti-enumeration defense — failed lookups and bad logins
     return the same generic 401.
2. After login you land on `/inbox`. Use the **status** filter
   to narrow to `pending` (default is `open`). The seeded
   conversation from Act 1 sits there with the customer's text.
3. Click into the row to open `/inbox/{id}` (the **InboxDetailPage**).
   The header shows the customer external id, the assigned agent
   (empty), and the **认领** (Claim) button.
4. Click **认领**. The button fires
   `POST /api/v1/agents/conversations/{id}/claim` (atomic
   `SELECT FOR UPDATE` + status + `assigned_agent_id` check on the
   backend). The row re-renders with `status: pending`,
   `assigned_agent_id: <you>`.
5. Scroll the **AI 建议回复** pane on the right and click **拿 AI
   建议**. Within a few seconds the pane shows a suggested reply
   plus a citation chip with the article id.
6. Click **应用到输入框** — the suggested text fills the composer
   (no auto-send; agents always have the final word).
7. Add a one-line personal touch (e.g. "Hi Sarah,") and click
   **Send**. The composer POSTs to
   `/api/v1/conversations/{id}/messages`. The customer receives
   the turn via the still-open WS on `:8000`.

**What to point out**

- 2-step login (email → tenant hint → password) — the same generic
  error is shown for "user does not exist", "wrong tenant", and
  "wrong password".
- WS messages stream into the page in real time via
  `useConversationWebSocket` (no polling).
- AI suggestion is **READ-ONLY**. The `suggest-reply` endpoint
  returns `suggested_text + citations + turn_kind` and explicitly
  **drops `tool_calls`** — so the agent cannot accidentally trigger
  a second escalation from a suggestion.
- Citations link back to the KB article so the agent can verify
  the source before sending.

## Act 3: Admin manages knowledge base (4 minutes)

**Story**: Lin is the Acme admin. A new product feature just
shipped and the docs need updating.

**Steps**

1. Sign out (top-right user menu) and log back in as
   `admin@demo.test` / `Demo123!`. Land on `/inbox`, then navigate
   to `/kb`.
2. Click **新建知识库**, name it `Acme Product Docs`, and create.
4. Click the new KB to open `/kb/{kbId}`.
3. Click **上传文档**, pick `docs/sample-docs/new-feature.pdf`
   (a 2 KB PDF shipped with this script), title it
   "Acme New Feature Guide", and submit.
4. The new article row appears immediately with status `draft`.
   Watch it transition: `draft → indexing → indexed` over the
   next few seconds as the background worker parses, chunks,
   embeds, and upserts into Qdrant.
5. Click the article to open
   `/kb/{kbId}/articles/{articleId}` and confirm the **chunks
   count** is non-zero and the status badge reads **indexed**.
6. Navigate to `/settings`. The **渠道 (Channels)** card lists the
   seeded Web channel (`demo-web`, type `web`, status `active`).
   The card is read-only in M1 — channel creation/deletion is
   server-side admin work today.

**What to point out**

- Multi-tenant isolation: Lin only sees Acme's KBs and channels
  — other tenants' rows never appear (DB WHERE-clause + Qdrant
  MUST filter + cross-tenant access returns 404, anti-enumeration).
- Async indexing: upload returns immediately; the worker drains
  the parse → chunk → embed → upsert pipeline in the background.
- Soft-delete for channels: `DELETE /api/v1/channels/{id}` flips
  status to `disabled` (no row removal) — preserves audit trail
  and avoids accidental data loss.
- Document parsing: PDF text + tables, with code blocks preserved
  as their own chunk type. Stage 6's multimodal enhancer treats
  each block kind separately so code chunks don't bleed into
  prose.

## Closing (1 minute)

Recap of M1 capabilities:

- **Multi-channel inbound** — Web Widget (WS), Feishu, Email,
  generic HTTP. One `process_inbound_envelope` entry point.
- **RAG-powered AI** — LangChain + LangGraph, `StateGraph(AgentState)`
  flows `START → retrieve → llm → (escalate | END)`.
- **Escalation to humans** — `escalate_to_human` tool with
  ContextVar-bound tenant context; full conversation history
  hands off to the agent workspace.
- **Agent workspace** — JWT-authenticated SPA with real-time WS
  message stream + READ-ONLY AI suggestion.
- **Multi-tenant isolation** — three-layer defense (DB row filter,
  Qdrant MUST filter, anti-enumeration 404).
- **PII discipline** — `core.logging.get_logger` (structlog),
  opaque IDs only, no customer message text in logs.

## Demo data reset

Between demo runs, wipe the seeded rows (the seed script is
intentionally non-destructive — see
`tests/e2e/README.md` § Re-seeding):

```bash
# From the repo root, with DATABASE_URL exported.
psql "$DATABASE_URL" <<'SQL'
DELETE FROM messages       WHERE conversation_id = '01HZDEMO00000000000000004';
DELETE FROM conversations  WHERE id              = '01HZDEMO00000000000000004';
DELETE FROM channels       WHERE id              = '01HZDEMO00000000000000003';
DELETE FROM users          WHERE id IN ('01HZDEMO00000000000000001','01HZDEMO00000000000000002');
DELETE FROM tenants        WHERE id              = '01HZDEMO00000000000000000';
SQL

# Re-seed (idempotent — safe to run any number of times).
cd apps/api && uv run python ../../tests/e2e/scripts/seed.py
```

If you only want a clean conversation state (tenant/users/channel
untouched), delete the seeded conversation + messages only. The
seed will recreate them on next run.

## Troubleshooting

These are the failure modes surfaced during Stages 9.10 / 9.11.

- **"Widget doesn't open"** — confirm `WIDGET_ALLOWED_ORIGINS_GLOBAL`
  in `apps/api/.env` includes the host origin (default dev host is
  `http://localhost:8080`). The `/api/v1/widget/token` call is the
  one that 403s on an unlisted origin. Restart the API after editing
  `.env`.
- **"AI doesn't reply"** — check `ANTHROPIC_API_KEY` is set. With
  no key the agent falls back to `FALLBACK_MESSAGE` and surfaces
  `turn_kind="llm_unavailable"` in the suggestion pane. RAG
  retrieval itself still functions (you'll see citations even when
  the LLM is down).
- **"WS disconnects"** — check Redis is reachable (`redis-cli ping`).
  The widget SDK reconnects automatically within ~30 s; the agent
  workspace reconnects on focus / page visibility. No data is lost:
  inbound messages are persisted before the broadcast.
- **"Login fails for `agent@demo.test`"** — run the seed:
  `cd apps/api && uv run python ../../tests/e2e/scripts/seed.py`.
  The seed is idempotent; safe to re-run. If the user truly is
  missing, `psql "$DATABASE_URL" -c "\\d users"` and confirm the
  row exists with `is_active = true`.
- **"PDF upload stays in `draft` / `indexing` forever"** — Qdrant
  must be up at `QDRANT_URL` (default `http://localhost:6333`).
  Check the API logs for a worker exception; the article row will
  eventually flip to `failed` with an `error_message` populated.
- **"AI suggestion pane shows the fallback warning"** — this is
  the expected path when LLM/Qdrant are unavailable. See
  `tests/e2e/README.md` § Known M1 limitations item 2.

## Recording tips

- Two browser profiles side by side: customer on `:8080`, agent on
  `:5173`. Drag them to opposite halves of the screen so the WS
  round-trip is visible at the same time.
- Keep the Network panel open during Act 1 — the WS frames
  (`message.created`, `message.complete`) are the best visual proof
  of the real-time path.
- For Act 3, refresh `/kb/{kbId}` once after upload so the audience
  sees the `draft → indexing → indexed` transition.

---

_Last verified against:_ `apps/web/src/App.tsx`, `tests/e2e/scripts/seed.py`,
`apps/web-sdk/src/iframe/chat.ts`, `deploy/docker-compose.yml` — September 2026.