/**
 * Iframe entry point.
 *
 * Lifecycle:
 *   1. Wait for parent SDK to send `{type:'init', config}` via postMessage.
 *   2. Validate the config and mount header + chat into the iframe document.
 *   3. Open the WebSocket using `config.widgetToken` (the token is passed
 *      via postMessage — never read from `parent.*` directly).
 *   4. Wire WS events to the chat panel (render inbound messages, send
 *      outbound on user action).
 *
 * The WS client is the same `createWsClient` used by the parent SDK —
 * esbuild bundles a private copy into the iframe build.
 */
import { createWsClient, type WsClient } from '../ws-client.js';
import { createChat } from './chat.js';
import { createHeader } from './header.js';
import {
  isIframeConfig,
  type ChatMessage,
  type IframeConfig,
} from './protocol.js';
import { injectStyles } from './styles.js';

export interface IframeHandle {
  destroy(): void;
}

export interface BootIframeOptions {
  documentRef: Document;
  windowRef: Window;
  /** Override `WebSocket` constructor (for tests). */
  webSocketImpl?: typeof WebSocket;
}

function buildWsUrl(apiBaseUrl: string, token: string): string {
  const url = new URL(`${apiBaseUrl.replace(/\/+$/, '')}/api/v1/widget/ws`);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('token', token);
  return url.toString();
}

function newMessageId(): string {
  return `m-${Math.random().toString(36).slice(2, 10)}-${Date.now().toString(36)}`;
}

/**
 * Boot the iframe. Returns a handle with a destroy() method for tests
 * and any host that wants to tear down the iframe programmatically.
 *
 * Until `init()` is called the iframe document is left empty — the
 * message listener is attached eagerly so we don't miss the parent's
 * init postMessage.
 */
export function bootIframe(options: BootIframeOptions): IframeHandle {
  const { documentRef, windowRef } = options;

  let ws: WsClient | null = null;
  let header: ReturnType<typeof createHeader> | null = null;
  let chat: ReturnType<typeof createChat> | null = null;

  function handleSend(text: string): void {
    if (!chat) return;
    const id = newMessageId();
    chat.appendMessage({
      id,
      role: 'customer',
      text,
      createdAt: Date.now(),
      status: 'pending',
    });
    if (ws) {
      const ok = ws.send({
        type: 'message',
        text,
        client_message_id: id,
      });
      if (!ok) {
        chat.updateMessage(id, { status: 'failed' });
      }
    } else {
      chat.updateMessage(id, { status: 'failed' });
    }
  }

  function handleEscalate(): void {
    if (!chat) return;
    chat.setEscalated(true);
    if (ws) {
      ws.send({
        type: 'message',
        text: '[需要人工服务]',
        client_message_id: newMessageId(),
      });
    }
  }

  function renderServerMessage(payload: Record<string, unknown>): void {
    if (!chat) return;
    const role = typeof payload.role === 'string' ? payload.role : 'system';
    const text = typeof payload.content === 'string' ? payload.content : '';
    let displayRole: ChatMessage['role'];
    let displayText: string;
    if (role === 'ai') {
      displayRole = 'ai';
      displayText = text;
    } else if (role === 'agent') {
      displayRole = 'agent';
      // The `message.created` broadcast carries no `content` field for M1
      // (the agent workspace pushes REST payloads, not WS payloads).
      // Surface a hint so the customer knows a human responded.
      displayText = text || '[客服回复]';
    } else {
      displayRole = 'system';
      displayText = text || '[系统消息]';
    }
    chat.appendMessage({
      id: typeof payload.message_id === 'string' ? payload.message_id : newMessageId(),
      role: displayRole,
      text: displayText,
      createdAt: Date.now(),
      status: 'sent',
    });
  }

  function confirmLastPending(): void {
    if (!chat) return;
    const pending = chat.el.querySelector<HTMLElement>(
      '[data-lumen-iframe="message"][data-role="customer"][data-status="pending"]:last-of-type',
    );
    if (pending) pending.setAttribute('data-status', 'sent');
  }

  function handleFrame(payload: unknown): void {
    if (!payload || typeof payload !== 'object') return;
    const frame = payload as Record<string, unknown>;
    const type = frame.type;
    if (type === 'ack') {
      confirmLastPending();
    } else if (type === 'message.complete' || type === 'message.created') {
      renderServerMessage(frame);
    }
    // `pong` and `error` are swallowed — the WS client tracks reconnect
    // state and surfaces errors via its own listener surface.
  }

  function init(config: IframeConfig): void {
    injectStyles(documentRef, config.accentColor);
    // Reset the document so a second init (shouldn't happen, but defensively)
    // doesn't stack duplicates.
    documentRef.body.innerHTML = '';

    header = createHeader(documentRef, config, windowRef);
    chat = createChat(documentRef, {
      onSend: handleSend,
      onEscalate: handleEscalate,
    });

    const footer = documentRef.createElement('div');
    footer.setAttribute('data-lumen-iframe', 'footer');
    footer.textContent = 'Powered by Lumen';

    documentRef.body.appendChild(header.el);
    documentRef.body.appendChild(chat.el);
    documentRef.body.appendChild(footer);

    const url = buildWsUrl(config.apiBaseUrl, config.widgetToken);
    ws = createWsClient({
      url,
      ...(options.webSocketImpl !== undefined
        ? { webSocketImpl: options.webSocketImpl }
        : {}),
    });
    ws.on('message', handleFrame);

    // Signal to parent that we're ready (the parent also kicks off init
    // via iframe.onload, so this is mostly informational / for tests).
    try {
      windowRef.parent?.postMessage({ type: 'ready' }, '*');
    } catch {
      // best effort
    }
  }

  function onMessage(event: MessageEvent): void {
    const data = event.data;
    if (!data || typeof data !== 'object') return;
    const msg = data as Record<string, unknown>;
    if (msg.type !== 'init') return;
    if (!isIframeConfig(msg.config)) return;
    init(msg.config);
    windowRef.removeEventListener('message', onMessage);
  }

  windowRef.addEventListener('message', onMessage);

  return {
    destroy(): void {
      ws?.close();
      ws = null;
      windowRef.removeEventListener('message', onMessage);
    },
  };
}