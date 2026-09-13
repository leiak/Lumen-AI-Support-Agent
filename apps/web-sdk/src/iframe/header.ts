/**
 * Header bar: tenant title + optional subtitle + close button.
 *
 * Clicking the X dispatches a `{type:'close'}` postMessage to the parent
 * SDK, which is the only way the iframe can request closure (it cannot
 * touch the parent's DOM directly). The parent listens on `window.message`
 * and collapses the widget wrapper.
 *
 * The targetWindow parameter is exposed for tests so we can swap in a
 * mock `parent` without depending on jsdom's iframe support.
 */
import type { IframeConfig } from './protocol.js';

export interface HeaderHandle {
  el: HTMLElement;
  setTitle(title: string): void;
  setSubtitle(subtitle: string): void;
}

export function createHeader(
  documentRef: Document,
  config: IframeConfig,
  targetWindow: Window,
): HeaderHandle {
  const root = documentRef.createElement('header');
  root.setAttribute('data-lumen-iframe', 'header');
  root.setAttribute('role', 'banner');

  const text = documentRef.createElement('div');
  text.setAttribute('data-lumen-iframe', 'header-text');
  text.style.display = 'flex';
  text.style.flexDirection = 'column';
  text.style.minWidth = '0';
  text.style.flex = '1 1 auto';

  const titleEl = documentRef.createElement('span');
  titleEl.setAttribute('data-lumen-iframe', 'header-title');
  titleEl.textContent = config.title;

  const subtitleEl = documentRef.createElement('span');
  subtitleEl.setAttribute('data-lumen-iframe', 'header-subtitle');
  subtitleEl.textContent = config.subtitle;

  text.appendChild(titleEl);
  text.appendChild(subtitleEl);

  const closeBtn = documentRef.createElement('button');
  closeBtn.type = 'button';
  closeBtn.setAttribute('data-lumen-iframe', 'header-close');
  closeBtn.setAttribute('aria-label', 'Close chat');
  closeBtn.textContent = '×';
  closeBtn.addEventListener('click', () => {
    try {
      targetWindow.parent?.postMessage({ type: 'close' }, '*');
    } catch {
      // best effort — if postMessage fails (very old browser), the user
      // can still close via the parent SDK's own close button.
    }
  });

  root.appendChild(text);
  root.appendChild(closeBtn);

  return {
    el: root,
    setTitle(next: string) {
      titleEl.textContent = next;
    },
    setSubtitle(next: string) {
      subtitleEl.textContent = next;
    },
  };
}