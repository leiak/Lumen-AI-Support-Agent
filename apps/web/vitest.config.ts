import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    // The Vite config (and pure-config helpers like
    // ``src/lib/dev-proxy.ts``) don't need jsdom and trip a
    // TextEncoder invariant when esbuild's plugin pipeline boots
    // inside jsdom. Files matching ``src/__tests__/proxy*.test.ts``
    // run in node and skip the setupFile.
    environmentMatchGlobs: [
      ['src/__tests__/proxy*.test.ts', 'node'],
    ],
    globals: false,
    css: false,
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    exclude: ['node_modules', 'dist', 'src/**/*.d.ts'],
  },
});