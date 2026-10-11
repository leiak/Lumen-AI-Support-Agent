// End-to-end widget verification:
//   1. Loads localhost:5173/, asserts the chat bubble + iframe render
//   2. Clicks the bubble to open the chat panel
//   3. Types a customer question into the iframe composer
//   4. Clicks send and waits for the AI reply to appear
//   5. Reports the resulting message history
// Throws (exit 1) on any failure.
import { chromium } from '../../node_modules/.pnpm/playwright@1.63.0/node_modules/playwright/index.mjs';

const URL = 'http://localhost:5173/';
const CUSTOMER_TEXT = 'How do I reset my password?';

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext();
const page = await ctx.newPage();

const consoleMsgs = [];
const pageErrors = [];
const requests = [];
const responses = [];
page.on('console', m => consoleMsgs.push(`[${m.type()}] ${m.text()}`));
page.on('pageerror', e => pageErrors.push(String(e)));
page.on('request', r => requests.push(`${r.method()} ${r.url()}`));
page.on('response', r => responses.push(`${r.status()} ${r.url()}`));

await page.goto(URL, { waitUntil: 'load' });
await page.waitForSelector('[data-lumen-widget="button"]', { timeout: 5000 });

const boot = await page.evaluate(() => {
  const b = document.querySelector('[data-lumen-widget="button"]');
  return {
    hasLumenConfig: typeof window.LumenAICustomerConfig === 'object',
    hasLumenSDK: typeof window.LumenAICustomer === 'object',
    sdkKeys: window.LumenAICustomer ? Object.keys(window.LumenAICustomer) : null,
    buttonText: b?.textContent ?? null,
  };
});

// 1. Open the chat panel
await page.click('[data-lumen-widget="button"]');
await page.waitForSelector('[data-lumen-widget="iframe"]', { timeout: 5000 });

// 2. Wait for the iframe's chat content to mount
const frameEl = await page.waitForSelector('[data-lumen-widget="iframe"]');
const frame = await frameEl.contentFrame();
await page.waitForTimeout(4000);
const iframeDebug = await frame.evaluate(() => ({
  readyState: document.readyState,
  bodyChildren: document.body.children.length,
  selectors: Array.from(document.querySelectorAll('[data-lumen-iframe]')).map(e => e.getAttribute('data-lumen-iframe')),
})).catch(e => ({ error: String(e) }));
const iframeAttrs = await page.evaluate(() => {
  const f = document.querySelector('[data-lumen-widget="iframe"]');
  return {
    srcdocLen: f?.getAttribute('srcdoc')?.length ?? 0,
    src: f?.getAttribute('src'),
    hasSrcdoc: f?.hasAttribute('srcdoc'),
  };
});
console.log('=== iframe debug ===');
console.log(JSON.stringify({ iframeAttrs, iframeDebug }, null, 2));
try {
  await frame.waitForSelector('[data-lumen-iframe="composer-input"]', { timeout: 15000, state: 'attached' });
} catch (e) {
  console.log('=== api requests (' + requests.length + ') ===');
  for (const r of requests.filter(x => x.includes('8000'))) console.log(r);
  console.log('=== api responses (' + responses.length + ') ===');
  for (const r of responses.filter(x => x.includes('8000'))) console.log(r);
  console.log('=== console (' + consoleMsgs.length + ' msgs) ===');
  for (const m of consoleMsgs) console.log(m);
  console.log('=== page errors (' + pageErrors.length + ') ===');
  for (const e of pageErrors) console.log(e);
  await browser.close();
  process.exit(1);
}

// 3. Type and send
await frame.fill('[data-lumen-iframe="composer-input"]', CUSTOMER_TEXT);
await frame.click('[data-lumen-iframe="composer-send"]');

// 4. Wait for AI reply — two messages (customer + ai) with a non-empty ai body
const deadline = Date.now() + 30000;
let aiReply = null;
let messageCount = 0;
let snapshot = [];
while (Date.now() < deadline) {
  snapshot = await frame.evaluate(() => {
    const msgs = Array.from(document.querySelectorAll('[data-lumen-iframe="message"]'));
    return msgs.map(m => ({
      role: m.getAttribute('data-role'),
      id: m.getAttribute('data-id'),
      text: m.textContent?.trim() ?? '',
    }));
  });
  messageCount = snapshot.length;
  aiReply = snapshot.find(m => m.role === 'ai' && m.text.length > 0);
  if (aiReply) break;
  await page.waitForTimeout(500);
}

const report = {
  boot,
  bubble_clicked: true,
  iframe_loaded: true,
  composer_typed: true,
  message_count: messageCount,
  customer_text_present: snapshot.some(m => m.role === 'customer' && m.text.includes(CUSTOMER_TEXT)),
  ai_reply: aiReply,
};
console.log('=== report ===');
console.log(JSON.stringify(report, null, 2));
console.log('=== console (' + consoleMsgs.length + ' msgs) ===');
for (const m of consoleMsgs) console.log(m);
console.log('=== page errors (' + pageErrors.length + ') ===');
for (const e of pageErrors) console.log(e);
console.log('=== api requests (' + requests.length + ') ===');
for (const r of requests.filter(x => x.includes('8000'))) console.log(r);
console.log('=== api responses (' + responses.length + ') ===');
for (const r of responses.filter(x => x.includes('8000'))) console.log(r);

await browser.close();
const ok = pageErrors.length === 0 && !!report.customer_text_present && !!aiReply;
process.exit(ok ? 0 : 1);