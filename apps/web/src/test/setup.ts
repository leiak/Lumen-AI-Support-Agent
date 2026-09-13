import '@testing-library/jest-dom/vitest';
import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

// jsdom does not implement matchMedia; shadcn/ui components rely on it.
// The ``typeof window`` guard lets this setup file run safely in
// tests that opt into the node environment (e.g.
// ``src/__tests__/proxy.test.ts`` which exercises the Vite config).
if (typeof window !== 'undefined') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

afterEach(() => {
  cleanup();
  if (typeof window !== 'undefined') {
    window.localStorage.clear();
  }
});