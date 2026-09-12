import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { JWT_STORAGE_KEY } from '@/lib/api-client';
import { messagesQueryKey } from '@/lib/messages';

/**
 * WS connection lifecycle for the agent workspace conversation pane.
 *
 * Connects to ``/api/v1/agents/conversations/{id}/ws`` with the agent
 * JWT passed via ``?token=`` (WebSocket clients cannot set arbitrary
 * headers, and the backend endpoint is documented in
 * ``apps/api/src/agent/ws.py``).
 *
 * On every ``message.complete`` / ``message.created`` frame the hook
 * invalidates the messages query so the message stream picks up the
 * new row via the canonical REST fetch (idempotent — the manager
 * uses the persisted message_id so dedupe is trivial client-side).
 *
 * Reconnect strategy
 * ------------------
 * Exponential backoff: 1s, 2s, 4s, 8s, ... capped at 30s. Resets on
 * any successful connection. Capped at ``MAX_RECONNECTS`` so a
 * long-running outage doesn't pin the browser with a reconnect
 * storm — after the cap the hook surfaces ``disconnected`` and
 * stops trying. The user can trigger a manual reconnect by
 * remounting (e.g. navigating away and back).
 */
export type ConversationWebSocketStatus =
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'disconnected';

export interface UseConversationWebSocketOptions {
  /**
   * Called whenever a non-heartbeat WS frame arrives. Useful for
   * components that want to show a "new message" toast.
   */
  onMessage?: (event: ConversationWebSocketEvent) => void;
  /**
   * Disable the connection entirely (e.g. the conversation id is
   * not yet known). Defaults to ``true``.
   */
  enabled?: boolean;
}

export interface ConversationWebSocketEvent {
  type: string;
  conversation_id?: string;
  message_id?: string;
  role?: string;
  content?: string;
  sender_id?: string | null;
}

export interface UseConversationWebSocketResult {
  status: ConversationWebSocketStatus;
}

const INITIAL_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;
const MAX_RECONNECTS = 8;
const HEARTBEAT_INTERVAL_MS = 25_000;

/**
 * Resolve the WS URL for ``conversationId``.
 *
 * Mirrors the existing widget endpoint pattern (``?token=`` query).
 * Vite's dev server proxies ``/api`` to ``localhost:8000`` so we
 * can use a relative URL — that avoids CORS noise during dev and
 * keeps the URL stable across ``VITE_API_BASE_URL`` changes.
 */
function buildWsUrl(conversationId: string, token: string): string {
  const base = (import.meta.env.VITE_API_BASE_URL as string | undefined) ??
    window.location.origin;
  const url = new URL(`/api/v1/agents/conversations/${conversationId}/ws`, base);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('token', token);
  return url.toString();
}

export function useConversationWebSocket(
  conversationId: string | undefined,
  options: UseConversationWebSocketOptions = {},
): UseConversationWebSocketResult {
  const queryClient = useQueryClient();
  const onMessageRef = useRef<typeof options.onMessage>(options.onMessage);
  onMessageRef.current = options.onMessage;

  const [status, setStatus] = useState<ConversationWebSocketStatus>(
    'connecting',
  );

  useEffect(() => {
    if (!conversationId || options.enabled === false) {
      setStatus('disconnected');
      return;
    }
    const token = window.localStorage.getItem(JWT_STORAGE_KEY);
    if (!token) {
      setStatus('disconnected');
      return;
    }

    let cancelled = false;
    let socket: WebSocket | null = null;
    let reconnectAttempt = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let heartbeatTimer: ReturnType<typeof setInterval> | null = null;

    const clearHeartbeat = (): void => {
      if (heartbeatTimer !== null) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
    };

    const scheduleReconnect = (): void => {
      if (cancelled) return;
      if (reconnectAttempt >= MAX_RECONNECTS) {
        setStatus('disconnected');
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
    };

    const handleFrame = (raw: string): void => {
      let frame: ConversationWebSocketEvent;
      try {
        frame = JSON.parse(raw) as ConversationWebSocketEvent;
      } catch {
        return;
      }
      if (frame.type === 'message.complete' || frame.type === 'message.created') {
        if (conversationId) {
          void queryClient.invalidateQueries({
            queryKey: messagesQueryKey(conversationId),
          });
          void queryClient.invalidateQueries({
            queryKey: ['conversation', conversationId],
          });
        }
      }
      onMessageRef.current?.(frame);
    };

    const connect = (): void => {
      if (cancelled) return;
      try {
        socket = new WebSocket(buildWsUrl(conversationId, token));
      } catch {
        scheduleReconnect();
        return;
      }
      socket.onopen = (): void => {
        if (cancelled) return;
        reconnectAttempt = 0;
        setStatus('connected');
        // Heartbeat: keep the connection alive through corporate
        // proxies that drop idle sockets. Server ignores ping frames
        // (it just needs the read loop to stay open).
        clearHeartbeat();
        heartbeatTimer = setInterval(() => {
          if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ type: 'ping' }));
          }
        }, HEARTBEAT_INTERVAL_MS);
      };
      socket.onmessage = (event: MessageEvent<string>): void => {
        handleFrame(event.data);
      };
      socket.onerror = (): void => {
        // ``onerror`` always precedes ``onclose``; let onclose drive
        // reconnect so we don't double-schedule.
      };
      socket.onclose = (): void => {
        clearHeartbeat();
        if (!cancelled) {
          scheduleReconnect();
        }
      };
    };

    connect();

    return (): void => {
      cancelled = true;
      clearHeartbeat();
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        if (
          socket.readyState === WebSocket.OPEN ||
          socket.readyState === WebSocket.CONNECTING
        ) {
          socket.close();
        }
      }
    };
  }, [conversationId, queryClient, options.enabled]);

  return { status };
}
