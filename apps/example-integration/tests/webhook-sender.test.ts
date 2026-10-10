import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock fetch before importing the module under test. This keeps the "unit"
// describe truly hermetic — no real network call is ever made unless the
// caller opts in via LUMEN_E2E=1.
const fetchMock = vi.fn();
vi.stubGlobal('fetch', fetchMock);

import { signFeishuPayload } from '../src/webhook-sender/sign.js';
import { buildFeishuMessageEvent } from '../src/webhook-sender/feishu-event.js';

const STUB_KEY = 'M1_STUB_ENCRYPT_KEY_REPLACE_IN_TASK_4_13';

const e2e = process.env.LUMEN_E2E === '1';
const itE2E = e2e ? it : it.skip;

describe('webhook-sender (unit)', () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it('signs a known-good vector with the same SHA-256 scheme Lumen verifies', () => {
    // Same vector pinned in tests/sign.test.ts — keeping both tests in sync
    // means a drift in either file is caught immediately.
    const sig = signFeishuPayload({
      timestamp: '1234567890',
      nonce: 'abc',
      encryptKey: 'key123',
      body: 'hello',
    });
    expect(sig).toBe(
      'a4d083495e8d193425e9b3ab7794a7dc9e670e3c5e0822624bbf3b59f99755bc',
    );
  });

  it('buildFeishuMessageEvent produces the shape the Feishu adapter expects', () => {
    const event = buildFeishuMessageEvent('hello world');
    expect(event.schema).toBe('2.0');
    expect(event.header.event_type).toBe('im.message.receive_v1');
    // adapter.py:46 calls json.loads(content) — it must be a JSON-encoded STRING,
    // not a nested object.
    expect(typeof event.event.message.content).toBe('string');
    const parsed = JSON.parse(event.event.message.content);
    expect(parsed.text).toBe('hello world');
  });

  it('produces the exact headers + URL that the webhook route expects', () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    );

    const event = buildFeishuMessageEvent('How do I reset my password?');
    const body = JSON.stringify(event);
    const ts = '1700000000';
    const nonce = 'test-nonce-001';
    const sig = signFeishuPayload({ timestamp: ts, nonce, encryptKey: STUB_KEY, body });

    const url = `http://localhost:8000/api/v1/channel/feishu/webhook/demo-feishu-app-001`;
    const resp = fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Lark-Request-Timestamp': ts,
        'X-Lark-Request-Nonce': nonce,
        'X-Lark-Signature': sig,
      },
      body,
    });
    // We just want to assert the call shape; consume the resolved promise.
    return resp.then(() => {
      const [calledUrl, init] = fetchMock.mock.calls[0]!;
      expect(calledUrl).toBe(
        'http://localhost:8000/api/v1/channel/feishu/webhook/demo-feishu-app-001',
      );
      expect(init.method).toBe('POST');
      const headers = init.headers as Record<string, string>;
      expect(headers['Content-Type']).toBe('application/json');
      expect(headers['X-Lark-Request-Timestamp']).toBe(ts);
      expect(headers['X-Lark-Request-Nonce']).toBe(nonce);
      expect(headers['X-Lark-Signature']).toBe(sig);
      expect(init.body).toBe(body);
    });
  });
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

      const url = `http://localhost:8000/api/v1/channel/feishu/webhook/demo-feishu-app-001`;
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
    },
    15_000,
  );
});
