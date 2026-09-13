// Build the embeddable SDK as a single IIFE JS file.
//
// Output: dist/lumen-widget.js
// The bundle is wrapped in an IIFE so embedding via <script src="..."> does
// not pollute the global namespace beyond window.LumenAICustomer.
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.resolve(__dirname, 'dist');
const outFile = path.join(outDir, 'lumen-widget.js');

if (!fs.existsSync(outDir)) {
  fs.mkdirSync(outDir, { recursive: true });
}

await build({
  entryPoints: [path.resolve(__dirname, 'src/boot.ts')],
  bundle: true,
  format: 'iife',
  globalName: 'LumenAICustomer',
  target: ['es2020'],
  platform: 'browser',
  minify: true,
  sourcemap: false,
  outfile: outFile,
  logLevel: 'info',
});

const stat = fs.statSync(outFile);
console.log(`built ${outFile} (${stat.size} bytes)`);
