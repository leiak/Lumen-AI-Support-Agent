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

// Minimal real-shape fixtures — only the fields each wrapper actually
// round-trips through. Tests assert SHAPE first, behavior second, so a
// regression that drifts the wrapper back to the wrong response shape
// fails loudly here instead of silently at runtime.
const queueItem = {
  id: '01HZDEMO00000000000000004',
  tenant_id: 't1',
  channel_id: 'ch1',
  customer_external_id: 'cust-001',
  status: 'pending',
  assigned_agent_id: null,
  ai_handling: false,
  opened_at: '2026-10-10T08:00:00Z',
  last_activity_at: '2026-10-10T08:00:00Z',
} as const;

const messageItem = {
  id: 'm1',
  conversation_id: '01HZDEMO00000000000000004',
  role: 'agent',
  content_text: 'hi',
  sender_id: 'agent-1',
  created_at: '2026-10-10T08:00:00Z',
} as const;

const suggestionPayload = {
  conversation_id: '01HZDEMO00000000000000004',
  suggested_text: 'Try X',
  citations: [],
  retrieval_score_max: 0,
  warning: null,
  turn_kind: 'rag_hit',
} as const;

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

  it('fetchQueue attaches Bearer token + unwraps .items', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [queueItem] }), { status: 200 }),
    );
    const items = await fetchQueue('tok');
    expect(items).toEqual([queueItem]);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('http://localhost:8000/api/v1/agents/queue');
    expect(init.headers.Authorization).toBe('Bearer tok');
    // No body on a GET.
    expect(init.body).toBeUndefined();
  });

  it('claimConversation hits /conversations/{id}/claim with no body', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ ...queueItem, assigned_agent_id: 'a1' }), {
        status: 200,
      }),
    );
    const out = await claimConversation('tok', 'c1');
    expect(out.assigned_agent_id).toBe('a1');
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('http://localhost:8000/api/v1/agents/conversations/c1/claim');
    expect(init.method).toBe('POST');
    // No body — id is in the path, not the payload.
    expect(init.body).toBeUndefined();
  });

  it('claimConversation throws on non-2xx', async () => {
    fetchMock.mockResolvedValueOnce(new Response('nope', { status: 409 }));
    await expect(claimConversation('tok', 'c1')).rejects.toThrow(/claim failed: 409/);
  });

  it('postReply sends content_text', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(messageItem), { status: 200 }),
    );
    const out = await postReply('tok', 'c1', 'hi');
    expect(out).toEqual(messageItem);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('http://localhost:8000/api/v1/conversations/c1/messages');
    expect(JSON.parse(init.body)).toEqual({ content_text: 'hi' });
  });

  it('listMessages unwraps .items array', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [messageItem] }), { status: 200 }),
    );
    const msgs = await listMessages('tok', 'c1');
    expect(msgs).toEqual([messageItem]);
  });

  it('suggestReply hits /conversations/{id}/suggest-reply with no body', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(suggestionPayload), { status: 200 }),
    );
    const out = await suggestReply('tok', 'c1');
    expect(out).toEqual(suggestionPayload);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('http://localhost:8000/api/v1/agents/conversations/c1/suggest-reply');
    expect(init.method).toBe('POST');
    expect(init.body).toBeUndefined();
  });
});
