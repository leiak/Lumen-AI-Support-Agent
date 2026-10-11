import { describe, expect, it, vi, beforeEach } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AdminSearchPage } from '@/pages/admin-search';
import { apiClient } from '@/lib/api-client';

// Tier 3 Task 3.3 — Cross-domain search page smoke tests.
//
// We mock the network layer (apiClient.get) so the assertions are
// fast + deterministic. The schema validation lives in
// ``src/lib/search.ts`` and is exercised by the backend integration
// suite.

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
      patch: vi.fn(),
      delete: vi.fn(),
    },
  };
});

function mockSearch(payload: unknown): void {
  vi.mocked(apiClient.get).mockResolvedValueOnce({ data: payload } as never);
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false },
    },
  });
}

function renderPage(initialPath = '/admin/search'): void {
  render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/admin/search" element={<AdminSearchPage />} />
          <Route path="/inbox" element={<div data-testid="inbox-landing" />} />
          <Route path="/kb/:id" element={<div data-testid="kb-landing" />} />
          <Route
            path="/admin/tickets/:id"
            element={<div data-testid="ticket-landing" />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('AdminSearchPage (Tier 3 Task 3.3)', () => {
  it('renders the empty-state prompt when no q', () => {
    renderPage();
    expect(screen.getByTestId('search-input')).toBeInTheDocument();
    expect(screen.getByText('请输入关键词以开始搜索。')).toBeInTheDocument();
  });

  it('renders grouped hits when the API returns matches', async () => {
    mockSearch({
      query: 'password',
      total: 2,
      results: [
        {
          kind: 'article',
          id: 'art-1',
          title: 'How to reset your password',
          subtitle: 'KB · indexed',
          snippet: 'Visit /reset to change your password.',
          extra: { knowledge_base_id: 'kb-1' },
        },
        {
          kind: 'ticket',
          id: 'tkt-1',
          title: 'Refund request',
          subtitle: 'new · p3',
          snippet: 'Refund request',
          extra: { status: 'new', priority: 'p3' },
        },
      ],
    });
    renderPage('/admin/search?q=password');
    await waitFor(() =>
      expect(screen.getByTestId('search-results')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('search-group-article')).toHaveTextContent('文章');
    expect(screen.getByTestId('search-group-ticket')).toHaveTextContent('工单');
    expect(screen.getByTestId('search-hit-art-1')).toHaveTextContent('password');
    expect(screen.getByTestId('search-hit-tkt-1')).toHaveTextContent('Refund');
    expect(screen.getByText(/共 2 条结果/)).toBeInTheDocument();
  });

  it('shows the empty state when API returns zero hits', async () => {
    mockSearch({ query: 'zzz', total: 0, results: [] });
    renderPage('/admin/search?q=zzz');
    await waitFor(() =>
      expect(screen.getByTestId('search-empty')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('search-empty')).toHaveTextContent('zzz');
  });

  it('surfaces an error message when the API throws', async () => {
    vi.mocked(apiClient.get).mockRejectedValueOnce(new Error('boom'));
    renderPage('/admin/search?q=fail');
    await waitFor(() =>
      expect(screen.getByTestId('search-error')).toBeInTheDocument(),
    );
  });

  it('navigates to the new query when the form is submitted', async () => {
    mockSearch({
      query: 'transmogrifier',
      total: 1,
      results: [
        {
          kind: 'article',
          id: 'art-2',
          title: 'Transmogrifier setup',
          subtitle: 'KB · indexed',
          snippet: null,
          extra: {},
        },
      ],
    });
    renderPage('/admin/search');
    const user = userEvent.setup();
    await user.type(screen.getByTestId('search-input'), 'transmogrifier');
    await user.click(screen.getByTestId('search-submit'));
    await waitFor(() =>
      expect(vi.mocked(apiClient.get)).toHaveBeenCalledWith(
        expect.stringContaining('q=transmogrifier'),
      ),
    );
  });

  it('article hits deep-link to /kb/:id', async () => {
    mockSearch({
      query: 'password',
      total: 1,
      results: [
        {
          kind: 'article',
          id: 'art-deep',
          title: 'Password reset',
          subtitle: 'KB · indexed',
          snippet: null,
          extra: {},
        },
      ],
    });
    renderPage('/admin/search?q=password');
    await waitFor(() =>
      expect(screen.getByTestId('search-hit-art-deep')).toBeInTheDocument(),
    );
    const link = screen.getByTestId('search-hit-art-deep').querySelector('a');
    expect(link?.getAttribute('href')).toBe('/kb/art-deep');
  });
});