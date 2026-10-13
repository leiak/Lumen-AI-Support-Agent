import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  approveDraft,
  fetchDraft,
  fetchDrafts,
  KbDraftDetailSchema,
  rejectDraft,
  triggerHistoryMining,
} from '@/lib/kb-drafts';
import { apiClient } from '@/lib/api-client';

vi.mock('@/lib/api-client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

const mockedGet = vi.mocked(apiClient.get);
const mockedPost = vi.mocked(apiClient.post);

beforeEach(() => {
  mockedGet.mockReset();
  mockedPost.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('fetchDrafts', () => {
  it('parses a valid list response', async () => {
    mockedGet.mockResolvedValueOnce({
      data: {
        drafts: [
          {
            id: 'd1',
            title: 'How to reset password',
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
    const result = await fetchDrafts();
    expect(result.drafts).toHaveLength(1);
    expect(result.drafts[0]?.title).toBe('How to reset password');
    expect(mockedGet).toHaveBeenCalledWith('/api/v1/admin/kb-drafts', {
      params: { status: 'DRAFT', limit: 50 },
    });
  });

  it('rejects malformed payload', async () => {
    mockedGet.mockResolvedValueOnce({ data: { drafts: [{ missing: 'fields' }] } });
    await expect(fetchDrafts()).rejects.toThrow();
  });
});

describe('fetchDraft', () => {
  it('parses a detail response with full body', async () => {
    mockedGet.mockResolvedValueOnce({
      data: {
        id: 'd1',
        title: 'T',
        body_preview: 'short',
        body: 'long body content',
        tags: [],
        source_question_count: 1,
        cluster_id: null,
        status: 'DRAFT',
        created_at: '2026-09-20T10:00:00Z',
        reviewed_at: null,
        reviewed_by: null,
      },
    });
    const draft = await fetchDraft('d1');
    expect(draft.body).toBe('long body content');
    expect(KbDraftDetailSchema.parse(draft)).toBeTruthy();
  });
});

describe('approveDraft', () => {
  it('POSTs to the approve endpoint and parses the result', async () => {
    mockedPost.mockResolvedValueOnce({
      data: { draft_id: 'd1', article_id: 'a1', status: 'APPROVED' },
    });
    const result = await approveDraft('d1');
    expect(result.article_id).toBe('a1');
    expect(result.status).toBe('APPROVED');
    expect(mockedPost).toHaveBeenCalledWith('/api/v1/admin/kb-drafts/d1/approve');
  });
});

describe('rejectDraft', () => {
  it('POSTs to the reject endpoint', async () => {
    mockedPost.mockResolvedValueOnce({
      data: { draft_id: 'd1', status: 'REJECTED' },
    });
    const result = await rejectDraft('d1');
    expect(result.status).toBe('REJECTED');
    expect(mockedPost).toHaveBeenCalledWith('/api/v1/admin/kb-drafts/d1/reject');
  });
});

describe('triggerHistoryMining', () => {
  it('POSTs to run-mining and parses the result (worker_started=true)', async () => {
    mockedPost.mockResolvedValueOnce({
      data: {
        enqueued: true,
        worker_started: true,
        function: 'history_mining_worker',
        job_id: 'job-1',
      },
    });
    const result = await triggerHistoryMining();
    expect(result.enqueued).toBe(true);
    expect(result.worker_started).toBe(true);
    expect(result.function).toBe('history_mining_worker');
    expect(result.job_id).toBe('job-1');
    expect(mockedPost).toHaveBeenCalledWith('/api/v1/admin/kb-drafts/run-mining');
  });

  it('parses worker_started=false (already running worker)', async () => {
    mockedPost.mockResolvedValueOnce({
      data: {
        enqueued: true,
        worker_started: false,
        function: 'history_mining_worker',
        job_id: 'job-7',
      },
    });
    const result = await triggerHistoryMining();
    expect(result.worker_started).toBe(false);
    expect(result.job_id).toBe('job-7');
  });

  it('rejects malformed payload (wrong function name)', async () => {
    mockedPost.mockResolvedValueOnce({
      data: {
        enqueued: true,
        worker_started: true,
        function: 'something_else',
        job_id: 'job-1',
      },
    });
    await expect(triggerHistoryMining()).rejects.toThrow();
  });
});