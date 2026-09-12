import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { apiClient } from '@/lib/api-client';
import type * as ApiClient from '@/lib/api-client';
import { ArticleUploadDialog } from '@/components/kb/article-upload-dialog';

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
  title: 'guide.md',
  source_type: 'upload',
  source_uri: null,
  status: 'draft',
  current_version_id: null,
  error_message: null,
  created_at: '2026-09-10T10:00:00Z',
  updated_at: '2026-09-10T10:00:00Z',
};

function makeFile(name: string, sizeBytes: number, type = 'text/plain'): File {
  // jsdom doesn't always honor size for empty buffers reliably, so we
  // pad a small repeated content string to the requested size. The
  // exact contents don't matter — the dialog only checks size + name.
  const seed = `${name}-payload`;
  const repeated = seed.repeat(Math.max(1, Math.ceil(sizeBytes / seed.length)));
  const content = repeated.slice(0, sizeBytes);
  return new File([content], name, { type });
}

describe('ArticleUploadDialog', () => {
  it('renders a file input', () => {
    render(
      <ArticleUploadDialog kbId="kb_x" open onOpenChange={() => undefined} />,
    );
    const fileInput = screen.getByTestId('article-upload-file');
    expect(fileInput).toBeInTheDocument();
    expect(fileInput).toHaveAttribute('type', 'file');
  });

  it('submits a multipart upload with the chosen file', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: createdArticle });
    const onCreated = vi.fn();
    const user = userEvent.setup();
    render(
      <ArticleUploadDialog
        kbId="kb_x"
        open
        onOpenChange={() => undefined}
        onCreated={onCreated}
      />,
    );

    const file = makeFile('guide.txt', 128, 'text/plain');
    const input = screen.getByTestId('article-upload-file') as HTMLInputElement;
    await user.upload(input, file);

    await user.click(screen.getByTestId('article-upload-submit'));

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/knowledge/knowledge-bases/kb_x/articles/upload',
        expect.any(FormData),
      );
    });
    const call = vi.mocked(apiClient.post).mock.calls[0];
    expect(call).toBeDefined();
    const formData = call![1] as FormData;
    expect(formData.get('file')).toBeInstanceOf(File);
    expect((formData.get('file') as File).name).toBe('guide.txt');
    expect(onCreated).toHaveBeenCalledWith(createdArticle.id);
  });

  it('rejects oversize files before submitting', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: createdArticle });
    const user = userEvent.setup();
    render(
      <ArticleUploadDialog kbId="kb_x" open onOpenChange={() => undefined} />,
    );

    const oversize = makeFile('huge.txt', 60 * 1024 * 1024, 'text/plain');
    const input = screen.getByTestId('article-upload-file') as HTMLInputElement;
    await user.upload(input, oversize);

    expect(screen.getByTestId('article-upload-oversize')).toBeInTheDocument();
    const submitButton = screen.getByTestId('article-upload-submit');
    expect(submitButton).toBeDisabled();

    await user.click(submitButton);
    expect(apiClient.post).not.toHaveBeenCalled();
  });
});
