/**
 * SDK entry point.
 *
 * Lifecycle (matches the embed-time contract documented in README.md):
 *   1. Read `window.LumenAICustomerConfig` and validate.
 *   2. Resolve / generate a stable `externalUserId` (localStorage-backed).
 *   3. Mint a widget JWT via the token endpoint.
 *   4. Open the WebSocket to the widget gateway.
 *   5. Mount the floating button + iframe shell.
 *
 * The bundle is wrapped as an IIFE by esbuild with `globalName: LumenAICustomer`
 * which exposes the public API on `window.LumenAICustomer`:
 *
 *   { init, open, close, destroy }
 *
 * `init()` is also called automatically on script load. A second call is
 * a no-op.
 */
import { readConfig, type LumenConfig, type ResolvedConfig } from './config.js';
import {
  createWidgetFrame,
  type WidgetFrame,
} from './widget-frame.js';
import {
  mintWidgetToken,
  type TokenResponse,
} from './token-client.js';
import {
  createWsClient,
  type WsClient,
} from './ws-client.js';

export interface PublicApi {
  init(): void;
  open(): void;
  close(): void;
  destroy(): void;
  /** @internal test escape hatch. */
  _internal?: InternalState;
}

export interface InternalState {
  config: ResolvedConfig;
  token: TokenResponse | null;
  ws: WsClient | null;
  frame: WidgetFrame | null;
  documentRef: Document;
  windowRef: Window;
}

const GLOBAL_KEY = 'LumenAICustomer';
const VISITOR_KEY = 'lumen-widget:visitor';
const READY_EVENT = 'lumen-widget:ready';

let state: InternalState | null = null;

function buildWsUrl(apiBaseUrl: string, token: string): string {
  // apiBaseUrl is normalized to no trailing slash in readConfig.
  const url = new URL(`${apiBaseUrl}/api/v1/widget/ws`);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('token', token);
  return url.toString();
}

function resolveExternalUserId(
  config: ResolvedConfig,
  storage: Storage,
): string {
  if (typeof config.externalUserId === 'string' && config.externalUserId) {
    return config.externalUserId;
  }
  try {
    const existing = storage.getItem(VISITOR_KEY);
    if (existing) return existing;
  } catch {
    // localStorage may be unavailable; fall through to random id.
  }
  const generated = `visitor-${Math.random().toString(36).slice(2, 10)}-${Date.now().toString(36)}`;
  try {
    storage.setItem(VISITOR_KEY, generated);
  } catch {
    // ignore — best-effort persistence
  }
  return generated;
}

function mountHost(documentRef: Document): HTMLElement {
  const HOST_ID = 'lumen-widget-host';
  let host = documentRef.getElementById(HOST_ID);
  if (host) return host;
  host = documentRef.createElement('div');
  host.id = HOST_ID;
  documentRef.body.appendChild(host);
  return host;
}

function dispatchReady(windowRef: Window): void {
  try {
    windowRef.dispatchEvent(new Event(READY_EVENT));
  } catch {
    // ignore — best effort
  }
}

async function initInternal(
  documentRef: Document,
  windowRef: Window,
  configSource: unknown,
  overrides: {
    fetchImpl?: typeof fetch;
    webSocketImpl?: typeof WebSocket;
    skipWs?: boolean;
  } = {},
): Promise<void> {
  if (state) return;

  const config = readConfig(configSource);
  const externalUserId = resolveExternalUserId(
    config,
    windowRef.localStorage,
  );

  // Mint the JWT. Surface failures to the console — the floating button
  // still mounts so a returning visitor can retry by reopening.
  let token: TokenResponse | null = null;
  try {
    token = await mintWidgetToken(config, externalUserId, {
      ...(overrides.fetchImpl !== undefined
        ? { fetchImpl: overrides.fetchImpl }
        : {}),
    });
  } catch (err) {
    // eslint-disable-next-line no-console -- surfacing to embedding site owner
    console.warn('[Lumen widget] failed to mint widget token:', err);
  }

  const host = mountHost(documentRef);
  const frame = createWidgetFrame(
    documentRef,
    {
      config,
      widgetToken: token?.token ?? '',
      externalUserId,
    },
    host,
  );

  let ws: WsClient | null = null;
  if (token && !overrides.skipWs) {
    const wsUrl = buildWsUrl(config.apiBaseUrl, token.token);
    try {
      ws = createWsClient({
        url: wsUrl,
        ...(overrides.webSocketImpl !== undefined
          ? { webSocketImpl: overrides.webSocketImpl }
          : {}),
      });
    } catch (err) {
      // eslint-disable-next-line no-console -- surfacing to embedding site owner
      console.warn('[Lumen widget] failed to open WebSocket:', err);
    }
  }

  state = {
    config,
    token,
    ws,
    frame,
    documentRef,
    windowRef,
  };

  dispatchReady(windowRef);
}

/**
 * Public API exposed on `window.LumenAICustomer`.
 *
 * - `init()`  — idempotent; called automatically on script load.
 * - `open()`  — opens the chat window.
 * - `close()` — minimizes back to the floating button.
 * - `destroy()` — removes all DOM, closes the WS, and clears state.
 *
 * The boot path is two-step so tests can swap in a fake `WebSocket`
 * before the auto-init kicks in: esbuild's IIFE wrapper calls
 * `boot().init()` after returning control to the caller.
 */
function buildPublicApi(): PublicApi {
  return {
    init(): void {
      void initInternal(document, window, getConfigSource());
    },
    open(): void {
      state?.frame?.open();
    },
    close(): void {
      state?.frame?.close();
    },
    destroy(): void {
      if (!state) return;
      state.ws?.close();
      state.frame?.destroy();
      state = null;
    },
  };
}

function getConfigSource(): unknown {
  // The embedding site sets this *before* the script tag runs.
  return (window as unknown as Record<string, unknown>)[GLOBAL_KEY + 'Config'];
}

/**
 * Boot the SDK. Called automatically by the IIFE wrapper, and can be
 * called again (idempotent) from the embedding site.
 *
 * Does NOT auto-initialize; the IIFE wrapper is responsible for calling
 * `.init()` on the returned API. This lets tests swap in fake globals
 * before init runs.
 */
export function boot(): PublicApi {
  return buildPublicApi();
}

// Expose the config type for embedding-site consumers that want strict
// typing via TypeScript declarations.
export type { LumenConfig };
