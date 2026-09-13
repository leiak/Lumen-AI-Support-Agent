/**
 * Floating button + chat iframe DOM manager.
 *
 * No React, no framework — vanilla DOM only. All user-supplied strings are
 * inserted via textContent (never innerHTML) so the SDK is XSS-safe even
 * if a tenant passes an attacker-controlled config value.
 *
 * The chat UI itself lives in a separate IIFE bundle (`dist/iframe.js`)
 * whose source is inlined into the iframe HTML at build time. The full
 * HTML is then injected into this module via the `__IFRAME_HTML__`
 * esbuild `define`. We set the iframe's `srcdoc` to that HTML so the
 * customer site never has to serve our files — fully cross-origin safe.
 *
 * Wire protocol between this parent SDK and the iframe:
 *   parent -> iframe: { type:'init', config:{...} }   (on iframe.onload)
 *   iframe -> parent: { type:'ready' }                (signal post-mount)
 *   iframe -> parent: { type:'close' }                (user clicked X)
 */

import type { ResolvedConfig } from './config.js';

declare const __IFRAME_HTML__: string;

const STORAGE_KEY = 'lumen-widget:open';

export interface WidgetFrameInit {
  config: ResolvedConfig;
  /** Pre-minted widget JWT — propagated to the iframe via postMessage. */
  widgetToken: string;
  /** Stable visitor id — propagated to the iframe via postMessage. */
  externalUserId: string;
}

export interface WidgetFrame {
  /** Open the chat window. Idempotent. */
  open(): void;
  /** Close the chat window (minimize back to the floating button). Idempotent. */
  close(): void;
  /** True if the chat window is currently open. */
  isOpen(): boolean;
  /** Remove all DOM nodes and detach event listeners. */
  destroy(): void;
  /** The iframe element (for tests). */
  iframe: HTMLIFrameElement;
  /** The floating button element. */
  button: HTMLButtonElement;
}

export function createWidgetFrame(
  documentRef: Document,
  init: WidgetFrameInit,
  host: HTMLElement,
): WidgetFrame {
  const { config, widgetToken, externalUserId } = init;
  const accent = sanitizeAccent(config.accentColor);

  injectStyles(documentRef, accent, config.position);

  // Floating button.
  const button = documentRef.createElement('button');
  button.type = 'button';
  button.setAttribute('data-lumen-widget', 'button');
  button.setAttribute('aria-label', 'Open chat');
  // textContent, not innerHTML — title is user-configurable.
  button.textContent = config.title.charAt(0) || '?';
  host.appendChild(button);

  // Chat iframe shell.
  const wrapper = documentRef.createElement('div');
  wrapper.setAttribute('data-lumen-widget', 'wrapper');
  wrapper.setAttribute('data-state', 'closed');

  const header = documentRef.createElement('div');
  header.setAttribute('data-lumen-widget', 'header');
  const headerTitle = documentRef.createElement('span');
  headerTitle.textContent = config.title;
  const headerSubtitle = documentRef.createElement('span');
  headerSubtitle.textContent = config.subtitle;
  const closeBtn = documentRef.createElement('button');
  closeBtn.type = 'button';
  closeBtn.setAttribute('data-lumen-widget', 'close');
  closeBtn.setAttribute('aria-label', 'Close chat');
  closeBtn.textContent = '×';
  header.appendChild(headerTitle);
  header.appendChild(headerSubtitle);
  header.appendChild(closeBtn);

  const iframe = documentRef.createElement('iframe');
  iframe.setAttribute('data-lumen-widget', 'iframe');
  iframe.title = config.title;
  iframe.setAttribute('allow', '');
  // srcdoc is the inlined iframe HTML — set lazily on first open so the
  // browser doesn't spin up the iframe document for visitors who never
  // click the floating button. This is a meaningful saving on pages where
  // the SDK loads but the user never engages.
  let srcdocSet = false;
  const ensureSrcdoc = (): void => {
    if (srcdocSet) return;
    iframe.srcdoc = __IFRAME_HTML__;
    srcdocSet = true;
  };

  wrapper.appendChild(header);
  wrapper.appendChild(iframe);
  host.appendChild(wrapper);

  let open = readPersistedState();

  function setOpenState(next: boolean): void {
    open = next;
    wrapper.setAttribute('data-state', next ? 'open' : 'closed');
    button.setAttribute('aria-expanded', next ? 'true' : 'false');
    if (next) {
      ensureSrcdoc();
    }
    try {
      window.localStorage.setItem(STORAGE_KEY, next ? '1' : '0');
    } catch {
      // localStorage may be unavailable (private mode, sandboxed iframe).
      // The widget still works for the current page.
    }
  }

  // Reflect persisted state on first render so a returning visitor sees
  // the same open/closed state as when they left.
  if (open) {
    setOpenState(true);
  }

  const onButtonClick = (): void => {
    setOpenState(true);
  };
  const onCloseClick = (): void => {
    setOpenState(false);
  };
  const onIframeLoad = (): void => {
    // Hand the iframe its config via postMessage. We always send — even
    // when widgetToken is empty — so the iframe can decide whether to
    // mount a graceful "connection failed" state. postMessage with a
    // missing contentWindow is a no-op in modern browsers.
    try {
      iframe.contentWindow?.postMessage(
        {
          type: 'init',
          config: {
            apiBaseUrl: config.apiBaseUrl,
            channelId: config.channelId,
            tenantId: config.tenantId,
            widgetToken,
            accentColor: config.accentColor,
            title: config.title,
            subtitle: config.subtitle,
            externalUserId,
            locale: config.locale,
          },
        },
        '*',
      );
    } catch {
      // best effort — if postMessage throws (e.g. detached contentWindow)
      // the user can still close via the SDK's own close button.
    }
  };
  const onWindowMessage = (event: MessageEvent): void => {
    const data = event.data;
    if (!data || typeof data !== 'object') return;
    const msg = data as Record<string, unknown>;
    // Only collapse if the message came from OUR iframe. Other iframes on
    // the page might also be posting `close` events; we ignore them.
    if (msg.type === 'close' && event.source === iframe.contentWindow) {
      setOpenState(false);
    }
  };

  button.addEventListener('click', onButtonClick);
  closeBtn.addEventListener('click', onCloseClick);
  iframe.addEventListener('load', onIframeLoad);
  window.addEventListener('message', onWindowMessage);

  return {
    open: (): void => setOpenState(true),
    close: (): void => setOpenState(false),
    isOpen: (): boolean => open,
    iframe,
    button,
    destroy: (): void => {
      button.removeEventListener('click', onButtonClick);
      closeBtn.removeEventListener('click', onCloseClick);
      iframe.removeEventListener('load', onIframeLoad);
      window.removeEventListener('message', onWindowMessage);
      button.remove();
      wrapper.remove();
    },
  };
}

function readPersistedState(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

/**
 * Strip anything that isn't a 3, 4, 6, or 8-char hex string, prefixed
 * with `#`. Anything else (named colors, rgb(), CSS injection attempts)
 * is dropped to the fallback. Returns the fallback string if input is
 * missing or invalid.
 */
function sanitizeAccent(input: string | undefined): string {
  const fallback = '#0ea5e9';
  if (typeof input !== 'string') return fallback;
  if (!/^#[0-9a-fA-F]{3,8}$/.test(input)) return fallback;
  return input;
}

function injectStyles(
  documentRef: Document,
  accent: string,
  position: 'bottom-right' | 'bottom-left',
): void {
  const STYLE_ID = 'lumen-widget-styles';
  if (documentRef.getElementById(STYLE_ID)) return;

  const style = documentRef.createElement('style');
  style.id = STYLE_ID;
  style.textContent = `
[data-lumen-widget="button"] {
  position: fixed;
  ${position === 'bottom-left' ? 'left: 20px;' : 'right: 20px;'}
  bottom: 20px;
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: ${accent};
  color: #fff;
  border: none;
  font-size: 22px;
  font-weight: 600;
  cursor: pointer;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  z-index: 2147483646;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
[data-lumen-widget="button"]:hover { filter: brightness(1.05); }
[data-lumen-widget="wrapper"] {
  position: fixed;
  ${position === 'bottom-left' ? 'left: 20px;' : 'right: 20px;'}
  bottom: 88px;
  width: 360px;
  height: 520px;
  max-width: calc(100vw - 40px);
  max-height: calc(100vh - 110px);
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.18);
  overflow: hidden;
  display: none;
  flex-direction: column;
  z-index: 2147483647;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  color: #111;
}
[data-lumen-widget="wrapper"][data-state="open"] { display: flex; }
[data-lumen-widget="header"] {
  display: flex;
  flex-direction: column;
  padding: 14px 16px;
  background: ${accent};
  color: #fff;
  flex-shrink: 0;
  position: relative;
}
[data-lumen-widget="header"] > span:first-child {
  font-size: 15px;
  font-weight: 600;
  line-height: 1.2;
  padding-right: 28px;
}
[data-lumen-widget="header"] > span:nth-child(2) {
  font-size: 12px;
  opacity: 0.85;
  margin-top: 2px;
  padding-right: 28px;
}
[data-lumen-widget="close"] {
  position: absolute;
  top: 8px;
  right: 12px;
  background: transparent;
  border: none;
  color: #fff;
  font-size: 22px;
  cursor: pointer;
  line-height: 1;
}
[data-lumen-widget="iframe"] {
  flex: 1 1 auto;
  width: 100%;
  border: none;
  background: #fafafa;
}
@media (max-width: 480px) {
  [data-lumen-widget="wrapper"] {
    left: 10px;
    right: 10px;
    width: auto;
    bottom: 80px;
  }
}
`;
  documentRef.head.appendChild(style);
}