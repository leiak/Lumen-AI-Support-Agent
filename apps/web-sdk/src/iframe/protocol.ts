/**
 * Shared types for the iframe UI.
 *
 * postMessage protocol (parent SDK <-> iframe)
 * ------------------------------------------
 * Parent -> iframe: { type: 'init', config: IframeConfig }
 * Iframe -> parent: { type: 'ready' }                // sent once after mount
 * Iframe -> parent: { type: 'close' }                // user clicked X
 *
 * The iframe only accepts `init` messages. All other inbound postMessage
 * events are ignored. The iframe never reads anything off `window.parent`
 * (e.g. token, config) directly — everything is plumbed via the postMessage
 * handshake so the iframe stays decoupled from the embedding site's globals.
 *
 * WS frames (mirror `apps/api/src/widget/ws/router.py`)
 * ------------------------------------------------------
 *   Outgoing (iframe -> server):
 *     { type: 'message', text, client_message_id, conversation_id? }
 *     { type: 'ping' }
 *     { type: 'typing' }   // ignored by server for M1
 *
 *   Incoming (server -> iframe):
 *     { type: 'ack', external_message_id }            // server confirms our message
 *     { type: 'pong' }                                // heartbeat reply
 *     { type: 'message.delta', conversation_id, text }
 *                                                    // streamed AI text chunk; bubbles
 *                                                    // are keyed by conversation_id
 *     { type: 'message.complete', conversation_id, message_id, role, content }
 *                                                    // AI auto-response — finalises the
 *                                                    // streamed bubble for conversation_id
 *     { type: 'message.created', conversation_id, message_id, role: 'agent', sender_id }
 *                                                    // human agent replied via REST
 *                                                    // (no `content` field — server
 *                                                    //  signals existence only)
 *     { type: 'error', detail }
 *
 * The customer-side `message` echo from `widget.adapter.WebWidgetAdapter.
 * send_outbound` is not currently broadcast for M1 — customer messages are
 * optimistically rendered on send and confirmed by `ack`.
 */
export interface IframeConfig {
  apiBaseUrl: string;
  channelId: string;
  tenantId: string;
  widgetToken: string;
  accentColor?: string;
  title: string;
  subtitle: string;
  externalUserId: string;
  locale?: string;
}

export type ParentMessage = { type: 'init'; config: IframeConfig };
export type IframeMessage =
  | { type: 'ready' }
  | { type: 'close' };

export type SenderRole = 'customer' | 'agent' | 'ai' | 'system';

export interface ChatMessage {
  id: string;
  role: SenderRole;
  text: string;
  createdAt: number;
  status: 'pending' | 'sent' | 'failed';
}

export function isIframeConfig(value: unknown): value is IframeConfig {
  if (!value || typeof value !== 'object') return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.apiBaseUrl === 'string' &&
    v.apiBaseUrl.length > 0 &&
    typeof v.channelId === 'string' &&
    v.channelId.length > 0 &&
    typeof v.tenantId === 'string' &&
    v.tenantId.length > 0 &&
    typeof v.widgetToken === 'string' &&
    typeof v.title === 'string' &&
    typeof v.subtitle === 'string' &&
    typeof v.externalUserId === 'string'
  );
}