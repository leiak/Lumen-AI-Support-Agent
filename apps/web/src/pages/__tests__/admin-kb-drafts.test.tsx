import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AdminKbDraftsPage } from '@/pages/admin-kb-drafts';
import { apiClient } from '@/lib/api-client';

vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api-client')>(
    '@/lib/api-client',
  );
  return {
    ...actual,
    apiClient: { get: vi.fn(), post: vi.fn() },
  };
});

const mockedGet = vi.mocked(apiClient.get);
const mockedPost = vi.mocked(apiClient.post);

beforeEach(() => {
  mockedGet.mockReset();
  mockedPost.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

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
        <AdminKbDraftsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('AdminKbDraftsPage', () => {
  it('fetches drafts on mount with default DRAFT status', async () => {
    mockedGet.mockResolvedValue({
      data: {
        drafts: [
          {
            id: 'd1',
            title: 'Reset Password',
            body_preview: 'Go to settings...',
            tags: ['account'],
            source_question_count: 5,
            cluster_id: 3,
            status: 'DRAFT',
            created_at: '2026-09-20T10:00:00Z',
          },
        ],
      },
    });

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('draft-card-d1')).toBeInTheDocument();
    });
    expect(mockedGet).toHaveBeenCalledWith('/api/v1/admin/kb-drafts', {
      params: { status: 'DRAFT', limit: 50 },
    });
  });

  it('refetches with new status when tab clicked', async () => {
    mockedGet.mockResolvedValue({ data: { drafts: [] } });
    renderPage();

    await waitFor(() => screen.getByTestId('status-tab-APPROVED'));
    const user = userEvent.setup();
    await user.click(screen.getByTestId('status-tab-APPROVED'));

    await waitFor(() => {
      expect(mockedGet).toHaveBeenCalledWith('/api/v1/admin/kb-drafts', {
        params: { status: 'APPROVED', limit: 50 },
      });
    });
  });

  it('approving a draft refetches the list', async () => {
    mockedGet.mockResolvedValue({
      data: {
        drafts: [
          {
            id: 'd1',
            title: 'T',
            body_preview: 'b',
            tags: [],
            source_question_count: 1,
            cluster_id: null,
            status: 'DRAFT',
            created_at: '2026-09-20T10:00:00Z',
          },
        ],
      },
    });
    mockedPost.mockResolvedValue({
      data: { draft_id: 'd1', article_id: 'a1', status: 'APPROVED' },
    });

    renderPage();

    await waitFor(() => screen.getByTestId('draft-approve-d1'));
    const user = userEvent.setup();
    await user.click(screen.getByTestId('draft-approve-d1'));

    await waitFor(() => {
      expect(mockedPost).toHaveBeenCalledWith(
        '/api/v1/admin/kb-drafts/d1/approve',
      );
    });
  });

  it('shows empty message when no drafts', async () => {
    mockedGet.mockResolvedValue({ data: { drafts: [] } });
    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('drafts-empty')).toBeInTheDocument();
    });
  });
});