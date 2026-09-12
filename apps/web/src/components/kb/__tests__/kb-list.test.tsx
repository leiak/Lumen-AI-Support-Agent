import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { KbList } from '@/components/kb/kb-list';
import { JWT_STORAGE_KEY } from '@/lib/api-client';
import type { KnowledgeBase } from '@/lib/knowledge';

const sampleKb = (
  overrides: Partial<KnowledgeBase> = {},
): KnowledgeBase => ({
  id: '01HZX8K1M5R7N3W2Q9P0KBAAAAAA',
  tenant_id: 'demo',
  name: '产品 FAQ',
  slug: 'product-faq',
  description: '常见问题集合',
  embedding_model: 'text-embedding-3-small',
  chunk_size: 800,
  chunk_overlap: 100,
  created_at: '2026-09-08T10:00:00Z',
  updated_at: '2026-09-08T10:00:00Z',
  ...overrides,
});

function wrap(ui: React.ReactElement): React.ReactElement {
  return <MemoryRouter>{ui}</MemoryRouter>;
}

beforeEach(() => {
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('KbList', () => {
  it('renders a row for each knowledge base', () => {
    render(
      wrap(
        <KbList
          knowledgeBases={[
            sampleKb({ id: 'kb_a', name: '产品 FAQ' }),
            sampleKb({ id: 'kb_b', name: '退换货政策' }),
          ]}
          articleCounts={{ kb_a: 3, kb_b: 0 }}
          onDelete={() => undefined}
        />,
      ),
    );
    const rows = screen.getAllByTestId('kb-row');
    expect(rows).toHaveLength(2);
    expect(screen.getByText('产品 FAQ')).toBeInTheDocument();
    expect(screen.getByText('退换货政策')).toBeInTheDocument();
    expect(screen.getAllByText('text-embedding-3-small').length).toBeGreaterThan(0);
    expect(screen.getByText('3 篇')).toBeInTheDocument();
  });

  it('renders the skeleton state when loading', () => {
    render(
      wrap(
        <KbList knowledgeBases={[]} isLoading onDelete={() => undefined} />,
      ),
    );
    expect(screen.getByTestId('kb-list-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('kb-row')).not.toBeInTheDocument();
  });

  it('renders the empty state when no KBs exist', () => {
    render(
      wrap(<KbList knowledgeBases={[]} onDelete={() => undefined} />),
    );
    expect(screen.getByTestId('kb-list-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('kb-row')).not.toBeInTheDocument();
  });

  it('triggers onDelete when a row delete button is clicked', () => {
    const onDelete = vi.fn();
    render(
      wrap(
        <KbList
          knowledgeBases={[sampleKb({ id: 'kb_a', name: '产品 FAQ' })]}
          onDelete={onDelete}
        />,
      ),
    );
    onDelete.mockClear();
    const deleteButton = screen.getByTestId('kb-row-delete');
    deleteButton.click();
    expect(onDelete).toHaveBeenCalledTimes(1);
  });
});
