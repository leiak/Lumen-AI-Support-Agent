/**
 * Chat panel: scrollable message list + composer.
 *
 * Roles:
 *   customer (right, blue)  - user's own messages (optimistic on send)
 *   agent    (left,  green) - human agent replies via REST
 *   ai       (left,  purple) - AI auto-response
 *   system   (center, gray) - server events / hints
 *
 * Composer:
 *   - textarea with auto-grow (capped at max-height)
 *   - Enter sends, Shift+Enter inserts newline
 *   - send button is disabled while textarea is empty / whitespace-only
 *   - "需要人工" button triggers `onEscalate` callback
 *
 * All message text is assigned via textContent (XSS-safe). The element
 * scroll is forced to the bottom on every append so the latest message
 * is always visible.
 */
import type { ChatMessage, SenderRole } from './protocol.js';

export interface ChatHandle {
  el: HTMLElement;
  appendMessage(msg: ChatMessage): void;
  updateMessage(id: string, patch: Partial<ChatMessage>): void;
  /** Show / hide the "已转人工" hint banner. */
  setEscalated(escalated: boolean): void;
}

export interface ChatOptions {
  onSend: (text: string) => void;
  onEscalate: () => void;
}

export function createChat(
  documentRef: Document,
  options: ChatOptions,
): ChatHandle {
  const root = documentRef.createElement('section');
  root.setAttribute('data-lumen-iframe', 'chat');
  root.setAttribute('role', 'log');
  root.setAttribute('aria-live', 'polite');

  const list = documentRef.createElement('div');
  list.setAttribute('data-lumen-iframe', 'message-list');
  list.setAttribute('data-empty', 'true');

  const hint = documentRef.createElement('div');
  hint.setAttribute('data-lumen-iframe', 'escalate-hint');
  hint.textContent = '已转人工,客服稍后会联系您';
  hint.style.display = 'none';

  const composer = documentRef.createElement('form');
  composer.setAttribute('data-lumen-iframe', 'composer');

  const textarea = documentRef.createElement('textarea');
  textarea.setAttribute('data-lumen-iframe', 'composer-input');
  textarea.rows = 2;
  textarea.placeholder = '请输入您的问题...';

  const sendBtn = documentRef.createElement('button');
  sendBtn.type = 'submit';
  sendBtn.setAttribute('data-lumen-iframe', 'composer-send');
  sendBtn.textContent = '发送';
  sendBtn.disabled = true;

  const escalateBtn = documentRef.createElement('button');
  escalateBtn.type = 'button';
  escalateBtn.setAttribute('data-lumen-iframe', 'composer-escalate');
  escalateBtn.textContent = '需要人工';

  const updateSendState = (): void => {
    sendBtn.disabled = textarea.value.trim().length === 0;
  };

  textarea.addEventListener('input', updateSendState);
  textarea.addEventListener('keydown', (e: KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!sendBtn.disabled) {
        composer.requestSubmit();
      }
    }
  });

  composer.addEventListener('submit', (e: Event) => {
    e.preventDefault();
    const text = textarea.value.trim();
    if (!text) return;
    options.onSend(text);
    textarea.value = '';
    updateSendState();
  });

  escalateBtn.addEventListener('click', () => {
    options.onEscalate();
  });

  composer.appendChild(textarea);
  composer.appendChild(sendBtn);
  composer.appendChild(escalateBtn);

  root.appendChild(list);
  root.appendChild(hint);
  root.appendChild(composer);

  function appendMessage(msg: ChatMessage): void {
    const bubble = documentRef.createElement('div');
    bubble.setAttribute('data-lumen-iframe', 'message');
    bubble.setAttribute('data-role', msg.role);
    bubble.setAttribute('data-id', msg.id);
    bubble.setAttribute('data-status', msg.status);
    bubble.textContent = msg.text;
    list.appendChild(bubble);
    list.setAttribute('data-empty', 'false');
    // Defer to next frame so the browser has applied the layout, then snap
    // to the bottom. Doing this synchronously inside appendChild works on
    // some engines but not jsdom (no layout pass).
    requestAnimationFrame(() => scrollToBottom());
  }

  function updateMessage(id: string, patch: Partial<ChatMessage>): void {
    const bubble = list.querySelector<HTMLElement>(
      `[data-lumen-iframe="message"][data-id="${cssEscape(id)}"]`,
    );
    if (!bubble) return;
    if (patch.text !== undefined) bubble.textContent = patch.text;
    if (patch.status !== undefined) bubble.setAttribute('data-status', patch.status);
    if (patch.role !== undefined) bubble.setAttribute('data-role', patch.role);
  }

  function scrollToBottom(): void {
    list.scrollTop = list.scrollHeight;
  }

  return {
    el: root,
    appendMessage,
    updateMessage,
    setEscalated(escalated: boolean) {
      hint.style.display = escalated ? 'block' : 'none';
    },
  };
}

/**
 * Escape a string for use inside an attribute selector. Falls back to a
 * regex-based escaper if `CSS.escape` is unavailable (older browsers).
 */
function cssEscape(s: string): string {
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
    return CSS.escape(s);
  }
  return s.replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`);
}

/** Exported for tests — not part of the public surface. */
export const _internal = {
  bubbleSelector(role: SenderRole): string {
    return `[data-lumen-iframe="message"][data-role="${role}"]`;
  },
};