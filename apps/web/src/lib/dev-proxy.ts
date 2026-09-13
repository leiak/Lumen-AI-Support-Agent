/**
 * Vite dev server reverse-proxy configuration.
 *
 * Kept in a standalone module so it can be unit-tested without booting
 * Vite's plugin pipeline (esbuild + @vitejs/plugin-react), which trips a
 * TextEncoder invariant under jsdom. ``vite.config.ts`` re-exports this
 * inside its ``server.proxy`` block; ``src/__tests__/proxy.test.ts``
 * asserts against the same object.
 *
 * Stage 9.10 added the WS proxy entry so the embedded widget can
 * reach ``/api/v1/widget/ws`` through the same dev server (Vite
 * forwards ``Upgrade: websocket`` when ``ws: true``).
 */
export type DevProxyTarget = {
  target: string;
  changeOrigin?: boolean;
  secure?: boolean;
  ws?: boolean;
};

export type DevProxyConfig = Record<string, DevProxyTarget>;

const DEFAULT_HTTP_TARGET = 'http://localhost:8000';
const DEFAULT_WS_TARGET = 'ws://localhost:8000';

export const DEV_PROXY: DevProxyConfig = {
  // HTTP — used by the agent SPA (login, agents/me, conversations, etc.).
  '/api': {
    target: process.env.VITE_API_BASE_URL || DEFAULT_HTTP_TARGET,
    changeOrigin: false,
    secure: false,
  },
  // WebSocket — the embedded widget's cross-origin stream must
  // reach the API's /api/v1/widget/ws endpoint. Vite's proxy
  // supports ``ws: true`` so the upgrade handshake is forwarded
  // rather than treated as plain HTTP.
  '/api/v1/widget/ws': {
    target: process.env.VITE_WS_BASE_URL || DEFAULT_WS_TARGET,
    ws: true,
    changeOrigin: false,
    secure: false,
  },
};