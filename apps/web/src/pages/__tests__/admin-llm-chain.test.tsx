import { describe, expect, it, vi, beforeEach } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AdminLlmChainPage } from '@/pages/admin-llm-chain';
import { apiClient } from '@/lib/api-client';

// Tier 3 Task 3.4 — runtime LLM chain admin page smoke tests.
//
// We mock apiClient so the assertions don't need a real backend.
// The schema validation lives in src/lib/llm-chain.ts and the
// mutation validation lives on the backend (tested separately).

vi.mock('@/lib/use-is-admin', () => ({
  useIsAdmin: () => ({ isAdmin: true, isLoading: false }),
}));

vi.mock('@/lib/api-client', async () => {
  // eslint-disable-next-line @typescript-eslint/consistent-type-imports -- vi.importActual needs the generic
  const actual = await vi.importActual<typeof import('@/lib/api-client')>(
    '@/lib/api-client',
  );
  return {
    ...actual,
    apiClient: {
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
    },
  };
});

function mockGet(payload: unknown): void {
  vi.mocked(apiClient.get).mockResolvedValueOnce({ data: payload } as never);
}

function mockPut(payload: unknown): void {
  vi.mocked(apiClient.put).mockResolvedValueOnce({ data: payload } as never);
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false },
    },
  });
}

function renderPage(): void {
  render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter>
        <AdminLlmChainPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('AdminLlmChainPage (Tier 3 Task 3.4)', () => {
  it('shows empty state when no chain exists', async () => {
    mockGet({ steps: [], providers: [] });
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId('chain-empty')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('providers-empty')).toBeInTheDocument();
  });

  it('renders chain rows + provider list', async () => {
    mockGet({
      steps: [
        { provider: 'minimax', model: 'MiniMax-M3', enabled: true },
        { provider: 'anthropic', model: 'claude-3-5-sonnet', enabled: false },
      ],
      providers: [
        { name: 'minimax', model: 'MiniMax-M3', is_default: true },
        { name: 'anthropic', model: 'claude-3-5-sonnet', is_default: false },
      ],
    });
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId('chain-row-minimax')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('chain-row-anthropic')).toBeInTheDocument();
    expect(screen.getByTestId('enabled-count')).toHaveTextContent('1/2');
    expect(screen.getByTestId('providers-list')).toHaveTextContent('minimax');
  });

  it('disables the save button when nothing changed', async () => {
    mockGet({
      steps: [
        { provider: 'minimax', model: 'MiniMax-M3', enabled: true },
      ],
      providers: [{ name: 'minimax', model: 'MiniMax-M3', is_default: true }],
    });
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId('chain-row-minimax')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('chain-save')).toBeDisabled();
  });

  it('reorders rows via the move buttons', async () => {
    mockGet({
      steps: [
        { provider: 'minimax', model: 'a', enabled: true },
        { provider: 'anthropic', model: 'b', enabled: true },
      ],
      providers: [],
    });
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId('chain-row-minimax')).toBeInTheDocument(),
    );
    await userEvent.setup().click(
      screen.getByTestId('chain-down-minimax'),
    );
    // After moving minimax down, the save button is enabled.
    expect(screen.getByTestId('chain-save')).not.toBeDisabled();
    // The first badge should now read #1 for anthropic.
    expect(
      screen.getByTestId('chain-row-anthropic').textContent,
    ).toContain('#1');
  });

  it('toggles a step on/off', async () => {
    mockGet({
      steps: [
        { provider: 'minimax', model: 'a', enabled: true },
      ],
      providers: [],
    });
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId('chain-row-minimax')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('enabled-count')).toHaveTextContent('1/1');
    await userEvent.setup().click(screen.getByTestId('chain-toggle-minimax'));
    expect(screen.getByTestId('enabled-count')).toHaveTextContent('0/1');
    expect(screen.getByTestId('chain-save')).toBeDisabled();
  });

  it('sends the new chain via PUT when Save is clicked', async () => {
    mockGet({
      steps: [
        { provider: 'minimax', model: 'a', enabled: true },
        { provider: 'anthropic', model: 'b', enabled: true },
      ],
      providers: [],
    });
    mockPut({
      steps: [
        { provider: 'anthropic', model: 'b', enabled: true },
        { provider: 'minimax', model: 'a', enabled: true },
      ],
      providers: [],
    });
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId('chain-row-minimax')).toBeInTheDocument(),
    );
    const user = userEvent.setup();
    await user.click(screen.getByTestId('chain-down-minimax'));
    await user.click(screen.getByTestId('chain-save'));
    await waitFor(() =>
      expect(vi.mocked(apiClient.put)).toHaveBeenCalledWith(
        '/api/v1/admin/llm-chain',
        expect.objectContaining({
          steps: expect.arrayContaining([
            expect.objectContaining({ provider: 'anthropic' }),
            expect.objectContaining({ provider: 'minimax' }),
          ]),
        }),
      ),
    );
  });
});