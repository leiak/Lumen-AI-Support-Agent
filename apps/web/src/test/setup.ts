import '@testing-library/jest-dom/vitest';
import { createElement, type ReactElement } from 'react';
import {
  QueryClient,
  QueryClientProvider,
} from '@tanstack/react-query';
import { cleanup, render, type RenderOptions, type RenderResult } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, vi } from 'vitest';

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

/**
 * Render a React element wrapped in a fresh ``QueryClientProvider`` and
 * ``MemoryRouter``. Use this in any test that mounts a component which
 * calls ``useQuery`` directly (e.g. ``ConversationActions`` from
 * ``@/components/conversation/conversation-header``). Centralised here
 * so future QueryClient knobs (logging failed queries, custom retries,
 * etc.) land in one place instead of being duplicated per test file.
 *
 * @param ui The component to render
 * @param options.initialEntries Passed through to ``MemoryRouter``.
 *   Defaults to ``['/']``.
 * @param options.queryClient Pass a pre-built ``QueryClient`` if a test
 *   needs to seed cache or inspect query state; otherwise a fresh
 *   client with ``retry: false`` + ``staleTime: 0`` is created and
 *   discarded.
 */
export function renderWithProviders(
  ui: ReactElement,
  options: {
    initialEntries?: string[];
    queryClient?: QueryClient;
  } = {},
): RenderResult & { queryClient: QueryClient } {
  const client =
    options.queryClient ??
    new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false },
      },
    });
  const Wrapper: RenderOptions['wrapper'] = ({ children }) =>
    createElement(
      QueryClientProvider,
      { client },
      createElement(
        MemoryRouter,
        { initialEntries: options.initialEntries ?? ['/'] },
        children,
      ),
    );
  const result = render(ui, { wrapper: Wrapper });
  return Object.assign(result, { queryClient: client });
}

afterEach(() => {
  cleanup();
  if (typeof window !== 'undefined') {
    window.localStorage.clear();
  }
});
