import { defineConfig } from 'vite';
import { viteStaticCopy } from 'vite-plugin-static-copy';

export default defineConfig({
  root: 'src/widget-demo',
  server: {
    port: 5173,
    strictPort: true,
  },
  plugins: [
    viteStaticCopy({
      targets: [
        {
          src: '../../../web-sdk/dist/*',
          dest: '.',
        },
      ],
    }),
  ],
});