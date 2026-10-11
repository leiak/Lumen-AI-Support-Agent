import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

/**
 * Admin Dashboard aggregate metrics — Tier 2 Task 2.5.
 *
 * Single endpoint:
 *   GET /api/v1/admin/tenants/{tenant_id}/dashboard
 *
 * Returns aggregate counts the SPA renders as KPI tiles + small
 * status/priority breakdowns. No PII in the payload — only opaque
 * numbers scoped to the caller's tenant.
 */

export const StatusCountSchema = z.object({
  key: z.string(),
  count: z.number().int().nonnegative(),
});

export type StatusCount = z.infer<typeof StatusCountSchema>;

export const DashboardReadSchema = z.object({
  today_utc_start: z.string(),
  tickets_today: z.number().int().nonnegative(),
  tickets_by_status: z.array(StatusCountSchema),
  tickets_by_priority: z.array(StatusCountSchema),
  conversations_open: z.number().int().nonnegative(),
  period: z.string(),  // YYYY-MM
  period_starts_at: z.string(),
  llm_tokens_this_period: z.number().int().nonnegative(),
  active_users: z.number().int().nonnegative(),
  total_users: z.number().int().nonnegative(),
});

export type DashboardRead = z.infer<typeof DashboardReadSchema>;

/**
 * Fetch the dashboard aggregate payload. Throws on auth / network /
 * schema errors; the page handles those via react-query state.
 */
export async function fetchDashboard(tenantId: string): Promise<DashboardRead> {
  const { data } = await apiClient.get(
    `/api/v1/admin/tenants/${tenantId}/dashboard`,
  );
  return DashboardReadSchema.parse(data);
}

/** Format an int token count with thousand separators (no unit suffix). */
export function formatTokenCount(value: number): string {
  return new Intl.NumberFormat('zh-CN').format(value);
}

/** Pick the largest bucket from a count breakdown (or null if empty). */
export function topBucket(
  rows: readonly StatusCount[],
): StatusCount | null {
  if (rows.length === 0) return null;
  return rows.reduce((acc, cur) => (cur.count > acc.count ? cur : acc));
}