import path from 'node:path';
import { mkdirSync, copyFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig, type Plugin } from 'vite';

// Widget demo Vite config.
//
// Why this file exists
// --------------------
// ``main.ts`` injects ``<script src="./lumen-widget.js">`` at runtime;
// the SDK bundle is built into ``apps/web-sdk/dist/`` (separate
// workspace package) and isn't a runtime dep of example-integration.
// Without copying, the dev server returns 404 for the SDK and the
// widget button never mounts.
//
// We sync the two built bundles into Vite's ``public/`` directory on
// every dev/build invocation via a tiny inline plugin (runs at
// ``configResolved`` so it executes before the dev server starts
// serving). Replaces the earlier ``vite-plugin-static-copy`` route
// which silently failed to match files on Windows backslash paths.
//
// If the SDK is unbuilt (fresh clone before any build), the copy
// sources don't exist and the plugin logs a warning; the widget
// page still loads but the chat fails to mount with
// ``lumen-widget.js 404`` — fix by running
// ``pnpm --filter @lumen/web-sdk build`` first.

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const sdkDist = path.resolve(__dirname, '../../../web-sdk/dist');
const publicDir = path.resolve(__dirname, 'public');

const SDK_FILES = ['lumen-widget.js', 'iframe.js'] as const;

function syncSdkPlugin(): Plugin {
  return {
    name: 'widget-demo:sync-sdk',
    configResolved() {
      if (!existsSync(sdkDist)) {
        // eslint-disable-next-line no-console
        console.warn(
          `[widget-demo] SDK not built at ${sdkDist} — run \`pnpm --filter @lumen/web-sdk build\` first.`,
        );
        return;
      }
      mkdirSync(publicDir, { recursive: true });
      for (const file of SDK_FILES) {
        const src = path.join(sdkDist, file);
        if (!existsSync(src)) {
          // eslint-disable-next-line no-console
          console.warn(`[widget-demo] missing SDK file: ${src}`);
          continue;
        }
        copyFileSync(src, path.join(publicDir, file));
      }
      // eslint-disable-next-line no-console
      console.log(`[widget-demo] synced ${SDK_FILES.length} SDK files into public/`);
    },
  };
}

export default defineConfig({
  // The dev server is launched from the repo root with
  // ``vite --config <path>``, so without an explicit ``root`` Vite
  // serves the wrong directory (the repo root) and returns 404 for
  // index.html / main.ts. Pin to the demo's own directory.
  root: __dirname,
  plugins: [syncSdkPlugin()],
  server: {
    // Default 5173 collides with apps/web's vite dev; pin to 5183.
    port: 5183,
    strictPort: true,
    host: '0.0.0.0',
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
