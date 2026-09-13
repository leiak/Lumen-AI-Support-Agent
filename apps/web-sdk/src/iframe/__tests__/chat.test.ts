import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createChat } from '../chat.js';
import type { ChatMessage } from '../protocol.js';

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('createChat', () => {
  it('renders customer / agent / ai message bubbles correctly', () => {
    const chat = createChat(document, { onSend: () => {}, onEscalate: () => {} });

    const messages: ChatMessage[] = [
      { id: 'm1', role: 'customer', text: 'hi from customer', createdAt: 1, status: 'sent' },
      { id: 'm2', role: 'agent', text: 'agent reply', createdAt: 2, status: 'sent' },
      { id: 'm3', role: 'ai', text: 'ai reply', createdAt: 3, status: 'sent' },
      { id: 'm4', role: 'system', text: 'system note', createdAt: 4, status: 'sent' },
    ];
    for (const m of messages) {
      chat.appendMessage(m);
    }

    document.body.appendChild(chat.el);

    for (const m of messages) {
      const sel = `[data-lumen-iframe="message"][data-id="${m.id}"][data-role="${m.role}"]`;
      const el = document.querySelector(sel);
      expect(el, `expected bubble for ${m.role}`).toBeTruthy();
      expect(el!.textContent).toBe(m.text);
    }
    // Empty placeholder clears once any message is rendered.
    const list = document.querySelector('[data-lumen-iframe="message-list"]')!;
    expect(list.getAttribute('data-empty')).toBe('false');
  });

  it('send button is disabled when textarea empty and enables on input', () => {
    const chat = createChat(document, { onSend: () => {}, onEscalate: () => {} });
    document.body.appendChild(chat.el);

    const sendBtn = document.querySelector<HTMLButtonElement>(
      '[data-lumen-iframe="composer-send"]',
    )!;
    const input = document.querySelector<HTMLTextAreaElement>(
      '[data-lumen-iframe="composer-input"]',
    )!;

    expect(sendBtn.disabled).toBe(true);

    input.value = '   '; // whitespace only — still disabled
    input.dispatchEvent(new Event('input'));
    expect(sendBtn.disabled).toBe(true);

    input.value = 'hello';
    input.dispatchEvent(new Event('input'));
    expect(sendBtn.disabled).toBe(false);

    input.value = '';
    input.dispatchEvent(new Event('input'));
    expect(sendBtn.disabled).toBe(true);
  });

  it('send dispatches onSend with the trimmed text and clears the textarea', () => {
    const onSend = vi.fn();
    const chat = createChat(document, { onSend, onEscalate: () => {} });
    document.body.appendChild(chat.el);

    const form = document.querySelector<HTMLFormElement>(
      '[data-lumen-iframe="composer"]',
    )!;
    const input = document.querySelector<HTMLTextAreaElement>(
      '[data-lumen-iframe="composer-input"]',
    )!;

    input.value = '   need help   ';
    form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));

    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith('need help');
    expect(input.value).toBe('');
  });

  it('auto-scrolls to the bottom when a new message is appended', () => {
    const chat = createChat(document, { onSend: () => {}, onEscalate: () => {} });
    document.body.appendChild(chat.el);

    const list = document.querySelector<HTMLDivElement>(
      '[data-lumen-iframe="message-list"]',
    )!;

    // jsdom doesn't perform layout, so scrollHeight stays at 0. Stub it so
    // we can assert that scrollTop is set to scrollHeight (the auto-scroll
    // contract) without depending on layout math.
    const scrollHeights: number[] = [];
    Object.defineProperty(list, 'scrollHeight', {
      configurable: true,
      get(): number {
        return scrollHeights.length;
      },
    });

    for (let i = 0; i < 3; i++) {
      scrollHeights.push(100 + i * 10);
      chat.appendMessage({
        id: `m-${i}`,
        role: 'customer',
        text: `msg ${i}`,
        createdAt: i,
        status: 'sent',
      });
    }

    // requestAnimationFrame is queued by appendMessage; flush microtasks
    // so the scroll-to-bottom callback runs synchronously.
    return new Promise<void>((resolve) => {
      requestAnimationFrame(() => {
        expect(list.scrollTop).toBe(list.scrollHeight);
        resolve();
      });
    });
  });
});