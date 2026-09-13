/**
 * Vanilla WebSocket client with exponential-backoff reconnect.
 *
 * Mirrors the backend pattern in `apps/api/src/widget/ws/router.py`:
 *   - WS URL:  wss://{apiBaseUrl}/api/v1/widget/ws?token={jwt}
 *   - Incoming frames are JSON: { type, ... }
 *   - Outbound frames can be: { type: "message", text, ... }, "ping", "typing"
 *
 * The client emits events on a tiny listener surface (onopen / onmessage /
 * onclose / onerror) so callers can plug it into React-free UI updates.
 *
 * Reconnect strategy
 * ------------------
 * Exponential backoff: 1s, 2s, 4s, 8s, ... capped at 30s. Resets on a
 * successful open. Capped at MAX_RECONNECTS so a long outage doesn't
 * pin the browser. After the cap, the client surfaces an error and
 * stops trying until `.reconnect()` is called manually.
 */

export interface WsClientOptions {
  url: string;
  /** Override WebSocket constructor (for tests). */
  webSocketImpl?: typeof WebSocket;
  /** Heartbeat interval in ms; 0 disables. Defaults to 25s. */
  heartbeatMs?: number;
  /** Max reconnects before giving up. Defaults to 8. */
  maxReconnects?: number;
}

export type WsListener<T> = (event: T) => void;

export interface WsClient {
  readonly url: string;
  /** Current lifecycle status. */
  readonly status: WsStatus;
  on(event: 'open', listener: WsListener<void>): void;
  on(event: 'message', listener: WsListener<unknown>): void;
  on(event: 'close', listener: WsListener<{ code: number; reason: string }>): void;
  on(event: 'error', listener: WsListener<unknown>): void;
  off(event: 'open', listener: WsListener<void>): void;
  off(event: 'message', listener: WsListener<unknown>): void;
  off(event: 'close', listener: WsListener<{ code: number; reason: string }>): void;
  off(event: 'error', listener: WsListener<unknown>): void;
  /** Send a JSON-serialisable frame. Returns false if not currently open. */
  send(frame: unknown): boolean;
  /** Close the underlying socket and stop reconnecting. */
  close(): void;
  /** Force a reconnect attempt (resets the backoff counter). */
  reconnect(): void;
}

export type WsStatus =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'closed'
  | 'failed';

const INITIAL_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;
const DEFAULT_HEARTBEAT_MS = 25_000;
const DEFAULT_MAX_RECONNECTS = 8;

/**
 * No-op WebSocket used when the host environment has no WebSocket
 * constructor. Calls resolve to a permanent CONNECTING state and
 * silently swallows sends. Production code paths always have a real
 * WebSocket — this exists to keep unit tests + exotic embeds functional.
 */
class NoopWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  constructor(_url: string) {
    // intentionally empty
  }
  send(_data: string): void {
    // intentionally empty
  }
  close(): void {
    this.readyState = 3;
  }
}

export function createWsClient(options: WsClientOptions): WsClient {
  // WebSocket may be missing in some test environments or very old
  // browsers. Use a no-op shim so callers can still mount the SDK and
  // get an explicit status — better than throwing during init.
  const WS = options.webSocketImpl ?? globalThis.WebSocket ?? NoopWebSocket;
  const heartbeatMs = options.heartbeatMs ?? DEFAULT_HEARTBEAT_MS;
  const maxReconnects = options.maxReconnects ?? DEFAULT_MAX_RECONNECTS;

  let status: WsStatus = 'idle';
  let socket: WebSocket | null = null;
  let reconnectAttempt = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  let closedByUser = false;

  const listeners: {
    open: Set<WsListener<void>>;
    message: Set<WsListener<unknown>>;
    close: Set<WsListener<{ code: number; reason: string }>>;
    error: Set<WsListener<unknown>>;
  } = {
    open: new Set(),
    message: new Set(),
    close: new Set(),
    error: new Set(),
  };

  function setStatus(next: WsStatus): void {
    status = next;
  }

  function emit<K extends 'open' | 'message' | 'close' | 'error'>(
    event: K,
    payload: Parameters<WsClientListeners[K]>[0],
  ): void {
    for (const fn of listeners[event]) {
      try {
        (fn as (p: unknown) => void)(payload as unknown);
      } catch {
        // Listener exceptions must not break the WS lifecycle.
      }
    }
  }

  function clearHeartbeat(): void {
    if (heartbeatTimer !== null) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
  }

  function scheduleReconnect(): void {
    if (closedByUser) return;
    if (reconnectAttempt >= maxReconnects) {
      setStatus('failed');
      return;
    }
    reconnectAttempt += 1;
    const backoff = Math.min(
      INITIAL_BACKOFF_MS * 2 ** (reconnectAttempt - 1),
      MAX_BACKOFF_MS,
    );
    setStatus('reconnecting');
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, backoff);
  }

  function connect(): void {
    if (closedByUser) return;
    setStatus('connecting');
    try {
      socket = new WS(options.url);
    } catch (err) {
      emit('error', err);
      scheduleReconnect();
      return;
    }
    socket.onopen = (): void => {
      if (closedByUser) return;
      reconnectAttempt = 0;
      setStatus('open');
      clearHeartbeat();
      if (heartbeatMs > 0) {
        heartbeatTimer = setInterval(() => {
          if (socket && socket.readyState === WS.OPEN) {
            try {
              socket.send(JSON.stringify({ type: 'ping' }));
            } catch {
              // send failures are surfaced via onerror → onclose.
            }
          }
        }, heartbeatMs);
      }
      emit('open', undefined);
    };
    socket.onmessage = (event: MessageEvent<string>): void => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(event.data);
      } catch {
        // Non-JSON frame — surface as raw text.
        emit('message', event.data);
        return;
      }
      emit('message', parsed);
    };
    socket.onerror = (event: Event): void => {
      emit('error', event);
    };
    socket.onclose = (event: CloseEvent): void => {
      clearHeartbeat();
      socket = null;
      emit('close', { code: event.code, reason: event.reason });
      if (!closedByUser) {
        scheduleReconnect();
      } else {
        setStatus('closed');
      }
    };
  }

  function send(frame: unknown): boolean {
    if (!socket || socket.readyState !== WS.OPEN) return false;
    try {
      socket.send(JSON.stringify(frame));
      return true;
    } catch {
      return false;
    }
  }

  function close(): void {
    closedByUser = true;
    clearHeartbeat();
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (socket) {
      try {
        socket.close();
      } catch {
        // best-effort
      }
      socket = null;
    }
    setStatus('closed');
  }

  function reconnect(): void {
    closedByUser = false;
    reconnectAttempt = 0;
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (socket) {
      try {
        socket.close();
      } catch {
        // best-effort
      }
      socket = null;
    }
    connect();
  }

  connect();

  return {
    get url(): string {
      return options.url;
    },
    get status(): WsStatus {
      return status;
    },
    on(event, listener): void {
      const set = listeners[event] as Set<(payload: unknown) => void>;
      set.add(listener as (payload: unknown) => void);
    },
    off(event, listener): void {
      const set = listeners[event] as Set<(payload: unknown) => void>;
      set.delete(listener as (payload: unknown) => void);
    },
    send,
    close,
    reconnect,
  };
}

interface WsClientListeners {
  open: WsListener<void>;
  message: WsListener<unknown>;
  close: WsListener<{ code: number; reason: string }>;
  error: WsListener<unknown>;
}
