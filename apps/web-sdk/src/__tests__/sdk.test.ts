import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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
}

function makeConfig(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    apiBaseUrl: 'https://api.example.com',
    channelId: 'chan_123',
    tenantId: 'tenant_abc',
    ...overrides,
  };
}

function mockTokenFetch(impl: ReturnType<typeof vi.fn>): typeof fetch {
  return impl as unknown as typeof fetch;
}

function makeTokenFetch(): ReturnType<typeof vi.fn> {
  return vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        token: 'jwt.token.here',
        expires_at: new Date(Date.now() + 30 * 60 * 1000).toISOString(),
        expires_in: 1800,
      }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    ),
  );
}

async function flushPromises(): Promise<void> {
  // Drain the full microtask queue plus a real-timer tick so the SDK's
  // async init (fetch → mount DOM → emit ready) has time to settle.
  // Multiple awaits are necessary because `await fetch(...)` resolves
  // through several microtask hops before reaching the frame mount.
  for (let i = 0; i < 10; i++) {
    await Promise.resolve();
  }
  await new Promise((r) => setTimeout(r, 0));
}

interface BootHandle {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- dynamic import escape
  boot: any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- dynamic import escape
  api: any;
}

async function loadFreshBoot(): Promise<BootHandle> {
  // Each call gets a fresh module so module-level state is reset.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- dynamic import escape
  const mod: any = await import('../sdk.js');
  const api = mod.boot();
  return { boot: mod.boot, api };
}

beforeEach(() => {
  window.localStorage.clear();
  document.body.innerHTML = '';
  FakeWebSocket.instances = [];
  // @ts-expect-error -- test fake
  globalThis.WebSocket = FakeWebSocket;
  // Drop the SDK module so the next dynamic import in the test body
  // gets a fresh module-level `state`.
  vi.resetModules();
});

afterEach(() => {
  delete (globalThis as { WebSocket?: unknown }).WebSocket;
});

describe('SDK', () => {
  it('test_sdk_reads_window_config_on_init', async () => {
    (window as unknown as Record<string, unknown>)['LumenAICustomerConfig'] =
      makeConfig({ title: '需要帮助?' });

    const fetchMock = makeTokenFetch();
    globalThis.fetch = mockTokenFetch(fetchMock);

    const { api } = await loadFreshBoot();
    api.init();
    await flushPromises();

    const btn = document.querySelector<HTMLButtonElement>(
      '[data-lumen-widget="button"]',
    );
    expect(btn).toBeTruthy();
    expect(btn!.textContent).toBe('需');

    api.destroy();
  });

  it('test_sdk_calls_token_endpoint_with_tenant_id', async () => {
    (window as unknown as Record<string, unknown>)['LumenAICustomerConfig'] =
      makeConfig();

    const fetchMock = makeTokenFetch();
    const originalFetch = globalThis.fetch;
    globalThis.fetch = mockTokenFetch(fetchMock);
    try {
      const { api } = await loadFreshBoot();
      api.init();
      await flushPromises();
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0]!;
      expect(url).toBe('https://api.example.com/api/v1/widget/token');
      const body = JSON.parse((init as RequestInit).body as string);
      expect(body.channel_id).toBe('chan_123');
      // tenant_id is *not* in the request body — the server derives it
      // from the channel lookup. Assert it stays out of the wire payload
      // so the server-side cross-tenant check is authoritative.
      expect(body.tenant_id).toBeUndefined();
      expect(typeof body.external_user_id).toBe('string');
      api.destroy();
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('test_sdk_mounts_floating_button', async () => {
    (window as unknown as Record<string, unknown>)['LumenAICustomerConfig'] =
      makeConfig();
    globalThis.fetch = mockTokenFetch(makeTokenFetch());
    const { api } = await loadFreshBoot();
    api.init();
    await flushPromises();
    const btn = document.querySelector<HTMLElement>(
      '[data-lumen-widget="button"]',
    );
    expect(btn).toBeTruthy();
    expect(btn!.tagName).toBe('BUTTON');
    api.destroy();
  });

  it('test_sdk_opens_iframe_on_button_click', async () => {
    (window as unknown as Record<string, unknown>)['LumenAICustomerConfig'] =
      makeConfig();
    globalThis.fetch = mockTokenFetch(makeTokenFetch());
    const { api } = await loadFreshBoot();
    api.init();
    await flushPromises();

    const btn = document.querySelector<HTMLButtonElement>(
      '[data-lumen-widget="button"]',
    )!;
    const wrapper = document.querySelector<HTMLElement>(
      '[data-lumen-widget="wrapper"]',
    )!;
    expect(wrapper.getAttribute('data-state')).toBe('closed');

    btn.click();
    expect(wrapper.getAttribute('data-state')).toBe('open');
    api.destroy();
  });

  it('test_sdk_closes_iframe_on_close_click', async () => {
    (window as unknown as Record<string, unknown>)['LumenAICustomerConfig'] =
      makeConfig();
    globalThis.fetch = mockTokenFetch(makeTokenFetch());
    const { api } = await loadFreshBoot();
    api.init();
    await flushPromises();

    const btn = document.querySelector<HTMLButtonElement>(
      '[data-lumen-widget="button"]',
    )!;
    const wrapper = document.querySelector<HTMLElement>(
      '[data-lumen-widget="wrapper"]',
    )!;
    btn.click();
    expect(wrapper.getAttribute('data-state')).toBe('open');

    const closeBtn = document.querySelector<HTMLButtonElement>(
      '[data-lumen-widget="close"]',
    )!;
    closeBtn.click();
    expect(wrapper.getAttribute('data-state')).toBe('closed');
    api.destroy();
  });

  it('test_sdk_persists_open_state_to_localStorage', async () => {
    (window as unknown as Record<string, unknown>)['LumenAICustomerConfig'] =
      makeConfig();
    globalThis.fetch = mockTokenFetch(makeTokenFetch());
    const { api } = await loadFreshBoot();
    api.init();
    await flushPromises();

    const btn = document.querySelector<HTMLButtonElement>(
      '[data-lumen-widget="button"]',
    )!;
    btn.click();
    expect(window.localStorage.getItem('lumen-widget:open')).toBe('1');

    const closeBtn = document.querySelector<HTMLButtonElement>(
      '[data-lumen-widget="close"]',
    )!;
    closeBtn.click();
    expect(window.localStorage.getItem('lumen-widget:open')).toBe('0');
    api.destroy();
  });
});
