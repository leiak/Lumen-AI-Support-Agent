import { defineConfig } from 'vitest/config';

// `__IFRAME_HTML__` is injected into the production bundle via esbuild's
// `define` (see esbuild.config.mjs). During tests esbuild transpiles each
// file fresh, so we mirror the define here — otherwise widget-frame.ts
// would `ReferenceError` the moment any test clicks the floating button.
// The empty-string stub is fine: sdk.test.ts never inspects iframe.srcdoc.
export default defineConfig({
  define: {
    __IFRAME_HTML__: JSON.stringify(''),
  },
  test: {
    environment: 'jsdom',
    globals: false,
    css: false,
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    exclude: ['node_modules', 'dist'],
  },
});