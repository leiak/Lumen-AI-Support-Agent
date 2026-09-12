import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { ArticleDetailPage } from '@/pages/article-detail';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import type * as ApiClient from '@/lib/api-client';

vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof ApiClient>('@/lib/api-client');
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

import type * as ReactRouterDom from 'react-router-dom';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof ReactRouterDom>('react-router-dom');
  return {
    ...actual,
    useNavigate: vi.fn(),
  };
});

const sampleArticle = {
  id: 'art_x',
  tenant_id: 'demo',
  knowledge_base_id: 'kb_x',
  title: '退换货流程',
  source_type: 'manual',
  source_uri: null,
  status: 'indexed',
  current_version_id: 'ver_1',
  error_message: null,
  created_at: '2026-09-08T10:00:00Z',
  updated_at: '2026-09-08T10:00:00Z',
  version: {
    id: 'ver_1',
    article_id: 'art_x',
    version_number: 1,
    content_hash: 'abc123',
    created_at: '2026-09-08T10:00:00Z',
    raw_text: '用户在购买 7 天内可以申请无理由退货。',
  },
};

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
      <MemoryRouter initialEntries={['/kb/kb_x/articles/art_x']}>
        <Routes>
          <Route
            path="/kb/:kbId/articles/:articleId"
            element={<ArticleDetailPage />}
          />
          <Route
            path="/kb/:kbId"
            element={<div data-testid="kb-detail-stub">kb detail</div>}
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
  vi.mocked(apiClient.patch).mockReset();
  vi.mocked(apiClient.delete).mockReset();
  vi.mocked(useNavigate).mockReturnValue(vi.fn());
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('ArticleDetailPage', () => {
  it('renders article metadata + status + raw_text', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: sampleArticle });
    renderPage();

    expect(await screen.findByText('退换货流程')).toBeInTheDocument();
    expect(screen.getByTestId('article-status-badge')).toHaveAttribute(
      'data-status',
      'indexed',
    );
    expect(screen.getByTestId('article-raw-text')).toHaveTextContent(
      '用户在购买 7 天内可以申请无理由退货。',
    );
  });

  it('reindex button submits POST /articles/{id}/reindex', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: sampleArticle });
    vi.mocked(apiClient.post).mockResolvedValue({
      data: {
        article_id: 'art_x',
        skipped: false,
        version_number: 2,
        status: 'indexed',
        chunks_indexed: 4,
      },
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByTestId('article-detail-reindex'));

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/knowledge/articles/art_x/reindex',
        expect.objectContaining({ force: true }),
      );
    });
  });

  it('reupload file input sends a multipart PATCH equivalent', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: sampleArticle });
    vi.mocked(apiClient.post).mockResolvedValue({
      data: {
        article_id: 'art_x',
        skipped: false,
        version_number: 2,
        status: 'indexing',
        chunks_indexed: 0,
      },
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByTestId('article-detail-reupload'));
    const fileInput = screen.getByTestId(
      'article-reupload-file',
    ) as HTMLInputElement;
    const file = new File(['updated body'], 'guide.txt', { type: 'text/plain' });
    await user.upload(fileInput, file);
    await user.click(screen.getByTestId('article-reupload-submit'));

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/knowledge/articles/art_x/upload',
        expect.any(FormData),
      );
    });
    const call = vi.mocked(apiClient.post).mock.calls[0];
    expect(call).toBeDefined();
    const formData = call![1] as FormData;
    expect(formData.get('file')).toBeInstanceOf(File);
    expect((formData.get('file') as File).name).toBe('guide.txt');
  });
});
