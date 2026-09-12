import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { KbPage } from '@/pages/kb';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import type * as ApiClient from '@/lib/api-client';

vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof ApiClient>('@/lib/api-client');
  return {
    ...actual,
    apiClient: {
      get: vi.fn(),
      post: vi.fn(),
      delete: vi.fn(),
    },
  };
});

import type * as ReactRouterDom from 'react-router-dom';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof ReactRouterDom>('react-router-dom');
  return {
    ...actual,
    useNavigate: vi.fn(),
  };
});

const sampleKb = (overrides: Partial<Record<string, unknown>> = {}) => ({
  id: '01HZX8K1M5R7N3W2Q9P0KBAAAAAA',
  tenant_id: 'demo',
  name: '产品 FAQ',
  slug: 'product-faq',
  description: null,
  embedding_model: 'text-embedding-3-small',
  chunk_size: 800,
  chunk_overlap: 100,
  created_at: '2026-09-08T10:00:00Z',
  updated_at: '2026-09-08T10:00:00Z',
  ...overrides,
});

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false },
    },
  });
}

function renderPage(): void {
  const client = makeQueryClient();
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/kb']}>
        <Routes>
          <Route path="/kb" element={<KbPage />} />
          <Route
            path="/kb/:kbId"
            element={<div data-testid="kb-detail-stub">detail</div>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  vi.mocked(apiClient.get).mockReset();
  vi.mocked(apiClient.post).mockReset();
  vi.mocked(apiClient.delete).mockReset();
  vi.mocked(useNavigate).mockReturnValue(vi.fn());
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('KbPage', () => {
  it('renders the list and the create button', async () => {
    vi.mocked(apiClient.get).mockImplementation((url) => {
      if (typeof url === 'string' && url.endsWith('/knowledge-bases')) {
        return Promise.resolve({ data: { items: [sampleKb()] } });
      }
      // article counts: empty array
      return Promise.resolve({ data: { items: [] } });
    });
    renderPage();
    expect(screen.getByTestId('kb-create-open')).toBeInTheDocument();
    expect(await screen.findByTestId('kb-list')).toBeInTheDocument();
    expect(screen.getByText('产品 FAQ')).toBeInTheDocument();
  });

  it('opens the create dialog when the create button is clicked', async () => {
    vi.mocked(apiClient.get).mockImplementation((url) => {
      if (typeof url === 'string' && url.endsWith('/knowledge-bases')) {
        return Promise.resolve({ data: { items: [] } });
      }
      return Promise.resolve({ data: { items: [] } });
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByTestId('kb-create-open'));
    expect(screen.getByTestId('kb-create-dialog')).toBeInTheDocument();
  });

  it('submits the create dialog and navigates to the new KB', async () => {
    vi.mocked(apiClient.get).mockImplementation((url) => {
      if (typeof url === 'string' && url.endsWith('/knowledge-bases')) {
        return Promise.resolve({ data: { items: [] } });
      }
      return Promise.resolve({ data: { items: [] } });
    });
    vi.mocked(apiClient.post).mockResolvedValue({
      data: sampleKb({ id: 'kb_new', name: '新知识库' }),
    });
    const navigate = vi.fn();
    vi.mocked(useNavigate).mockReturnValue(navigate);
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByTestId('kb-create-open'));
    await user.type(screen.getByTestId('kb-create-name'), '新知识库');
    await user.click(screen.getByTestId('kb-create-submit'));

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/knowledge/knowledge-bases',
        expect.objectContaining({ name: '新知识库' }),
      );
    });
    // Dialog closes on success.
    await waitFor(() =>
      expect(screen.queryByTestId('kb-create-dialog')).not.toBeInTheDocument(),
    );
    expect(navigate).toHaveBeenCalledWith('/kb/kb_new');
  });
});
