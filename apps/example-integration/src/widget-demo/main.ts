// Expose SDK config before the IIFE script tag (in index.html) initializes.
declare global {
  interface Window {
    LumenAICustomerConfig: {
      apiBaseUrl: string;
      channelId: string;
      tenantId: string;
      externalUserId: string;
      accentColor: string;
      title: string;
      subtitle: string;
      position: 'bottom-right' | 'bottom-left';
    };
    LumenAICustomer?: {
      open(): void;
      close(): void;
      destroy(): void;
    };
  }
}

window.LumenAICustomerConfig = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000',
  channelId: import.meta.env.VITE_CHANNEL_ID ?? '01HZDEMO00000000000000003',
  tenantId: import.meta.env.VITE_TENANT_ID ?? '01HZDEMO00000000000000000',
  externalUserId: `demo-visitor-${crypto.randomUUID()}`,
  accentColor: '#0ea5e9',
  title: 'AI 客服',
  subtitle: '7×24 在线',
  position: 'bottom-right',
};

// Convenience: log SDK readiness to the console so users can verify init.
window.addEventListener('load', () => {
  setTimeout(() => {
    const sdk = window.LumenAICustomer;
    if (sdk) {
      console.info('[example-integration] SDK ready:', Object.keys(sdk));
    } else {
      console.warn('[example-integration] window.LumenAICustomer not found — check lumen-widget.js loaded');
    }
  }, 1500);
});

// Load the SDK AFTER the config is set. A static <script async> in
// index.html has a race: the SDK IIFE may run before this module
// finishes evaluating, see window.LumenAICustomerConfig as undefined,
// and throw. Injecting the tag here guarantees the config is in place
// first.
const sdk = document.createElement('script');
sdk.src = './lumen-widget.js';
sdk.async = false;
document.body.appendChild(sdk);