import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { apiClient } from '@/lib/api-client';
import type * as ApiClient from '@/lib/api-client';
import { ArticleCreateDialog } from '@/components/kb/article-create-dialog';

vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof ApiClient>('@/lib/api-client');
  return {
    ...actual,
    apiClient: {
      post: vi.fn(),
    },
  };
});

beforeEach(() => {
  vi.mocked(apiClient.post).mockReset();
});

afterEach(() => {
  cleanup();
});

const createdArticle = {
  id: '01HZX8K1M5R7N3W2Q9P0ARTAAAAAA',
  tenant_id: 'demo',
  knowledge_base_id: 'kb_x',
  title: '退换货流程',
  source_type: 'manual',
  source_uri: null,
  status: 'draft',
  current_version_id: null,
  error_message: null,
  created_at: '2026-09-10T10:00:00Z',
  updated_at: '2026-09-10T10:00:00Z',
};

describe('ArticleCreateDialog', () => {
  it('renders the title, source-type and raw-text fields', () => {
    render(
      <ArticleCreateDialog
        kbId="kb_x"
        open
        onOpenChange={() => undefined}
      />,
    );
    expect(screen.getByTestId('article-create-dialog')).toBeInTheDocument();
    expect(screen.getByTestId('article-create-title')).toBeInTheDocument();
    expect(screen.getByTestId('article-create-source')).toBeInTheDocument();
    expect(screen.getByTestId('article-create-text')).toBeInTheDocument();
  });

  it('submits a POST /articles request with the form values', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: createdArticle });
    const onCreated = vi.fn();
    const user = userEvent.setup();
    render(
      <ArticleCreateDialog
        kbId="kb_x"
        open
        onOpenChange={() => undefined}
        onCreated={onCreated}
      />,
    );

    await user.type(screen.getByTestId('article-create-title'), '退换货流程');
    await user.type(
      screen.getByTestId('article-create-text'),
      '用户在购买 7 天内可以申请无理由退货。',
    );
    await user.click(screen.getByTestId('article-create-submit'));

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/knowledge/knowledge-bases/kb_x/articles',
        expect.objectContaining({
          title: '退换货流程',
          source_type: 'manual',
          raw_text: '用户在购买 7 天内可以申请无理由退货。',
        }),
      );
    });
    expect(onCreated).toHaveBeenCalledWith(createdArticle.id);
  });

  it('blocks submit when the title is empty', async () => {
    const user = userEvent.setup();
    render(
      <ArticleCreateDialog
        kbId="kb_x"
        open
        onOpenChange={() => undefined}
      />,
    );
    // The title input has the `required` attribute — submit must
    // refuse to proceed and the POST must never fire.
    const submitButton = screen.getByTestId('article-create-submit');
    expect(submitButton).toBeDisabled();
    await user.click(submitButton);
    expect(apiClient.post).not.toHaveBeenCalled();
  });
});
