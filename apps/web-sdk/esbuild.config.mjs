// Build the embeddable SDK + the iframe content bundle.
//
// Outputs:
//   dist/lumen-widget.js  — single IIFE, globalName: LumenAICustomer
//                            (parent SDK: floating button + iframe shell +
//                             srcdoc with the iframe HTML inlined)
//   dist/iframe.js        — single IIFE (iframe chat content; mounted via
//                            srcdoc from the parent SDK at runtime)
//   dist/iframe.html      — pre-bundled iframe HTML page (standalone, for
//                            dev use / future CDN deployment). The parent
//                            SDK does NOT load this file — it uses the
//                            `__IFRAME_HTML__` define below so the chat
//                            UI is fully inlined into lumen-widget.js and
//                            requires no cross-origin fetch.
//
// Build order:
//   1. Build dist/iframe.js.
//   2. Wrap iframe.js in a static HTML page → dist/iframe.html.
//   3. Build dist/lumen-widget.js, with the inlined iframe HTML injected
//      via esbuild's `define` so widget-frame.ts can set it as srcdoc.
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.resolve(__dirname, 'dist');

if (!fs.existsSync(outDir)) {
  fs.mkdirSync(outDir, { recursive: true });
}

// ---------------------------------------------------------------------------
// Step 1: build the iframe bundle (used standalone by dist/iframe.html and
//         inlined as a string into dist/lumen-widget.js).
// ---------------------------------------------------------------------------
const iframeJsPath = path.join(outDir, 'iframe.js');
await build({
  entryPoints: [path.resolve(__dirname, 'src/iframe/main.ts')],
  bundle: true,
  format: 'iife',
  target: ['es2020'],
  platform: 'browser',
  minify: true,
  sourcemap: false,
  outfile: iframeJsPath,
  logLevel: 'info',
});

// ---------------------------------------------------------------------------
// Step 2: build dist/iframe.html — a self-contained HTML page that loads
//         dist/iframe.js from the same directory. This file is provided
//         for dev/testing and as a CDN-ready artefact for a future stage;
//         the parent SDK does NOT reference it.
// ---------------------------------------------------------------------------
const iframeJsSource = fs.readFileSync(iframeJsPath, 'utf8');
const iframeHtmlPath = path.join(outDir, 'iframe.html');
const iframeHtml = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lumen</title>
</head>
<body>
<script>${iframeJsSource}</script>
</body>
</html>
`;
fs.writeFileSync(iframeHtmlPath, iframeHtml);

// ---------------------------------------------------------------------------
// Step 3: build the parent SDK. The iframe HTML is inlined as the
//         `__IFRAME_HTML__` constant via esbuild's `define` so the
//         parent's widget-frame.ts can set `iframe.srcdoc = __IFRAME_HTML__`
//         without any cross-origin file fetch.
// ---------------------------------------------------------------------------
const lumenJsPath = path.join(outDir, 'lumen-widget.js');
await build({
  entryPoints: [path.resolve(__dirname, 'src/boot.ts')],
  bundle: true,
  format: 'iife',
  globalName: 'LumenAICustomer',
  target: ['es2020'],
  platform: 'browser',
  minify: true,
  sourcemap: false,
  outfile: lumenJsPath,
  logLevel: 'info',
  define: {
    __IFRAME_HTML__: JSON.stringify(iframeHtml),
  },
});

// ---------------------------------------------------------------------------
// Final size report — useful for keeping the M1 widget under the 50KB
// target. Fails the build (non-zero exit) if either bundle is missing.
// ---------------------------------------------------------------------------
const lumenStat = fs.statSync(lumenJsPath);
const iframeStat = fs.statSync(iframeJsPath);
const iframeHtmlStat = fs.statSync(iframeHtmlPath);
console.log(`built ${lumenJsPath} (${lumenStat.size} bytes)`);
console.log(`built ${iframeJsPath} (${iframeStat.size} bytes)`);
console.log(`built ${iframeHtmlPath} (${iframeHtmlStat.size} bytes)`);