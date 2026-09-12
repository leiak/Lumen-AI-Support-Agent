import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { ArticleList } from '@/components/kb/article-list';
import { JWT_STORAGE_KEY } from '@/lib/api-client';
import type { Article } from '@/lib/knowledge';

const sampleArticle = (
  overrides: Partial<Article> = {},
): Article => ({
  id: '01HZX8K1M5R7N3W2Q9P0ARTAAAAAA',
  tenant_id: 'demo',
  knowledge_base_id: '01HZX8K1M5R7N3W2Q9P0KBAAAAAA',
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

function renderList(props: Partial<React.ComponentProps<typeof ArticleList>> = {}): void {
  render(
    <MemoryRouter>
      <ArticleList
        articles={[]}
        kbId="kb_x"
        onDelete={() => undefined}
        {...props}
      />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('ArticleList', () => {
  it('renders article rows with status badges', () => {
    renderList({
      articles: [
        sampleArticle({ id: 'a1', title: '退换货流程', status: 'draft' }),
        sampleArticle({ id: 'a2', title: '保修政策', status: 'indexed' }),
      ],
    });
    const rows = screen.getAllByTestId('article-row');
    expect(rows).toHaveLength(2);
    expect(screen.getByText('退换货流程')).toBeInTheDocument();
    expect(screen.getByText('保修政策')).toBeInTheDocument();
    const badges = screen.getAllByTestId('article-status-badge');
    expect(badges).toHaveLength(2);
    expect(badges[0]).toHaveAttribute('data-status', 'draft');
    expect(badges[1]).toHaveAttribute('data-status', 'indexed');
  });

  it('renders a distinct badge per lifecycle status', () => {
    renderList({
      articles: [
        sampleArticle({ id: 'a1', title: '草稿样本', status: 'draft' }),
        sampleArticle({ id: 'a2', title: '索引样本', status: 'indexing' }),
        sampleArticle({ id: 'a3', title: '就绪样本', status: 'indexed' }),
        sampleArticle({ id: 'a4', title: '失败样本', status: 'failed' }),
      ],
    });
    const badges = screen.getAllByTestId('article-status-badge');
    expect(badges).toHaveLength(4);
    const dataStatuses = badges.map((b) => b.getAttribute('data-status'));
    expect(dataStatuses).toEqual(['draft', 'indexing', 'indexed', 'failed']);
    expect(screen.getByText('草稿')).toBeInTheDocument();
    expect(screen.getByText('索引中')).toBeInTheDocument();
    expect(screen.getByText('已索引')).toBeInTheDocument();
    expect(screen.getByText('失败')).toBeInTheDocument();
  });

  it('renders the loading skeleton and the empty state', () => {
    const { rerender } = render(
      <MemoryRouter>
        <ArticleList
          articles={[]}
          kbId="kb_x"
          isLoading
          onDelete={() => undefined}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('article-list-loading')).toBeInTheDocument();

    rerender(
      <MemoryRouter>
        <ArticleList articles={[]} kbId="kb_x" onDelete={() => undefined} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('article-list-empty')).toBeInTheDocument();
  });

  it('opens the delete confirmation when the row delete button is clicked', async () => {
    const onDelete = vi.fn();
    const user = userEvent.setup();
    renderList({
      articles: [sampleArticle({ id: 'a1', title: '退换货流程' })],
      onDelete,
    });
    const row = screen.getByTestId('article-row');
    const deleteButton = within(row).getByTestId('article-row-delete');
    await user.click(deleteButton);
    // The parent owns the actual confirmation dialog — the row just
    // hands the article back so it can mount the dialog.
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(onDelete).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'a1', title: '退换货流程' }),
    );
  });
});
