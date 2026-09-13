// @vitest-environment node
import { describe, expect, it } from 'vitest';

import { DEV_PROXY } from '@/lib/dev-proxy';

/**
 * Stage 9.10 — defensive hardening for the dev server's reverse proxy.
 *
 * Two contracts that downstream tests / pages depend on:
 *  1. HTTP requests to ``/api`` from the Vite SPA must be proxied to
 *     the FastAPI backend on port 8000 in dev.
 *  2. WebSocket upgrades to ``/api/v1/widget/ws`` must also be proxied
 *     so the embedded widget works against the local API without
 *     hard-coding an origin allowlist for the dev port.
 *
 * The proxy config is exported from ``src/lib/dev-proxy.ts`` so this
 * test can import it without booting Vite's plugin pipeline (esbuild +
 * react), which trips a TextEncoder invariant under jsdom. Vite's own
 * config re-exports the same object.
 */
describe('vite dev proxy', () => {
  it('routes HTTP /api to the backend on port 8000 by default', () => {
    const api = DEV_PROXY['/api'];
    expect(api).toBeDefined();
    if (!api) return;
    expect(api.target).toBe('http://localhost:8000');
  });

  it('forwards the widget WS upgrade via ws: true', () => {
    const ws = DEV_PROXY['/api/v1/widget/ws'];
    expect(ws).toBeDefined();
    if (!ws) return;
    expect(ws.ws).toBe(true);
    // Default dev WS target mirrors the HTTP target's host but uses
    // the ws:// scheme so vite forwards the upgrade.
    expect(ws.target).toBe('ws://localhost:8000');
  });
});