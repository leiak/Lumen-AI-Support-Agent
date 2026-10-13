import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// Tech debt #20 — admin SPA for KB draft review (M3).
//
// Backend shape verified against `apps/api/src/admin/api.py`
// (Stage 18 / M2.B Task 8) and `apps/api/src/knowledge/models.py`
// (KbArticleDraft model). After tech debt #17 the endpoints require
// JWT auth; the axios interceptor in `api-client.ts` attaches the
// bearer token automatically — no manual header passing.
//
// PII discipline: the listing endpoint returns a 200-char body_preview
// only. The full body is fetched on demand by `fetchDraft`. The
// `source_questions` field is opaque ULIDs and is NOT exposed here.

export const DraftStatusSchema = z.enum(['DRAFT', 'APPROVED', 'REJECTED']);
export type DraftStatus = z.infer<typeof DraftStatusSchema>;

export const KbDraftSchema = z.object({
  id: z.string(),
  title: z.string(),
  body_preview: z.string(),
  tags: z.array(z.string()),
  source_question_count: z.number().int().nonnegative(),
  cluster_id: z.number().int().nullable(),
  status: DraftStatusSchema,
  created_at: z.string(),
});
export type KbDraft = z.infer<typeof KbDraftSchema>;

export const KbDraftDetailSchema = KbDraftSchema.extend({
  body: z.string(),
  reviewed_at: z.string().nullable(),
  reviewed_by: z.string().nullable(),
});
export type KbDraftDetail = z.infer<typeof KbDraftDetailSchema>;

export const KbDraftListSchema = z.object({
  drafts: z.array(KbDraftSchema),
});
export type KbDraftList = z.infer<typeof KbDraftListSchema>;

export interface ListDraftsParams {
  status?: DraftStatus;
  limit?: number;
}

/**
 * GET /api/v1/admin/kb-drafts — list drafts for review.
 * Defaults to `status=DRAFT` so the admin sees the reviewable queue first.
 */
export async function fetchDrafts(
  params: ListDraftsParams = {},
): Promise<KbDraftList> {
  const { data } = await apiClient.get('/api/v1/admin/kb-drafts', {
    params: {
      status: params.status ?? 'DRAFT',
      limit: params.limit ?? 50,
    },
  });
  return KbDraftListSchema.parse(data);
}

/**
 * GET /api/v1/admin/kb-drafts/{id} — fetch a single draft with the full body.
 * Cross-tenant access returns 404 (anti-enumeration, enforced server-side).
 */
export async function fetchDraft(draftId: string): Promise<KbDraftDetail> {
  const { data } = await apiClient.get(`/api/v1/admin/kb-drafts/${draftId}`);
  return KbDraftDetailSchema.parse(data);
}

export interface ApproveResult {
  draft_id: string;
  article_id: string;
  status: 'APPROVED';
}

/**
 * POST /api/v1/admin/kb-drafts/{id}/approve — approve a draft.
 * Creates a live Article in the tenant's auto-mined KB. 200 on success,
 * 404 if not found / cross-tenant, 409 if already approved/rejected.
 */
export async function approveDraft(draftId: string): Promise<ApproveResult> {
  const { data } = await apiClient.post(
    `/api/v1/admin/kb-drafts/${draftId}/approve`,
  );
  return z.object({
    draft_id: z.string(),
    article_id: z.string(),
    status: z.literal('APPROVED'),
  }).parse(data);
}

export interface RejectResult {
  draft_id: string;
  status: 'REJECTED';
}

/**
 * POST /api/v1/admin/kb-drafts/{id}/reject — reject a draft.
 * No Article is created. Same status codes as approve.
 */
export async function rejectDraft(draftId: string): Promise<RejectResult> {
  const { data } = await apiClient.post(
    `/api/v1/admin/kb-drafts/${draftId}/reject`,
  );
  return z.object({
    draft_id: z.string(),
    status: z.literal('REJECTED'),
  }).parse(data);
}

// ---------------------------------------------------------------------------
// "立即执行" — manual history mining trigger (Tier 1)
//
// The history-mining worker normally runs as a weekly cron (Sunday
// 03:00 UTC). Operators want a "run now" button on /admin/kb-drafts
// for the common case where there are zero DRAFTs because the cron
// hasn't fired yet. The backend endpoint
// ``POST /api/v1/admin/kb-drafts/run-mining`` enqueues the same
// ``history_mining_worker`` job and spawns an arq worker subprocess
// if one isn't already consuming the queue. 503 means Redis is down
// — the SPA surfaces that as "队列暂不可达,请稍后重试".
// ---------------------------------------------------------------------------

export const RunMiningResultSchema = z.object({
  enqueued: z.boolean(),
  worker_started: z.boolean(),
  function: z.literal('history_mining_worker'),
  job_id: z.string().nullable(),
});
export type RunMiningResult = z.infer<typeof RunMiningResultSchema>;

/**
 * POST /api/v1/admin/kb-drafts/run-mining — trigger history mining on demand.
 *
 * @returns the enqueue outcome. ``worker_started=true`` means the
 *   endpoint had to spawn a new arq worker subprocess because none
 *   was alive; ``worker_started=false`` means a worker was already
 *   running and will pick up the job on its next poll.
 *
 * @throws on 503 — the user-facing copy should be "队列暂不可达,请稍后重试"
 *   so the operator doesn't think the click did nothing.
 */
export async function triggerHistoryMining(): Promise<RunMiningResult> {
  const { data } = await apiClient.post('/api/v1/admin/kb-drafts/run-mining');
  return RunMiningResultSchema.parse(data);
}
