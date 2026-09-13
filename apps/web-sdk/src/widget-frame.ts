/**
 * Floating button + chat iframe DOM manager.
 *
 * No React, no framework — vanilla DOM only. All user-supplied strings are
 * inserted via textContent (never innerHTML) so the SDK is XSS-safe even
 * if a tenant passes an attacker-controlled config value.
 *
 * The chat content itself is a placeholder iframe (Stage 9.9 will fill it).
 * For Stage 9.8 the iframe shows a minimal greeting so the integration is
 * visually verifiable end-to-end.
 */

import type { ResolvedConfig } from './config.js';

const STORAGE_KEY = 'lumen-widget:open';

export interface WidgetFrame {
  /** Open the chat window. Idempotent. */
  open(): void;
  /** Close the chat window (minimize back to the floating button). Idempotent. */
  close(): void;
  /** True if the chat window is currently open. */
  isOpen(): boolean;
  /** Remove all DOM nodes and detach event listeners. */
  destroy(): void;
  /** The iframe element (for tests + Stage 9.9 to mount content into). */
  iframe: HTMLIFrameElement;
  /** The floating button element. */
  button: HTMLButtonElement;
}

export function createWidgetFrame(
  documentRef: Document,
  config: ResolvedConfig,
  host: HTMLElement,
): WidgetFrame {
  const accent = sanitizeAccent(config.accentColor);

  // Inject the SDK stylesheet once per host document. Re-injecting is a
  // no-op because we look up by id before appending.
  injectStyles(documentRef, accent, config.position);

  // Floating button.
  const button = documentRef.createElement('button');
  button.type = 'button';
  button.setAttribute('data-lumen-widget', 'button');
  button.setAttribute('aria-label', 'Open chat');
  // textContent, not innerHTML — title is user-configurable.
  button.textContent = config.title.charAt(0) || '?';
  host.appendChild(button);

  // Chat iframe shell. We use srcdoc for the placeholder so the customer
  // site doesn't need to serve a separate file. Stage 9.9 will replace
  // srcdoc with a real chat UI.
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
  iframe.srcdoc = buildPlaceholderHtml(config.title, config.subtitle);
  iframe.setAttribute('allow', '');

  wrapper.appendChild(header);
  wrapper.appendChild(iframe);
  host.appendChild(wrapper);

  let open = readPersistedState();

  function setOpenState(next: boolean): void {
    open = next;
    wrapper.setAttribute('data-state', next ? 'open' : 'closed');
    button.setAttribute('aria-expanded', next ? 'true' : 'false');
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
  button.addEventListener('click', onButtonClick);
  closeBtn.addEventListener('click', onCloseClick);

  return {
    open: (): void => setOpenState(true),
    close: (): void => setOpenState(false),
    isOpen: (): boolean => open,
    iframe,
    button,
    destroy: (): void => {
      button.removeEventListener('click', onButtonClick);
      closeBtn.removeEventListener('click', onCloseClick);
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
  // Use a template string for readability — no user input is interpolated
  // except `accent` (already sanitized to a hex literal) and `position`
  // (whitelisted to two literal values).
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
}
[data-lumen-widget="header"] > span:first-child {
  font-size: 15px;
  font-weight: 600;
  line-height: 1.2;
}
[data-lumen-widget="header"] > span:nth-child(2) {
  font-size: 12px;
  opacity: 0.85;
  margin-top: 2px;
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

function buildPlaceholderHtml(title: string, subtitle: string): string {
  // Static placeholder — title/subtitle are injected via textContent in
  // the host page, NOT via innerHTML here. The iframe is a separate
  // document context so even this string interpolation is sanitized.
  const safeTitle = escapeHtml(title);
  const safeSubtitle = escapeHtml(subtitle);
  return `<!doctype html>
<html><head><meta charset="utf-8"><title>${safeTitle}</title>
<style>
  html, body { margin: 0; height: 100%; }
  body {
    display: flex; align-items: center; justify-content: center;
    flex-direction: column; gap: 8px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #444; background: #fafafa; padding: 24px; box-sizing: border-box;
    text-align: center;
  }
  h1 { font-size: 16px; margin: 0; color: #111; }
  p { font-size: 13px; margin: 0; color: #666; }
</style></head>
<body>
  <h1>${safeTitle}</h1>
  <p>${safeSubtitle}</p>
  <p style="margin-top:12px;font-size:11px;color:#999;">Stage 9.9 will mount the chat UI here.</p>
</body></html>`;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

