import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { KbDetailPage } from '@/pages/kb-detail';
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

const sampleKb = {
  id: 'kb_x',
  tenant_id: 'demo',
  name: '产品 FAQ',
  slug: 'product-faq',
  description: null,
  embedding_model: 'text-embedding-3-small',
  chunk_size: 800,
  chunk_overlap: 100,
  created_at: '2026-09-08T10:00:00Z',
  updated_at: '2026-09-08T10:00:00Z',
};

const sampleArticle = (overrides: Partial<Record<string, unknown>> = {}) => ({
  id: 'art_x',
  tenant_id: 'demo',
  knowledge_base_id: 'kb_x',
  title: '退换货流程',
  source_type: 'manual',
  source_uri: null,
  status: 'draft',
  current_version_id: null,
  error_message: null,
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
      <MemoryRouter initialEntries={['/kb/kb_x']}>
        <Routes>
          <Route path="/kb/:kbId" element={<KbDetailPage />} />
          <Route
            path="/kb/:kbId/articles/:articleId"
            element={<div data-testid="article-detail-stub">article</div>}
          />
          <Route
            path="/kb"
            element={<div data-testid="kb-list-stub">kb-list</div>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function mockKbAndArticles(articles: unknown[] = []): void {
  vi.mocked(apiClient.get).mockImplementation((url) => {
    if (typeof url === 'string' && url.endsWith('/knowledge-bases/kb_x')) {
      return Promise.resolve({ data: sampleKb });
    }
    if (typeof url === 'string' && url.endsWith('/articles')) {
      return Promise.resolve({ data: { items: articles } });
    }
    return Promise.reject(new Error(`unexpected GET ${url as string}`));
  });
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

describe('KbDetailPage', () => {
  it('renders the article list and the upload + new-article buttons', async () => {
    mockKbAndArticles([sampleArticle()]);
    renderPage();

    expect(await screen.findByTestId('kb-detail-name')).toHaveTextContent('产品 FAQ');
    expect(screen.getByTestId('kb-detail-upload')).toBeInTheDocument();
    expect(screen.getByTestId('kb-detail-new-article')).toBeInTheDocument();
    expect(await screen.findByTestId('article-row')).toBeInTheDocument();
  });

  it('opens the upload dialog when the upload button is clicked', async () => {
    mockKbAndArticles([]);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('kb-detail-upload'));
    expect(screen.getByTestId('article-upload-dialog')).toBeInTheDocument();
  });

  it('submits an upload and the new row shows up in the list', async () => {
    const newArticle = sampleArticle({
      id: 'art_new',
      title: 'guide.txt',
      status: 'indexing',
    });
    mockKbAndArticles([]);

    vi.mocked(apiClient.post).mockResolvedValueOnce({ data: newArticle });

    // After the upload, the articles list should refetch and include
    // the new article.
    const getMock = vi.mocked(apiClient.get);
    getMock.mockImplementation((url) => {
      if (typeof url === 'string' && url.endsWith('/knowledge-bases/kb_x')) {
        return Promise.resolve({ data: sampleKb });
      }
      if (typeof url === 'string' && url.endsWith('/articles')) {
        // First call (initial): empty. Subsequent calls (post-upload
        // invalidation): include the new article.
        const seen = (getMock.mock.calls as unknown[][]).filter(
          (call) => typeof call[0] === 'string' && (call[0] as string).endsWith('/articles'),
        ).length;
        if (seen <= 1) {
          return Promise.resolve({ data: { items: [] } });
        }
        return Promise.resolve({ data: { items: [newArticle] } });
      }
      return Promise.reject(new Error(`unexpected GET ${url as string}`));
    });

    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId('kb-detail-upload'));

    const fileInput = screen.getByTestId('article-upload-file') as HTMLInputElement;
    const file = new File(['hello'], 'guide.txt', { type: 'text/plain' });
    await user.upload(fileInput, file);

    await user.click(screen.getByTestId('article-upload-submit'));

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/knowledge/knowledge-bases/kb_x/articles/upload',
        expect.any(FormData),
      );
    });
    expect(await screen.findByText('guide.txt')).toBeInTheDocument();
  });

  it('removes a row after delete confirmation', async () => {
    const article = sampleArticle({ id: 'art_x', title: '退换货流程' });
    mockKbAndArticles([article]);
    vi.mocked(apiClient.delete).mockResolvedValue({ data: null });

    // After delete, refetch returns an empty list.
    const getMock = vi.mocked(apiClient.get);
    getMock.mockImplementation((url) => {
      if (typeof url === 'string' && url.endsWith('/knowledge-bases/kb_x')) {
        return Promise.resolve({ data: sampleKb });
      }
      if (typeof url === 'string' && url.endsWith('/articles')) {
        const seen = (getMock.mock.calls as unknown[][]).filter(
          (call) => typeof call[0] === 'string' && (call[0] as string).endsWith('/articles'),
        ).length;
        if (seen <= 1) {
          return Promise.resolve({ data: { items: [article] } });
        }
        return Promise.resolve({ data: { items: [] } });
      }
      return Promise.reject(new Error(`unexpected GET ${url as string}`));
    });

    const user = userEvent.setup();
    renderPage();

    const row = await screen.findByTestId('article-row');
    expect(within(row).getByText('退换货流程')).toBeInTheDocument();

    await user.click(within(row).getByTestId('article-row-delete'));
    const dialog = await screen.findByTestId('article-delete-dialog');
    await user.click(within(dialog).getByTestId('article-delete-confirm'));

    await waitFor(() => {
      expect(apiClient.delete).toHaveBeenCalledWith(
        '/api/v1/knowledge/articles/art_x',
      );
    });
    await waitFor(() =>
      expect(screen.queryByText('退换货流程')).not.toBeInTheDocument(),
    );
  });
});
