// One-shot headless verification of the widget demo.
// Loads the page, waits for the SDK to inject the chat bubble, then
// reports what it found. Throws on failure.
import { chromium } from '../../node_modules/.pnpm/playwright@1.63.0/node_modules/playwright/index.mjs';

const URL = 'http://localhost:5173/';

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext();
const page = await ctx.newPage();

const consoleMsgs = [];
const pageErrors = [];
page.on('console', m => consoleMsgs.push(`[${m.type()}] ${m.text()}`));
page.on('pageerror', e => pageErrors.push(String(e)));

await page.goto(URL, { waitUntil: 'load' });
// Give the SDK a moment to fetch token + inject the bubble.
await page.waitForTimeout(3000);

const report = await page.evaluate(() => {
  const out = {
    title: document.title,
    h1: document.querySelector('h1')?.textContent ?? null,
    staticText: document.body.innerText.slice(0, 200),
    hasLumenConfig: typeof window.LumenAICustomerConfig === 'object',
    hasLumenSDK: typeof window.LumenAICustomer === 'object',
    sdkKeys: window.LumenAICustomer ? Object.keys(window.LumenAICustomer) : null,
    bubbleButton: !!document.querySelector('[data-lumen-widget="button"]'),
    iframeEl: !!document.querySelector('[data-lumen-widget="iframe"]'),
    wrapper: !!document.querySelector('[data-lumen-widget="wrapper"]'),
    buttonRect: (() => {
      const b = document.querySelector('[data-lumen-widget="button"]');
      if (!b) return null;
      const r = b.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height, text: b.textContent };
    })(),
  };
  return out;
});

console.log('=== report ===');
console.log(JSON.stringify(report, null, 2));
console.log('=== console (' + consoleMsgs.length + ' msgs) ===');
for (const m of consoleMsgs) console.log(m);
console.log('=== page errors (' + pageErrors.length + ') ===');
for (const e of pageErrors) console.log(e);

await browser.close();
process.exit(pageErrors.length === 0 && report.bubbleButton ? 0 : 1);
