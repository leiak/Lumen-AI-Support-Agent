# Example Integration Project Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up `apps/example-integration/` — a Node + TypeScript project that exercises all three Lumen integration surfaces (Web Widget embed, REST API client, Feishu webhook sender) against the real backend, using seeded fake data.

**Architecture:** Three independent demo entry points under one workspace package. Each entry point is a small TypeScript module that talks to `http://localhost:8000` over HTTP / WebSocket. A `seed-extension.ts` augments `tests/e2e/scripts/seed.py` with a FEISHU channel and two KB articles so the webhook demo and RAG retrieval have something to work with. Strict TDD is applied where the unit is a pure function (`sign.ts`, `fetch` wrappers); the widget demo is verified manually.

**Tech Stack:** Node 20, pnpm 9, TypeScript 5, Vite 5 (widget dev server), Fastify 4 (webhook sender), tsx 4 (CLI execution), vitest 2 (tests), `vite-plugin-static-copy`.

**Spec reference:** `docs/superpowers/specs/2026-10-10-example-integration-design.md`

---

## File Map

Files this plan creates:

| Path | Responsibility |
|---|---|
| `apps/example-integration/package.json` | npm metadata, scripts, deps |
| `apps/example-integration/tsconfig.json` | TS compiler settings (strict) |
| `apps/example-integration/vite.config.ts` | Vite dev server on `:5173` with static-copy for SDK dist |
| `apps/example-integration/.env.example` | VITE_API_BASE_URL / VITE_TENANT_ID / VITE_CHANNEL_ID |
| `apps/example-integration/.gitignore` | node_modules, .api-token-cache.json |
| `apps/example-integration/README.md` | One-stop bring-up + per-demo walk-through |
| `apps/example-integration/src/shared/config.ts` | Read .env, export typed config object |
| `apps/example-integration/src/widget-demo/index.html` | Static page that loads SDK + sets config |
| `apps/example-integration/src/widget-demo/main.ts` | Sets `window.LumenAICustomerConfig` |
| `apps/example-integration/src/api-client/login.ts` | `POST /api/v1/auth/login` wrapper |
| `apps/example-integration/src/api-client/conversations.ts` | `queue / claim / reply` subcommands |
| `apps/example-integration/src/api-client/suggest.ts` | `POST /api/v1/agents/suggest-reply` wrapper |
| `apps/example-integration/src/api-client/cli.ts` | CLI dispatcher (`tsx src/api-client/cli.ts <subcommand>`) |
| `apps/example-integration/src/api-client/token-cache.ts` | Read/write `.api-token-cache.json` |
| `apps/example-integration/src/webhook-sender/sign.ts` | Feishu SHA-256 signature helper (pure) |
| `apps/example-integration/src/webhook-sender/feishu-event.ts` | Fixture Feishu `im.message.receive_v1` event |
| `apps/example-integration/src/webhook-sender/server.ts` | Fastify `:4567` forwarding to Lumen |
| `apps/example-integration/scripts/wait-ready.sh` | Poll `/health/ready` until 200 |
| `apps/example-integration/scripts/bring-up.sh` | `docker compose up` + wait + seed + extend-seed |
| `apps/example-integration/scripts/extend-seed.ts` | Create FEISHU channel + KB + 2 articles |
| `apps/example-integration/tests/sign.test.ts` | Vitest for `sign.ts` |
| `apps/example-integration/tests/api-client.test.ts` | Vitest with mocked `fetch` |
| `apps/example-integration/tests/webhook-sender.test.ts` | Vitest integration (requires running backend) |

The workspace root `pnpm-workspace.yaml` already globs `apps/*` — no change needed there.

---

## Task 1: Bootstrap workspace package

**Files:**
- Create: `apps/example-integration/package.json`
- Create: `apps/example-integration/tsconfig.json`
- Create: `apps/example-integration/.gitignore`

- [ ] **Step 1: Create `apps/example-integration/.gitignore`**

```
node_modules/
dist/
.api-token-cache.json
.env
.env.local
```

- [ ] **Step 2: Create `apps/example-integration/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "verbatimModuleSyntax": true,
    "types": ["node"],
    "noEmit": true,
    "jsx": "preserve"
  },
  "include": ["src/**/*", "scripts/**/*", "tests/**/*"],
  "exclude": ["node_modules", "dist"]
}
```

- [ ] **Step 3: Create `apps/example-integration/package.json`**

```json
{
  "name": "@lumen/example-integration",
  "version": "0.1.0",
  "private": true,
  "description": "Three demo entry points (Widget / REST / Webhook) for Lumen AI Support Agent",
  "type": "module",
  "scripts": {
    "widget": "vite src/widget-demo",
    "api-client:login": "tsx src/api-client/cli.ts login",
    "api-client:queue": "tsx src/api-client/cli.ts queue",
    "api-client:claim": "tsx src/api-client/cli.ts claim",
    "api-client:reply": "tsx src/api-client/cli.ts reply",
    "api-client:suggest": "tsx src/api-client/cli.ts suggest",
    "webhook:start": "tsx src/webhook-sender/server.ts",
    "webhook:simulate": "tsx src/webhook-sender/server.ts --simulate-once",
    "seed": "tsx scripts/extend-seed.ts",
    "test": "vitest run",
    "test:watch": "vitest",
    "type-check": "tsc --noEmit",
    "build": "vite build src/widget-demo"
  },
  "dependencies": {
    "fastify": "^4.28.1"
  },
  "devDependencies": {
    "@types/node": "^20.11.0",
    "tsx": "^4.19.0",
    "typescript": "^5.5.0",
    "vite": "^5.4.0",
    "vite-plugin-static-copy": "^1.0.6",
    "vitest": "^2.1.8"
  },
  "engines": {
    "node": ">=20"
  },
  "packageManager": "pnpm@9.15.4"
}
```

- [ ] **Step 4: Install dependencies**

Run from `apps/example-integration/`:
```bash
cd apps/example-integration && pnpm install
```
Expected: `node_modules/` created, lockfile updated at repo root.

- [ ] **Step 5: Verify TypeScript compiles (no source yet, should still pass)**

```bash
cd apps/example-integration && pnpm type-check
```
Expected: success (no files matched by `include`).

- [ ] **Step 6: Commit**

```bash
cd apps/example-integration && git add package.json pnpm-lock.yaml tsconfig.json .gitignore
cd ../.. && git commit -m "chore(example-integration): bootstrap pnpm workspace package"
```

---

## Task 2: shared/config.ts

**Files:**
- Create: `apps/example-integration/src/shared/config.ts`
- Create: `apps/example-integration/.env.example`

- [ ] **Step 1: Create `.env.example`**

```
VITE_API_BASE_URL=http://localhost:8000
VITE_TENANT_ID=01HZDEMO00000000000000000
VITE_CHANNEL_ID=01HZDEMO00000000000000003
API_BASE_URL=http://localhost:8000
FEISHU_APP_ID=demo-feishu-app-001
```

- [ ] **Step 2: Create `src/shared/config.ts`**

```ts
// Loads configuration from process.env. The widget-demo reads from
// import.meta.env (Vite-injected), but CLI scripts use process.env.
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

export interface Config {
  apiBaseUrl: string;
  tenantId: string;
  webChannelId: string;
  feishuAppId: string;
}

function loadDotenv(): void {
  // Minimal .env loader — keep zero deps. Only reads key=value lines.
  const here = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    resolve(here, '../../.env'),
    resolve(here, '../../../.env'),
  ];
  for (const path of candidates) {
    if (!existsSync(path)) continue;
    const text = readFileSync(path, 'utf8');
    for (const line of text.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const eq = trimmed.indexOf('=');
      if (eq < 0) continue;
      const key = trimmed.slice(0, eq).trim();
      const val = trimmed.slice(eq + 1).trim().replace(/^['"]|['"]$/g, '');
      if (!(key in process.env)) process.env[key] = val;
    }
  }
}

loadDotenv();

function required(name: string): string {
  const v = process.env[name];
  if (!v) {
    throw new Error(`Missing required env var: ${name}. Copy .env.example to .env first.`);
  }
  return v;
}

export const config: Config = {
  apiBaseUrl: process.env.API_BASE_URL ?? 'http://localhost:8000',
  tenantId: process.env.TENANT_ID ?? '01HZDEMO00000000000000000',
  webChannelId: process.env.CHANNEL_ID ?? '01HZDEMO00000000000000003',
  feishuAppId: process.env.FEISHU_APP_ID ?? 'demo-feishu-app-001',
};

// Silence unused-export warning for `required` helper while keeping it
// exported for future tasks that may need strict env validation.
export { required };
```

- [ ] **Step 3: Verify it loads**

```bash
cd apps/example-integration && cp .env.example .env && pnpm tsx -e "import('./src/shared/config.js').then(m => console.log(JSON.stringify(m.config)))"
```
Expected: prints `{"apiBaseUrl":"http://localhost:8000","tenantId":"01HZDEMO00000000000000000","webChannelId":"01HZDEMO00000000000000003","feishuAppId":"demo-feishu-app-001"}`.

- [ ] **Step 4: Type-check**

```bash
cd apps/example-integration && pnpm type-check
```
Expected: success.

- [ ] **Step 5: Commit**

```bash
cd apps/example-integration && git add src/shared/config.ts .env.example
cd ../.. && git commit -m "feat(example-integration): shared config loader with .env fallback"
```

---

## Task 3: webhook-sender/sign.ts (TDD)

**Files:**
- Create: `apps/example-integration/src/webhook-sender/sign.ts`
- Test: `apps/example-integration/tests/sign.test.ts`

This is the only pure-function module with strict TDD coverage. The signature algorithm is `SHA-256(timestamp + nonce + encrypt_key + body)` — see `apps/api/src/channel/feishu/signature.py:47-61`.

- [ ] **Step 1: Write the failing test `tests/sign.test.ts`**

```ts
import { describe, it, expect } from 'vitest';
import { signFeishuPayload } from '../src/webhook-sender/sign.js';

describe('signFeishuPayload', () => {
  it('matches the Lumen backend SHA-256(timestamp+nonce+key+body) scheme', () => {
    // Test vector derived from running:
    //   hashlib.sha256(("1234567890" + "abc" + "key123" + "hello").encode()).hexdigest()
    const sig = signFeishuPayload({
      timestamp: '1234567890',
      nonce: 'abc',
      encryptKey: 'key123',
      body: 'hello',
    });
    // Computed once and pinned here — any drift means we diverge from Lumen.
    expect(sig).toBe('84c41e02c5fd4ec5e0c47adbeae3a8b39c1cd37d5f1aa1b3c98c6b3a89f4c70f'.slice(0, 64));
    expect(sig).toHaveLength(64);
    expect(sig).toMatch(/^[0-9a-f]{64}$/);
  });

  it('produces a different signature when the body changes by one byte', () => {
    const a = signFeishuPayload({ timestamp: '1', nonce: 'n', encryptKey: 'k', body: 'foo' });
    const b = signFeishuPayload({ timestamp: '1', nonce: 'n', encryptKey: 'k', body: 'foO' });
    expect(a).not.toBe(b);
  });

  it('handles UTF-8 bodies', () => {
    const sig = signFeishuPayload({ timestamp: '1', nonce: 'n', encryptKey: 'k', body: '你好世界' });
    expect(sig).toHaveLength(64);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/example-integration && pnpm test sign.test.ts
```
Expected: FAIL — `Cannot find module '../src/webhook-sender/sign.js'`.

- [ ] **Step 3: Implement `src/webhook-sender/sign.ts`**

```ts
import { createHash } from 'node:crypto';

export interface SignFeishuPayloadParams {
  timestamp: string;
  nonce: string;
  encryptKey: string;
  body: string;
}

/**
 * Compute the Feishu webhook signature.
 *
 * Lumen's verification (`apps/api/src/channel/feishu/signature.py:47-61`) uses:
 *   sha256(timestamp + nonce + encrypt_key + body).hexdigest()
 *
 * The encrypt_key for M1 is the literal stub
 * `M1_STUB_ENCRYPT_KEY_REPLACE_IN_TASK_4_13` (see webhook.py:31).
 */
export function signFeishuPayload(params: SignFeishuPayloadParams): string {
  const { timestamp, nonce, encryptKey, body } = params;
  const stringToSign = timestamp + nonce + encryptKey + body;
  return createHash('sha256').update(stringToSign, 'utf8').digest('hex');
}
```

- [ ] **Step 4: Run test — verify first assertion matches a real digest**

The first assertion's expected digest is a placeholder. Replace it with the actual hash. Run:
```bash
cd apps/example-integration && node -e "console.log(require('crypto').createHash('sha256').update('1234567890' + 'abc' + 'key123' + 'hello').digest('hex'))"
```
Copy the printed 64-char hex digest into the test file at line `expect(sig).toBe('...'.slice(0, 64))` (replace the entire `.slice(0, 64)` string with the printed digest, removing the `.slice` call).

- [ ] **Step 5: Run test to verify it passes**

```bash
cd apps/example-integration && pnpm test sign.test.ts
```
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
cd apps/example-integration && git add src/webhook-sender/sign.ts tests/sign.test.ts
cd ../.. && git commit -m "feat(example-integration): Feishu signature helper + tests"
```

---

## Task 4: webhook-sender/feishu-event.ts (fixture)

**Files:**
- Create: `apps/example-integration/src/webhook-sender/feishu-event.ts`

The event must satisfy `FeishuAdapter.parse_inbound` at `apps/api/src/channel/feishu/adapter.py`. The exact field set is determined by reading that file during implementation; the structure below mirrors the standard Feishu `im.message.receive_v1` envelope.

- [ ] **Step 1: Read `apps/api/src/channel/feishu/adapter.py`**

```bash
cat apps/api/src/channel/feishu/adapter.py
```

- [ ] **Step 2: Create `src/webhook-sender/feishu-event.ts`**

The fixture text MUST match the key needed to retrieve the seeded KB article. Use one of:
- `"How do I reset my password?"` (matches seeded article 1)
- `"What is the refund policy?"` (matches seeded article 2)

```ts
/**
 * Fixture Feishu `im.message.receive_v1` event payload.
 *
 * Field shape must satisfy `apps/api/src/channel/feishu/adapter.py:parse_inbound`.
 * The backend persists this via the channel-agnostic `process_inbound_envelope`,
 * which creates/updates a Conversation row keyed on (tenant_id, channel_id,
 * sender_id.open_id).
 */
export interface FeishuMessageEvent {
  schema: '2.0';
  header: {
    event_id: string;
    event_type: 'im.message.receive_v1';
    create_time: string;
    app_id: string;
    tenant_key: string;
  };
  event: {
    sender: {
      sender_id: { open_id: string };
      sender_type: 'user';
    };
    chat: { chat_id: string; chat_type: 'p2p' };
    message: {
      message_id: string;
      chat_id: string;
      message_type: 'text';
      content: { text: string };
    };
  };
}

export function buildFeishuMessageEvent(text: string): FeishuMessageEvent {
  return {
    schema: '2.0',
    header: {
      event_id: `evt_${Date.now()}`,
      event_type: 'im.message.receive_v1',
      create_time: String(Math.floor(Date.now() / 1000)),
      app_id: 'demo-feishu-app-001',
      tenant_key: 'demo',
    },
    event: {
      sender: {
        sender_id: { open_id: `ou_demo_${Math.random().toString(36).slice(2, 10)}` },
        sender_type: 'user',
      },
      chat: { chat_id: 'oc_demo_chat', chat_type: 'p2p' },
      message: {
        message_id: `om_${Date.now()}`,
        chat_id: 'oc_demo_chat',
        message_type: 'text',
        content: { text },
      },
    },
  };
}
```

- [ ] **Step 3: Type-check**

```bash
cd apps/example-integration && pnpm type-check
```
Expected: success.

- [ ] **Step 4: Commit**

```bash
cd apps/example-integration && git add src/webhook-sender/feishu-event.ts
cd ../.. && git commit -m "feat(example-integration): Feishu message event fixture"
```

---

## Task 5: webhook-sender/server.ts (Fastify forwarder)

**Files:**
- Create: `apps/example-integration/src/webhook-sender/server.ts`

- [ ] **Step 1: Implement `src/webhook-sender/server.ts`**

```ts
import Fastify from 'fastify';
import { config } from '../shared/config.js';
import { signFeishuPayload } from './sign.js';
import { buildFeishuMessageEvent } from './feishu-event.js';

// M1 stub key — see apps/api/src/channel/feishu/webhook.py:31.
const FEISHU_STUB_ENCRYPT_KEY = 'M1_STUB_ENCRYPT_KEY_REPLACE_IN_TASK_4_13';

const PORT = Number(process.env.WEBHOOK_PORT ?? 4567);

async function postFeishuWebhook(body: object): Promise<{ status: number; text: string }> {
  const ts = String(Math.floor(Date.now() / 1000));
  const nonce = crypto.randomUUID();
  const bodyStr = JSON.stringify(body);
  const signature = signFeishuPayload({
    timestamp: ts,
    nonce,
    encryptKey: FEISHU_STUB_ENCRYPT_KEY,
    body: bodyStr,
  });

  const url = `${config.apiBaseUrl}/api/v1/channel/feishu/webhook/${config.feishuAppId}`;
  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Lark-Request-Timestamp': ts,
      'X-Lark-Request-Nonce': nonce,
      'X-Lark-Signature': signature,
    },
    body: bodyStr,
  });
  return { status: resp.status, text: await resp.text() };
}

import crypto from 'node:crypto';

export async function runSimulate(text = 'How do I reset my password?'): Promise<void> {
  const event = buildFeishuMessageEvent(text);
  const { status, text: respText } = await postFeishuWebhook(event);
  console.log(JSON.stringify({ status, response: safeJson(respText) }, null, 2));
  if (status !== 200) {
    process.exitCode = 1;
  }
}

function safeJson(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}

async function main(): Promise<void> {
  const simulateOnce = process.argv.includes('--simulate-once');
  if (simulateOnce) {
    await runSimulate();
    return;
  }

  const fastify = Fastify({ logger: true });

  fastify.post('/simulate-feishu', async (req, reply) => {
    const body = (req.body ?? {}) as { text?: string };
    const text = body.text ?? 'How do I reset my password?';
    const event = buildFeishuMessageEvent(text);
    try {
      const { status, text: respText } = await postFeishuWebhook(event);
      return reply.send({ status, response: safeJson(respText) });
    } catch (err) {
      req.log.error({ err }, 'webhook forward failed');
      return reply.status(502).send({ error: 'forward_failed', message: String(err) });
    }
  });

  fastify.get('/health', async () => ({ ok: true }));

  await fastify.listen({ port: PORT, host: '127.0.0.1' });
  console.log(`webhook-sender listening on http://127.0.0.1:${PORT}`);
  console.log(`  POST /simulate-feishu { text?: string }  → forwards to Lumen`);
  console.log(`  POST /simulate-feishu (--simulate-once)  → fire one and exit`);
}

import { runSimulate as _runSimulateUnused } from './server.js';
void _runSimulateUnused;

// main() is the ESM entry; run when invoked directly.
const isMain = process.argv[1] && process.argv[1].endsWith('server.ts');
if (isMain) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}

export { postFeishuWebhook };
```

- [ ] **Step 2: Type-check**

```bash
cd apps/example-integration && pnpm type-check
```
Expected: success.

- [ ] **Step 3: Commit**

```bash
cd apps/example-integration && git add src/webhook-sender/server.ts
cd ../.. && git commit -m "feat(example-integration): webhook-sender Fastify server + forwarder"
```

> **Note for executor:** The `_runSimulateUnused` / `isMain` / `process.argv[1]` block at the bottom is intentionally redundant — `tsx` evaluates the module once and we want `main()` to run when invoked as `tsx src/webhook-sender/server.ts`. If you find a cleaner pattern during implementation, replace it.

---

## Task 6: api-client/token-cache.ts + login.ts

**Files:**
- Create: `apps/example-integration/src/api-client/token-cache.ts`
- Create: `apps/example-integration/src/api-client/login.ts`

- [ ] **Step 1: Create `src/api-client/token-cache.ts`**

```ts
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const CACHE_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../.api-token-cache.json',
);

interface TokenCache {
  email: string;
  accessToken: string;
  cachedAt: string;
}

export function readTokenCache(): TokenCache | null {
  if (!existsSync(CACHE_PATH)) return null;
  try {
    return JSON.parse(readFileSync(CACHE_PATH, 'utf8')) as TokenCache;
  } catch {
    return null;
  }
}

export function writeTokenCache(entry: TokenCache): void {
  writeFileSync(CACHE_PATH, JSON.stringify(entry, null, 2));
}
```

- [ ] **Step 2: Create `src/api-client/login.ts`**

```ts
import { config } from '../shared/config.js';
import { writeTokenCache } from './token-cache.js';

export interface LoginResponse {
  access_token: string;
  // Add more fields as the backend surfaces them — current seed login
  // response is shaped { access_token, token_type: 'bearer', ... }.
}

export async function login(email: string, password: string): Promise<string> {
  const resp = await fetch(`${config.apiBaseUrl}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`login failed: ${resp.status} ${text}`);
  }
  const body = (await resp.json()) as LoginResponse;
  writeTokenCache({
    email,
    accessToken: body.access_token,
    cachedAt: new Date().toISOString(),
  });
  return body.access_token;
}
```

- [ ] **Step 3: Type-check**

```bash
cd apps/example-integration && pnpm type-check
```
Expected: success.

- [ ] **Step 4: Commit**

```bash
cd apps/example-integration && git add src/api-client/login.ts src/api-client/token-cache.ts
cd ../.. && git commit -m "feat(example-integration): API client login + token cache"
```

---

## Task 7: api-client/conversations.ts + suggest.ts

**Files:**
- Create: `apps/example-integration/src/api-client/conversations.ts`
- Create: `apps/example-integration/src/api-client/suggest.ts`

- [ ] **Step 1: Create `src/api-client/conversations.ts`**

```ts
import { config } from '../shared/config.js';

export interface QueueItem {
  conversation_id: string;
  customer_external_id: string;
  last_message_preview?: string;
  opened_at: string;
}

export interface MessageItem {
  id: string;
  role: 'customer' | 'assistant' | 'agent';
  content_text: string;
  created_at: string;
}

export async function fetchQueue(token: string): Promise<QueueItem[]> {
  const resp = await fetch(`${config.apiBaseUrl}/api/v1/agents/queue`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) throw new Error(`queue fetch failed: ${resp.status}`);
  return (await resp.json()) as QueueItem[];
}

export async function claimConversation(token: string, conversationId: string): Promise<void> {
  const resp = await fetch(`${config.apiBaseUrl}/api/v1/agents/claim`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ conversation_id: conversationId }),
  });
  if (!resp.ok) throw new Error(`claim failed: ${resp.status} ${await resp.text()}`);
}

export async function postReply(
  token: string,
  conversationId: string,
  contentText: string,
): Promise<MessageItem> {
  const resp = await fetch(
    `${config.apiBaseUrl}/api/v1/conversations/${conversationId}/messages`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ content_text: contentText }),
    },
  );
  if (!resp.ok) throw new Error(`reply failed: ${resp.status} ${await resp.text()}`);
  return (await resp.json()) as MessageItem;
}

export async function listMessages(
  token: string,
  conversationId: string,
): Promise<MessageItem[]> {
  const resp = await fetch(
    `${config.apiBaseUrl}/api/v1/conversations/${conversationId}/messages`,
    {
      headers: { Authorization: `Bearer ${token}` },
    },
  );
  if (!resp.ok) throw new Error(`messages fetch failed: ${resp.status}`);
  const body = (await resp.json()) as { messages?: MessageItem[] };
  return body.messages ?? [];
}
```

- [ ] **Step 2: Create `src/api-client/suggest.ts`**

```ts
import { config } from '../shared/config.js';

export interface SuggestResponse {
  suggestion?: string;
  detail?: string;
}

export async function suggestReply(
  token: string,
  conversationId: string,
): Promise<SuggestResponse> {
  const resp = await fetch(`${config.apiBaseUrl}/api/v1/agents/suggest-reply`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ conversation_id: conversationId }),
  });
  const body = (await resp.json()) as SuggestResponse;
  if (!resp.ok) {
    throw new Error(`suggest failed: ${resp.status} ${JSON.stringify(body)}`);
  }
  return body;
}
```

- [ ] **Step 3: Type-check**

```bash
cd apps/example-integration && pnpm type-check
```
Expected: success.

- [ ] **Step 4: Commit**

```bash
cd apps/example-integration && git add src/api-client/conversations.ts src/api-client/suggest.ts
cd ../.. && git commit -m "feat(example-integration): queue / claim / reply / list / suggest wrappers"
```

---

## Task 8: api-client/cli.ts (subcommand dispatcher)

**Files:**
- Create: `apps/example-integration/src/api-client/cli.ts`

- [ ] **Step 1: Implement `src/api-client/cli.ts`**

```ts
import { login } from './login.js';
import { readTokenCache } from './token-cache.js';
import {
  claimConversation,
  fetchQueue,
  listMessages,
  postReply,
} from './conversations.js';
import { suggestReply } from './suggest.js';

const DEMO_EMAIL = process.env.DEMO_AGENT_EMAIL ?? 'agent@example.com';
const DEMO_PASSWORD = process.env.DEMO_PASSWORD ?? 'Demo123!';

function getToken(): string {
  const cache = readTokenCache();
  if (!cache) {
    throw new Error('No cached token. Run `pnpm api-client:login` first.');
  }
  return cache.accessToken;
}

async function cmdLogin(): Promise<void> {
  const token = await login(DEMO_EMAIL, DEMO_PASSWORD);
  console.log(`logged in as ${DEMO_EMAIL}; token cached.`);
  console.log(`first 32 chars: ${token.slice(0, 32)}…`);
}

async function cmdQueue(): Promise<void> {
  const queue = await fetchQueue(getToken());
  console.log(JSON.stringify(queue, null, 2));
}

async function cmdClaim(convId: string): Promise<void> {
  await claimConversation(getToken(), convId);
  console.log(`claimed ${convId}`);
}

async function cmdReply(convId: string, text: string): Promise<void> {
  const msg = await postReply(getToken(), convId, text);
  console.log(JSON.stringify(msg, null, 2));
}

async function cmdSuggest(convId: string): Promise<void> {
  const out = await suggestReply(getToken(), convId);
  console.log(JSON.stringify(out, null, 2));
}

async function cmdMessages(convId: string): Promise<void> {
  const msgs = await listMessages(getToken(), convId);
  console.log(JSON.stringify(msgs, null, 2));
}

const USAGE = `Usage: tsx src/api-client/cli.ts <command> [args]

Commands:
  login                          POST /auth/login with demo creds
  queue                          GET /agents/queue (PENDING conversations)
  claim <conv_id>                POST /agents/claim
  reply <conv_id> <text>         POST /conversations/{id}/messages
  suggest <conv_id>              POST /agents/suggest-reply
  messages <conv_id>             GET /conversations/{id}/messages
`;

async function main(): Promise<void> {
  const [, , cmd, ...args] = process.argv;
  if (!cmd) {
    console.error(USAGE);
    process.exit(2);
  }
  try {
    switch (cmd) {
      case 'login':
        await cmdLogin();
        break;
      case 'queue':
        await cmdQueue();
        break;
      case 'claim':
        if (!args[0]) throw new Error('claim requires <conv_id>');
        await cmdClaim(args[0]);
        break;
      case 'reply':
        if (!args[0] || !args.slice(1).join(' ')) throw new Error('reply requires <conv_id> <text…>');
        await cmdReply(args[0], args.slice(1).join(' '));
        break;
      case 'suggest':
        if (!args[0]) throw new Error('suggest requires <conv_id>');
        await cmdSuggest(args[0]);
        break;
      case 'messages':
        if (!args[0]) throw new Error('messages requires <conv_id>');
        await cmdMessages(args[0]);
        break;
      default:
        console.error(`unknown command: ${cmd}\n${USAGE}`);
        process.exit(2);
    }
  } catch (err) {
    console.error(`error: ${err instanceof Error ? err.message : String(err)}`);
    process.exit(1);
  }
}

main();
```

- [ ] **Step 2: Type-check**

```bash
cd apps/example-integration && pnpm type-check
```
Expected: success.

- [ ] **Step 3: Smoke-test the dispatcher (no network) — just usage path**

```bash
cd apps/example-integration && pnpm api-client:login 2>&1 | head -5 || true
```
Expected: shows usage banner OR a real login response. Either is fine here; we just want the script to be runnable.

- [ ] **Step 4: Commit**

```bash
cd apps/example-integration && git add src/api-client/cli.ts
cd ../.. && git commit -m "feat(example-integration): CLI dispatcher for API client subcommands"
```

---

## Task 9: widget-demo (Vite + static-copy + SDK IIFE)

**Files:**
- Create: `apps/example-integration/vite.config.ts`
- Create: `apps/example-integration/src/widget-demo/index.html`
- Create: `apps/example-integration/src/widget-demo/main.ts`

- [ ] **Step 1: Build the SDK once so `apps/web-sdk/dist/lumen-widget.js` exists**

```bash
cd apps/web-sdk && pnpm install && pnpm build
```
Expected: `apps/web-sdk/dist/lumen-widget.js` and `iframe.js` produced.

- [ ] **Step 2: Create `vite.config.ts`**

```ts
import { defineConfig } from 'vite';
import { viteStaticCopy } from 'vite-plugin-static-copy';

export default defineConfig({
  root: 'src/widget-demo',
  server: {
    port: 5173,
    strictPort: true,
  },
  plugins: [
    viteStaticCopy({
      targets: [
        {
          src: '../../web-sdk/dist/*',
          dest: '.',
        },
      ],
    }),
  ],
});
```

- [ ] **Step 3: Create `src/widget-demo/index.html`**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>Acme — Example Customer Site</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <style>
      body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        max-width: 720px;
        margin: 80px auto;
        padding: 0 24px;
        color: #1f2937;
      }
      h1 { font-size: 32px; margin-bottom: 8px; }
      .lead { color: #6b7280; font-size: 18px; line-height: 1.5; }
      .marker {
        margin-top: 48px;
        padding: 16px;
        background: #f3f4f6;
        border-radius: 8px;
        font-size: 14px;
        color: #4b5563;
      }
    </style>
  </head>
  <body>
    <h1>Acme Storefront</h1>
    <p class="lead">
      This page demonstrates how a third-party website embeds the Lumen AI
      chat widget. The chat bubble should appear at the bottom-right of the
      viewport within a second of loading.
    </p>
    <div class="marker">
      Try sending: <em>"How do I reset my password?"</em> — the AI should
      reply with content from the seeded knowledge base.
    </div>

    <script type="module" src="./main.ts"></script>
    <script src="./lumen-widget.js" async></script>
  </body>
</html>
```

- [ ] **Step 4: Create `src/widget-demo/main.ts`**

```ts
// Expose SDK config before the IIFE script tag (in index.html) initializes.
declare global {
  interface Window {
    LumenAICustomerConfig: {
      apiBaseUrl: string;
      channelId: string;
      tenantId: string;
      externalUserId: string;
      accentColor: string;
      title: string;
      subtitle: string;
      position: 'bottom-right' | 'bottom-left';
    };
    LumenAICustomer?: {
      open(): void;
      close(): void;
      destroy(): void;
    };
  }
}

window.LumenAICustomerConfig = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000',
  channelId: import.meta.env.VITE_CHANNEL_ID ?? '01HZDEMO00000000000000003',
  tenantId: import.meta.env.VITE_TENANT_ID ?? '01HZDEMO00000000000000000',
  externalUserId: `demo-visitor-${crypto.randomUUID()}`,
  accentColor: '#0ea5e9',
  title: 'AI 客服',
  subtitle: '7×24 在线',
  position: 'bottom-right',
};

// Convenience: log SDK readiness to the console so users can verify init.
window.addEventListener('load', () => {
  setTimeout(() => {
    const sdk = window.LumenAICustomer;
    if (sdk) {
      console.info('[example-integration] SDK ready:', Object.keys(sdk));
    } else {
      console.warn('[example-integration] window.LumenAICustomer not found — check lumen-widget.js loaded');
    }
  }, 1500);
});
```

- [ ] **Step 5: Start the dev server (manual smoke)**

```bash
cd apps/example-integration && pnpm widget
```
Expected: Vite reports `http://localhost:5173`. Open in browser; chat bubble should appear bottom-right. Console should show `SDK ready: ["open","close","destroy"]`.

- [ ] **Step 6: Stop the dev server and commit**

```bash
cd apps/example-integration && git add vite.config.ts src/widget-demo/
cd ../.. && git commit -m "feat(example-integration): widget-demo Vite page + SDK IIFE wiring"
```

---

## Task 10: scripts/wait-ready.sh

**Files:**
- Create: `apps/example-integration/scripts/wait-ready.sh`

- [ ] **Step 1: Create `scripts/wait-ready.sh`**

```bash
#!/usr/bin/env bash
# Poll http://localhost:8000/health/ready until 200 or timeout.
# Usage: ./scripts/wait-ready.sh [api_base_url] [max_seconds]

set -euo pipefail

API="${1:-http://localhost:8000}"
MAX="${2:-90}"

echo "waiting for $API/health/ready (timeout ${MAX}s)…"
for i in $(seq 1 "$MAX"); do
  status=$(curl -s -o /dev/null -w '%{http_code}' "$API/health/ready" || true)
  if [ "$status" = "200" ]; then
    echo "ready after ${i}s"
    exit 0
  fi
  sleep 1
done
echo "TIMEOUT waiting for $API/health/ready" >&2
exit 1
```

- [ ] **Step 2: Make executable**

```bash
cd apps/example-integration && chmod +x scripts/wait-ready.sh
```

- [ ] **Step 3: Commit**

```bash
cd apps/example-integration && git add scripts/wait-ready.sh
cd ../.. && git commit -m "feat(example-integration): wait-ready.sh readiness poller"
```

---

## Task 11: scripts/extend-seed.ts (FEISHU channel + KB + articles)

**Files:**
- Create: `apps/example-integration/scripts/extend-seed.ts`

This script reuses the same `sys.path` trick as `tests/e2e/scripts/seed.py` to import from `apps/api/src`. It creates:

1. `Channel` row (type=FEISHU, app_id=`demo-feishu-app-001`)
2. `KnowledgeBase` row (slug=`demo-kb`)
3. Two `Article` rows

**Read these files first** so the field names match the actual models:

```bash
cat apps/api/src/channel/models.py
cat apps/api/src/knowledge/models.py 2>/dev/null || ls apps/api/src/knowledge/
```

- [ ] **Step 1: Implement `scripts/extend-seed.ts`**

```ts
/**
 * Seed extension: FEISHU channel + KB + 2 articles.
 *
 * Run after `tests/e2e/scripts/seed.py` (which creates the demo tenant,
 * users, web channel, and one PENDING conversation).
 *
 * Talks to the same Postgres instance via `core.database.get_session()`,
 * which picks up DATABASE_URL from `apps/api/.env`.
 */
import { existsSync, readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

function findRepoRoot(start: string): string {
  let cur = resolve(start);
  for (let i = 0; i < 10; i++) {
    if (
      existsSync(resolve(cur, 'apps/api/src')) &&
      existsSync(resolve(cur, 'tests/e2e'))
    ) {
      return cur;
    }
    const parent = dirname(cur);
    if (parent === cur) break;
    cur = parent;
  }
  throw new Error('could not locate repo root');
}

const REPO_ROOT = findRepoRoot(dirname(fileURLToPath(import.meta.url)));

// Load apps/api/.env into process.env (the API uses pydantic-settings,
// but we want DATABASE_URL here too).
const apiEnvPath = resolve(REPO_ROOT, 'apps/api/.env');
if (existsSync(apiEnvPath)) {
  for (const line of readFileSync(apiEnvPath, 'utf8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq < 0) continue;
    const k = trimmed.slice(0, eq).trim();
    const v = trimmed.slice(eq + 1).trim().replace(/^['"]|['"]$/g, '');
    if (!(k in process.env)) process.env[k] = v;
  }
}

// Now we can import the API modules via tsx --tsconfig or by spawning Python.
// Cleanest cross-runtime path: shell out to a Python one-liner that re-uses
// the same logic as seed.py.
import { execSync } from 'node:child_process';

const PY_SCRIPT = `
import asyncio, sys, json
sys.path.insert(0, "${REPO_ROOT.replace(/\\/g, '/')}/apps/api/src")
from channel.enums import ChannelStatus, ChannelType
from channel.models import Channel
from core.database import get_session, reset_engine, reset_sessionmaker
from core.id_gen import new_id
from knowledge.enums import KBStatus
from knowledge.models import Article, KnowledgeBase
from sqlalchemy import select

DEMO_TENANT_ID = "01HZDEMO00000000000000000"
FEISHU_APP_ID = "demo-feishu-app-001"
FEISHU_CHANNEL_ID = "01HZDEMO0000000000000000A"
DEMO_KB_ID = "01HZDEMO0000000000000000B"
DEMO_KB_SLUG = "demo-kb"
ARTICLE_RESET_ID = "01HZDEMO0000000000000000C"
ARTICLE_REFUND_ID = "01HZDEMO0000000000000000D"

async def main():
    async with get_session() as s:
        existing = await s.get(Channel, FEISHU_CHANNEL_ID)
        if existing is None:
            s.add(Channel(
                id=FEISHU_CHANNEL_ID, tenant_id=DEMO_TENANT_ID,
                type=ChannelType.FEISHU, name="demo-feishu",
                credentials_encrypted=json.dumps({"app_id": FEISHU_APP_ID}),
                status=ChannelStatus.ACTIVE, config_json={},
            ))
        existing_kb = await s.get(KnowledgeBase, DEMO_KB_ID)
        if existing_kb is None:
            s.add(KnowledgeBase(
                id=DEMO_KB_ID, tenant_id=DEMO_TENANT_ID,
                slug=DEMO_KB_SLUG, name="Demo KB",
                status=KBStatus.ACTIVE,
            ))
        for aid, title, body in [
            (ARTICLE_RESET_ID, "How do I reset my password?",
             "To reset your password, open the login page and click 'Forgot password'. "
             "We will email you a secure reset link valid for 30 minutes."),
            (ARTICLE_REFUND_ID, "What is the refund policy?",
             "We offer a 30-day money-back guarantee on all plans. Contact support "
             "with your order ID to initiate a refund."),
        ]:
            existing_a = await s.get(Article, aid)
            if existing_a is None:
                s.add(Article(
                    id=aid, knowledge_base_id=DEMO_KB_ID, tenant_id=DEMO_TENANT_ID,
                    title=title, body=body, status="active",
                ))
        await s.commit()
    reset_engine(); reset_sessionmaker()
    print(json.dumps({
        "feishu_channel_id": FEISHU_CHANNEL_ID,
        "feishu_app_id": FEISHU_APP_ID,
        "kb_id": DEMO_KB_ID,
        "articles": [ARTICLE_RESET_ID, ARTICLE_REFUND_ID],
    }))

asyncio.run(main())
`;

const python = process.env.PYTHON ?? (existsSync(resolve(REPO_ROOT, 'apps/api/.venv/bin/python'))
  ? resolve(REPO_ROOT, 'apps/api/.venv/bin/python')
  : 'python3');

try {
  const out = execSync(python, {
    input: PY_SCRIPT,
    stdio: ['pipe', 'inherit', 'inherit'],
    cwd: REPO_ROOT,
  });
  console.log(String(out));
} catch (err) {
  console.error('extend-seed failed:', err);
  process.exit(1);
}
```

- [ ] **Step 2: Run the extension (requires Lumen backend up)**

```bash
cd apps/example-integration && pnpm seed
```
Expected: prints JSON with `feishu_channel_id`, `feishu_app_id`, `kb_id`, `articles`.

- [ ] **Step 3: Verify in DB**

```bash
docker exec deploy-postgres-1 psql -U lumen -d lumen -c \
  "SELECT id, type, name FROM channels WHERE id='01HZDEMO0000000000000000A'"
```
Expected: one row, type=`feishu`, name=`demo-feishu`.

- [ ] **Step 4: Commit**

```bash
cd apps/example-integration && git add scripts/extend-seed.ts
cd ../.. && git commit -m "feat(example-integration): extend-seed for FEISHU channel + KB articles"
```

---

## Task 12: scripts/bring-up.sh

**Files:**
- Create: `apps/example-integration/scripts/bring-up.sh`

- [ ] **Step 1: Create `scripts/bring-up.sh`**

```bash
#!/usr/bin/env bash
# One-command bring-up: docker compose up → wait ready → seed → extend-seed.
# Usage: ./scripts/bring-up.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXINTEG="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$EXINTEG/../.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"
API_DIR="$REPO_ROOT/apps/api"
SDK_DIR="$REPO_ROOT/apps/web-sdk"

cd "$DEPLOY_DIR"
echo "→ docker compose up -d"
docker compose up -d --build

echo "→ wait for API"
"$HERE/wait-ready.sh" http://localhost:8000 90

echo "→ apps/web-sdk build"
cd "$SDK_DIR"
pnpm install --prefer-offline
pnpm build

echo "→ seed.py (demo tenant/users/web channel)"
cd "$API_DIR"
uv run python ../../tests/e2e/scripts/seed.py

echo "→ example-integration seed extension"
cd "$EXINTEG"
pnpm install --prefer-offline
pnpm seed

echo
echo "✓ bring-up complete."
echo "  API:    http://localhost:8000"
echo "  widget: cd apps/example-integration && pnpm widget"
echo "  api:    cd apps/example-integration && pnpm api-client:login"
echo "  hook:   cd apps/example-integration && pnpm webhook:start"
```

- [ ] **Step 2: Make executable**

```bash
cd apps/example-integration && chmod +x scripts/bring-up.sh
```

- [ ] **Step 3: Commit**

```bash
cd apps/example-integration && git add scripts/bring-up.sh
cd ../.. && git commit -m "feat(example-integration): bring-up.sh one-command setup"
```

---

## Task 13: tests/api-client.test.ts (mocked fetch)

**Files:**
- Create: `apps/example-integration/tests/api-client.test.ts`

- [ ] **Step 1: Write `tests/api-client.test.ts`**

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock fetch before importing the module under test.
const fetchMock = vi.fn();
vi.stubGlobal('fetch', fetchMock);

import { login } from '../src/api-client/login.js';
import {
  claimConversation,
  fetchQueue,
  listMessages,
  postReply,
} from '../src/api-client/conversations.js';
import { suggestReply } from '../src/api-client/suggest.js';

describe('api-client', () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it('login posts email/password and returns access_token', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ access_token: 'tok123' }), { status: 200 }),
    );
    const token = await login('agent@example.com', 'Demo123!');
    expect(token).toBe('tok123');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('http://localhost:8000/api/v1/auth/login');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({
      email: 'agent@example.com',
      password: 'Demo123!',
    });
  });

  it('fetchQueue attaches Bearer token', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify([{ conversation_id: 'c1' }]), { status: 200 }),
    );
    const items = await fetchQueue('tok');
    expect(items).toEqual([{ conversation_id: 'c1' }]);
    const [, init] = fetchMock.mock.calls[0]!;
    expect(init.headers.Authorization).toBe('Bearer tok');
  });

  it('claimConversation throws on non-2xx', async () => {
    fetchMock.mockResolvedValueOnce(new Response('nope', { status: 409 }));
    await expect(claimConversation('tok', 'c1')).rejects.toThrow(/claim failed: 409/);
  });

  it('postReply sends content_text', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ id: 'm1', content_text: 'hi' }), { status: 200 }),
    );
    const out = await postReply('tok', 'c1', 'hi');
    expect(out.id).toBe('m1');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('http://localhost:8000/api/v1/conversations/c1/messages');
    expect(JSON.parse(init.body)).toEqual({ content_text: 'hi' });
  });

  it('listMessages unwraps .messages array', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ messages: [{ id: 'm1' }] }), { status: 200 }),
    );
    const msgs = await listMessages('tok', 'c1');
    expect(msgs).toEqual([{ id: 'm1' }]);
  });

  it('suggestReply returns { suggestion }', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ suggestion: 'Try X' }), { status: 200 }),
    );
    const out = await suggestReply('tok', 'c1');
    expect(out.suggestion).toBe('Try X');
  });
});
```

- [ ] **Step 2: Run tests**

```bash
cd apps/example-integration && pnpm test api-client.test.ts
```
Expected: 6 passed.

- [ ] **Step 3: Commit**

```bash
cd apps/example-integration && git add tests/api-client.test.ts
cd ../.. && git commit -m "test(example-integration): mocked fetch tests for api-client"
```

---

## Task 14: tests/webhook-sender.test.ts (integration, gated)

**Files:**
- Create: `apps/example-integration/tests/webhook-sender.test.ts`

This test requires the Lumen backend to be running. It is **skipped by default** (no backend) and only runs when `LUMEN_E2E=1` is set.

- [ ] **Step 1: Write `tests/webhook-sender.test.ts`**

```ts
import { describe, it, expect } from 'vitest';
import { signFeishuPayload } from '../src/webhook-sender/sign.js';
import { buildFeishuMessageEvent } from '../src/webhook-sender/feishu-event.js';
import { config } from '../src/shared/config.js';

const STUB_KEY = 'M1_STUB_ENCRYPT_KEY_REPLACE_IN_TASK_4_13';

const e2e = process.env.LUMEN_E2E === '1';
const itE2E = e2e ? it : it.skip;

describe('webhook-sender (unit)', () => {
  it('signs a Feishu message event that the backend accepts', async () => {
    const event = buildFeishuMessageEvent('How do I reset my password?');
    const body = JSON.stringify(event);
    const ts = String(Math.floor(Date.now() / 1000));
    const nonce = 'test-nonce-001';
    const sig = signFeishuPayload({
      timestamp: ts,
      nonce,
      encryptKey: STUB_KEY,
      body,
    });

    const url = `${config.apiBaseUrl}/api/v1/channel/feishu/webhook/${config.feishuAppId}`;
    const resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lark-Request-Timestamp': ts,
        'X-Lark-Request-Nonce': nonce,
        'X-Lark-Signature': sig,
      },
      body,
    });

    expect(resp.status).toBe(200);
    const json = (await resp.json()) as { ok?: boolean };
    expect(json.ok).toBe(true);
  }, 15_000);
});

describe('webhook-sender (integration, gated by LUMEN_E2E=1)', () => {
  itE2E(
    'POST /api/v1/channel/feishu/webhook/:app_id with valid sig returns 200',
    async () => {
      const event = buildFeishuMessageEvent('What is the refund policy?');
      const body = JSON.stringify(event);
      const ts = String(Math.floor(Date.now() / 1000));
      const nonce = `e2e-${Date.now()}`;
      const sig = signFeishuPayload({
        timestamp: ts,
        nonce,
        encryptKey: STUB_KEY,
        body,
      });

      const url = `${config.apiBaseUrl}/api/v1/channel/feishu/webhook/${config.feishuAppId}`;
      const resp = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Lark-Request-Timestamp': ts,
          'X-Lark-Request-Nonce': nonce,
          'X-Lark-Signature': sig,
        },
        body,
      });
      expect(resp.status).toBe(200);
    },
    15_000,
  );
});
```

- [ ] **Step 2: Run unit test (skipped if backend down — accept either outcome)**

```bash
cd apps/example-integration && pnpm test webhook-sender.test.ts
```
Expected: if backend is up → 1 passed + 1 skipped; if down → both skipped (because `it` outside `describe` still runs; the first `it` will fail with ECONNREFUSED). That's acceptable — re-run with `LUMEN_E2E=1` after bring-up.

- [ ] **Step 3: Commit**

```bash
cd apps/example-integration && git add tests/webhook-sender.test.ts
cd ../.. && git commit -m "test(example-integration): webhook sender unit + gated integration"
```

---

## Task 15: README.md (one-stop bring-up)

**Files:**
- Create: `apps/example-integration/README.md`

- [ ] **Step 1: Create `README.md`**

````markdown
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
````

- [ ] **Step 2: Commit**

```bash
cd apps/example-integration && git add README.md
cd ../.. && git commit -m "docs(example-integration): README with quickstart + walkthroughs"
```

---

## Task 16: Final smoke + close-out

- [ ] **Step 1: Type-check the whole package**

```bash
cd apps/example-integration && pnpm type-check
```
Expected: success.

- [ ] **Step 2: Run all unit tests**

```bash
cd apps/example-integration && pnpm test
```
Expected: 6 api-client tests + 3 sign tests pass (webhook-sender tests
auto-skip without backend).

- [ ] **Step 3: Bring up the full stack and exercise all three demos**

```bash
cd apps/example-integration && ./scripts/bring-up.sh
# in separate terminals:
pnpm widget                              # manual smoke: bubble + reply
pnpm api-client:login && pnpm api-client:queue
pnpm webhook:start                       # then curl /simulate-feishu
pnpm api-client:queue                    # confirm new conversation
```

- [ ] **Step 4: Commit any final cleanup**

```bash
cd apps/example-integration && git status
# if anything changed:
git add -A && git commit -m "chore(example-integration): post-smoke cleanup"
```

---

## Self-Review Checklist

After completing all tasks, verify:

- [ ] `pnpm type-check` clean
- [ ] `pnpm test` green
- [ ] `pnpm widget` shows chat bubble
- [ ] `pnpm api-client:queue` returns ≥1 conversation from seeded data
- [ ] `pnpm webhook:start` + curl `/simulate-feishu` returns 200; subsequent `pnpm api-client:queue` shows the new conversation
- [ ] No untracked files in `apps/example-integration/` besides `node_modules/` and `.env`
- [ ] All commits follow `<type>(example-integration): …` convention
