import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { bootIframe } from '../main.js';

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
}

function dispatchInit(targetWindow: Window, config: Record<string, unknown>): void {
  targetWindow.dispatchEvent(
    new MessageEvent('message', {
      data: { type: 'init', config },
    }),
  );
}

beforeEach(() => {
  document.body.innerHTML = '';
  FakeWebSocket.instances = [];
  // jsdom makes window.parent === window; provide a no-op parent for the
  // iframe's postMessage calls.
  Object.defineProperty(window, 'parent', {
    configurable: true,
    value: { postMessage: vi.fn() },
  });
  // @ts-expect-error -- test fake
  globalThis.WebSocket = FakeWebSocket;
});

afterEach(() => {
  delete (globalThis as { WebSocket?: unknown }).WebSocket;
});

describe('bootIframe', () => {
  it('mounts chat + header on init', () => {
    const handle = bootIframe({
      documentRef: document,
      windowRef: window,
      webSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
    });

    dispatchInit(window, {
      apiBaseUrl: 'https://api.example.com',
      channelId: 'chan_1',
      tenantId: 'tenant_a',
      widgetToken: 'jwt.token.here',
      title: 'Need help?',
      subtitle: 'Subtitle',
      externalUserId: 'visitor-1',
    });

    expect(document.querySelector('[data-lumen-iframe="header"]')).toBeTruthy();
    expect(document.querySelector('[data-lumen-iframe="header-title"]')!.textContent).toBe(
      'Need help?',
    );
    expect(document.querySelector('[data-lumen-iframe="chat"]')).toBeTruthy();
    expect(document.querySelector('[data-lumen-iframe="composer"]')).toBeTruthy();
    expect(document.querySelector('[data-lumen-iframe="footer"]')!.textContent).toBe(
      'Powered by Lumen',
    );
    handle.destroy();
  });

  it('receives config via postMessage', () => {
    const onMessageSpy = vi.fn();
    window.addEventListener('message', onMessageSpy);

    const handle = bootIframe({
      documentRef: document,
      windowRef: window,
      webSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
    });

    // Before init the document should be untouched (no chat/header yet).
    expect(document.querySelector('[data-lumen-iframe="chat"]')).toBeNull();

    dispatchInit(window, {
      apiBaseUrl: 'https://api.example.com',
      channelId: 'chan_1',
      tenantId: 'tenant_a',
      widgetToken: 'jwt.token.here',
      title: 'Need help?',
      subtitle: 'Subtitle',
      externalUserId: 'visitor-1',
    });

    // After init the chat panel is mounted.
    expect(document.querySelector('[data-lumen-iframe="chat"]')).toBeTruthy();
    // Second init is ignored — listener is detached.
    dispatchInit(window, {
      apiBaseUrl: 'https://api.example.com',
      channelId: 'chan_1',
      tenantId: 'tenant_a',
      widgetToken: 'jwt.token.here',
      title: 'Different title',
      subtitle: 'Subtitle',
      externalUserId: 'visitor-1',
    });
    expect(document.querySelector('[data-lumen-iframe="header-title"]')!.textContent).toBe(
      'Need help?',
    );

    handle.destroy();
    window.removeEventListener('message', onMessageSpy);
  });

  it('initializes WS with token from config', () => {
    const handle = bootIframe({
      documentRef: document,
      windowRef: window,
      webSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
    });

    dispatchInit(window, {
      apiBaseUrl: 'https://api.example.com',
      channelId: 'chan_1',
      tenantId: 'tenant_a',
      widgetToken: 'jwt.token.here',
      title: 't',
      subtitle: 's',
      externalUserId: 'visitor-1',
    });

    expect(FakeWebSocket.instances).toHaveLength(1);
    const ws = FakeWebSocket.instances[0]!;
    expect(ws.url).toContain('wss://api.example.com/api/v1/widget/ws');
    expect(ws.url).toContain('token=jwt.token.here');

    // The real WebSocket client refuses to send until the socket is OPEN.
    ws.fakeOpen();

    // Outgoing customer message should land on the socket with the right
    // shape (this is the contract the backend widget router parses).
    const form = document.querySelector<HTMLFormElement>(
      '[data-lumen-iframe="composer"]',
    )!;
    const input = document.querySelector<HTMLTextAreaElement>(
      '[data-lumen-iframe="composer-input"]',
    )!;
    input.value = 'hello world';
    form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));

    expect(ws.sent).toHaveLength(1);
    const frame = JSON.parse(ws.sent[0]!);
    expect(frame.type).toBe('message');
    expect(frame.text).toBe('hello world');
    expect(typeof frame.client_message_id).toBe('string');

    handle.destroy();
  });
});