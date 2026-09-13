/**
 * Injects the iframe stylesheet into the iframe document.
 *
 * Mirrors the pattern in `widget-frame.ts`: a single <style> tag with
 * the full CSS string, idempotently registered by id. All data-*
 * selectors are stable across builds so tests can query them.
 *
 * The CSS is XSS-safe: the only interpolated value is the tenant accent
 * color, and it's validated to be a hex literal before injection.
 */
const STYLE_ID = 'lumen-iframe-styles';

export function injectStyles(
  documentRef: Document,
  accentColor?: string,
): void {
  if (documentRef.getElementById(STYLE_ID)) return;
  const accent = isHexColor(accentColor) ? accentColor : '#0ea5e9';
  const style = documentRef.createElement('style');
  style.id = STYLE_ID;
  style.textContent = `
:root { color-scheme: light dark; --lumen-accent: ${accent}; }
html, body { margin: 0; height: 100%; box-sizing: border-box; }
body {
  display: flex; flex-direction: column;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 14px;
  color: #111;
  background: #fafafa;
}
[data-lumen-iframe="header"] {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 14px;
  background: var(--lumen-accent);
  color: #fff;
  flex-shrink: 0;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  min-height: 44px;
}
[data-lumen-iframe="header-title"] {
  font-size: 14px; font-weight: 600;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  padding-right: 24px;
}
[data-lumen-iframe="header-subtitle"] {
  font-size: 11px; opacity: 0.85;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  padding-right: 24px;
}
[data-lumen-iframe="header-close"] {
  background: transparent; border: none; color: #fff;
  font-size: 22px; line-height: 1; cursor: pointer; padding: 0 6px;
}
[data-lumen-iframe="chat"] {
  flex: 1 1 auto;
  display: flex; flex-direction: column;
  min-height: 0;
}
[data-lumen-iframe="message-list"] {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 12px;
  display: flex; flex-direction: column; gap: 8px;
}
[data-lumen-iframe="message-list"][data-empty="true"]::before {
  content: "开始对话吧,我们会尽快回复您";
  display: block;
  margin: auto;
  font-size: 12px;
  color: #9ca3af;
}
[data-lumen-iframe="message"] {
  max-width: 80%;
  padding: 8px 12px;
  border-radius: 12px;
  word-break: break-word;
  white-space: pre-wrap;
  line-height: 1.4;
  font-size: 14px;
}
[data-lumen-iframe="message"][data-role="customer"] {
  align-self: flex-end;
  background: var(--lumen-accent);
  color: #fff;
  border-bottom-right-radius: 4px;
}
[data-lumen-iframe="message"][data-role="agent"] {
  align-self: flex-start;
  background: #10b981;
  color: #fff;
  border-bottom-left-radius: 4px;
}
[data-lumen-iframe="message"][data-role="ai"] {
  align-self: flex-start;
  background: #8b5cf6;
  color: #fff;
  border-bottom-left-radius: 4px;
}
[data-lumen-iframe="message"][data-role="system"] {
  align-self: center;
  background: #e5e7eb;
  color: #374151;
  font-size: 12px;
}
[data-lumen-iframe="message"][data-status="pending"] { opacity: 0.6; }
[data-lumen-iframe="message"][data-status="failed"] {
  background: #ef4444 !important;
  color: #fff !important;
}
[data-lumen-iframe="escalate-hint"] {
  padding: 6px 14px;
  font-size: 12px;
  color: #047857;
  background: #ecfdf5;
  text-align: center;
  border-top: 1px solid #d1fae5;
  flex-shrink: 0;
}
[data-lumen-iframe="composer"] {
  display: flex; align-items: flex-end; gap: 6px;
  padding: 8px 10px;
  border-top: 1px solid #e5e7eb;
  background: #fff;
  flex-shrink: 0;
}
[data-lumen-iframe="composer-input"] {
  flex: 1 1 auto;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  padding: 6px 8px;
  font: inherit;
  resize: none;
  max-height: 80px;
  outline: none;
  background: #fff;
  color: inherit;
}
[data-lumen-iframe="composer-input"]:focus { border-color: var(--lumen-accent); }
[data-lumen-iframe="composer-send"],
[data-lumen-iframe="composer-escalate"] {
  border: none; border-radius: 6px;
  padding: 6px 10px;
  font: inherit;
  cursor: pointer;
  white-space: nowrap;
  line-height: 1.2;
}
[data-lumen-iframe="composer-send"] {
  background: var(--lumen-accent);
  color: #fff;
}
[data-lumen-iframe="composer-send"]:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
[data-lumen-iframe="composer-escalate"] {
  background: #f3f4f6;
  color: #374151;
}
[data-lumen-iframe="footer"] {
  padding: 4px 8px;
  text-align: center;
  font-size: 10px;
  color: #9ca3af;
  background: #f9fafb;
  border-top: 1px solid #f3f4f6;
  flex-shrink: 0;
}
@media (prefers-color-scheme: dark) {
  body { background: #0f172a; color: #e2e8f0; }
  [data-lumen-iframe="composer"] { background: #1e293b; border-top-color: #334155; }
  [data-lumen-iframe="composer-input"] { background: #0f172a; color: #e2e8f0; border-color: #475569; }
  [data-lumen-iframe="composer-escalate"] { background: #334155; color: #e2e8f0; }
  [data-lumen-iframe="message"][data-role="system"] { background: #334155; color: #cbd5e1; }
  [data-lumen-iframe="message-list"] { background: #0f172a; }
  [data-lumen-iframe="message-list"][data-empty="true"]::before { color: #64748b; }
  [data-lumen-iframe="escalate-hint"] { background: #064e3b; color: #6ee7b7; border-top-color: #065f46; }
  [data-lumen-iframe="footer"] { background: #1e293b; color: #64748b; border-top-color: #334155; }
}
`;
  documentRef.head.appendChild(style);
}

function isHexColor(input: string | undefined): boolean {
  if (typeof input !== 'string') return false;
  return /^#[0-9a-fA-F]{3,8}$/.test(input);
}