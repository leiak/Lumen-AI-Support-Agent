import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createWsClient } from '../ws-client.js';

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
    this.onclose?.({ code: 1000, reason: '' } as CloseEvent);
  }

  fakeOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({} as Event);
  }

  fakeMessage(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }

  fakeClose(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code: 1006, reason: '' } as CloseEvent);
  }
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  // @ts-expect-error -- swapping in our fake for the duration of the test.
  globalThis.WebSocket = FakeWebSocket;
});

afterEach(() => {
  delete (globalThis as { WebSocket?: unknown }).WebSocket;
  vi.useRealTimers();
});

describe('createWsClient', () => {
  it('test_ws_client_connects_to_correct_url', () => {
    const client = createWsClient({
      url: 'wss://api.example.com/api/v1/widget/ws?token=abc',
      webSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
      heartbeatMs: 0,
    });
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.instances[0]!.url).toBe(
      'wss://api.example.com/api/v1/widget/ws?token=abc',
    );
    expect(client.url).toBe(
      'wss://api.example.com/api/v1/widget/ws?token=abc',
    );
    client.close();
  });

  it('test_ws_client_reconnects_on_disconnect', async () => {
    vi.useFakeTimers();
    const client = createWsClient({
      url: 'wss://api.example.com/api/v1/widget/ws?token=abc',
      webSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
      heartbeatMs: 0,
    });
    const first = FakeWebSocket.instances[0]!;
    first.fakeOpen();
    expect(client.status).toBe('open');

    first.fakeClose();
    expect(client.status).toBe('reconnecting');

    // Advance ~1s for the first backoff tick.
    await vi.advanceTimersByTimeAsync(1_100);
    expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(2);

    client.close();
  });

  it('test_ws_client_dispatches_message_event', () => {
    const client = createWsClient({
      url: 'wss://api.example.com/api/v1/widget/ws?token=abc',
      webSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
      heartbeatMs: 0,
    });
    const received: unknown[] = [];
    client.on('message', (payload) => {
      received.push(payload);
    });
    const socket = FakeWebSocket.instances[0]!;
    socket.fakeOpen();
    socket.fakeMessage({ type: 'message', text: 'hello' });
    socket.fakeMessage({ type: 'pong' });
    expect(received).toHaveLength(2);
    expect(received[0]).toEqual({ type: 'message', text: 'hello' });
    expect(received[1]).toEqual({ type: 'pong' });

    client.close();
  });
});
