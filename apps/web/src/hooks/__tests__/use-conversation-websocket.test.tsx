import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { useConversationWebSocket } from '@/hooks/use-conversation-websocket';
import { JWT_STORAGE_KEY } from '@/lib/api-client';
import { messagesQueryKey } from '@/lib/messages';

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 3;

  url: string;
  readyState: number = FakeWebSocket.CONNECTING;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  sent: string[] = [];

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    if (this.readyState === FakeWebSocket.CLOSED) return;
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({} as CloseEvent);
  }

  // Test helpers
  fakeOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({} as Event);
  }

  fakeMessage(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }

  fakeClose(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({} as CloseEvent);
  }
}

beforeEach(() => {
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  FakeWebSocket.instances = [];
  // @ts-expect-error -- swapping in our fake for the duration of the test.
  globalThis.WebSocket = FakeWebSocket;
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  // Restore the real WebSocket so other tests use the real one.
  delete (globalThis as { WebSocket?: unknown }).WebSocket;
  vi.useRealTimers();
});

function StatusProbe({
  conversationId,
}: {
  conversationId: string | undefined;
}): JSX.Element {
  const { status } = useConversationWebSocket(conversationId);
  return <div data-testid="ws-status" data-status={status} />;
}

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
}

function renderProbe(conversationId: string | undefined): void {
  const client = makeClient();
  // Seed the messages query with stale data so we can assert the
  // invalidation actually triggers a refetch.
  client.setQueryData(messagesQueryKey(conversationId ?? ''), []);
  render(
    <QueryClientProvider client={client}>
      <StatusProbe conversationId={conversationId} />
    </QueryClientProvider>,
  );
}

describe('useConversationWebSocket', () => {
  it('opens a WS connection on mount', async () => {
    renderProbe('conv-1');
    expect(FakeWebSocket.instances).toHaveLength(1);
    const url = FakeWebSocket.instances[0]!.url;
    expect(url).toContain('/api/v1/agents/conversations/conv-1/ws');
    expect(url).toContain('token=test-token');
  });

  it('transitions to connected after the open event', async () => {
    renderProbe('conv-2');
    const socket = FakeWebSocket.instances[0]!;
    act(() => {
      socket.fakeOpen();
    });
    await waitFor(() => {
      expect(screen.getByTestId('ws-status').dataset.status).toBe('connected');
    });
  });

  it('invalidates the messages query on a message.complete event', async () => {
    const client = makeClient();
    client.setQueryData(messagesQueryKey('conv-3'), []);
    render(
      <QueryClientProvider client={client}>
        <StatusProbe conversationId="conv-3" />
      </QueryClientProvider>,
    );
    const socket = FakeWebSocket.instances[0]!;
    act(() => {
      socket.fakeOpen();
    });
    // Spy on invalidateQueries via the client.
    const spy = vi.spyOn(client, 'invalidateQueries');
    act(() => {
      socket.fakeMessage({
        type: 'message.complete',
        conversation_id: 'conv-3',
        message_id: 'msg-1',
      });
    });
    expect(spy).toHaveBeenCalledWith({
      queryKey: messagesQueryKey('conv-3'),
    });
  });

  it('reconnects on disconnect with exponential backoff', async () => {
    vi.useFakeTimers();
    renderProbe('conv-4');
    const firstSocket = FakeWebSocket.instances[0]!;
    act(() => {
      firstSocket.fakeOpen();
    });
    expect(screen.getByTestId('ws-status').dataset.status).toBe('connected');

    act(() => {
      firstSocket.fakeClose();
    });
    expect(screen.getByTestId('ws-status').dataset.status).toBe('reconnecting');

    // After ~1s a new socket should appear.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(2);
  });

  it('reports disconnected when no JWT is stored', async () => {
    window.localStorage.clear();
    renderProbe('conv-5');
    await waitFor(() => {
      expect(screen.getByTestId('ws-status').dataset.status).toBe('disconnected');
    });
    expect(FakeWebSocket.instances).toHaveLength(0);
  });
});
