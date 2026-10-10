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
