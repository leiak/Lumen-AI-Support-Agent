import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// ---------------------------------------------------------------------------
// Budget Dashboard admin SPA — tenant budget config + usage + credit grants.
//
// Backend shape verified against
//   apps/api/src/admin/schemas/budget.py
// (M4.D Task 4 + M4.D Pack B #2/#5) and the route handlers in
//   apps/api/src/admin/api.py
//
// Three endpoints:
//   GET /api/v1/admin/tenants/{tenant_id}/budget
//   GET /api/v1/admin/tenants/{tenant_id}/budget/usage?breakdown=true
//   GET /api/v1/admin/tenants/{tenant_id}/credits?period=YYYY-MM
//
// The credits endpoint is super-admin only — per-tenant admins (the
// audience of this SPA when logged in via the demo JWT) get a 404. The
// page degrades to an empty credits list in that case instead of
// throwing — see ``AdminBudgetPage`` for the rendering branch.
//
// IMPORTANT: the credits endpoint wraps the array as
// ``{ credits: [...], total_tokens: N }`` (NOT a bare list) and
// requires the ``?period=YYYY-MM`` query param. The per-row shape
// uses ``tokens`` + ``note`` (not ``amount_tokens`` / ``reason`` /
// ``expires_at``).
// ---------------------------------------------------------------------------

export const TenantBudgetSchema = z.object({
  soft_warn_tokens: z.number().int().nullable(),
  hard_cap_tokens: z.number().int().nullable(),
  period_anchor_tz: z.string(),
  updated_at: z.string(),
});
export type TenantBudget = z.infer<typeof TenantBudgetSchema>;

export const BudgetUsageBreakdownItemSchema = z.object({
  provider: z.string(),
  model: z.string(),
  prompt_tokens: z.number().int(),
  completion_tokens: z.number().int(),
  total_tokens: z.number().int(),
  request_count: z.number().int(),
});
export type BudgetUsageBreakdownItem = z.infer<typeof BudgetUsageBreakdownItemSchema>;

export const BudgetUsageSchema = z.object({
  period: z.string(),
  tokens_used: z.number().int(),
  soft_warn_tokens: z.number().int().nullable(),
  hard_cap_tokens: z.number().int().nullable(),
  period_starts_at: z.string(),
  effective_cap: z.number().int(),
  credits_total: z.number().int(),
  // Populated only when the request carries `?breakdown=true`; null
  // otherwise. The page hides the breakdown card when this is null.
  breakdown: z.array(BudgetUsageBreakdownItemSchema).nullable(),
});
export type BudgetUsage = z.infer<typeof BudgetUsageSchema>;

export const BudgetCreditSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  period: z.string(),
  tokens: z.number().int(),
  note: z.string(),
  granted_by: z.string(),
  created_at: z.string(),
});
export type BudgetCredit = z.infer<typeof BudgetCreditSchema>;

export const BudgetCreditListSchema = z.object({
  credits: z.array(BudgetCreditSchema),
  total_tokens: z.number().int(),
});
export type BudgetCreditList = z.infer<typeof BudgetCreditListSchema>;

// --- Mutation inputs ------------------------------------------------------
//
// Tier 1 Task 1.4: the budget page is no longer read-only. Two mutations
// are exposed — `updateBudget` (per-tenant admin) and `grantCredit`
// (super-admin only; the backend returns 404 for everyone else via the
// anti-enumeration gate). Both are POST upserts; the API does not
// currently support PATCH for these rows.

export const BudgetUpdateInputSchema = z.object({
  soft_warn_tokens: z.number().int().positive(),
  hard_cap_tokens: z.number().int().positive(),
  period_anchor_tz: z.string().min(1).max(64),
});
export type BudgetUpdateInput = z.infer<typeof BudgetUpdateInputSchema>;

export const CreditGrantInputSchema = z.object({
  tokens: z.number().int().positive(),
  note: z.string().min(1).max(500),
  period: z.string().regex(/^\d{4}-\d{2}$/),
});
export type CreditGrantInput = z.infer<typeof CreditGrantInputSchema>;

/**
 * GET /api/v1/admin/tenants/{tenant_id}/budget
 *
 * Returns the per-tenant budget *config* (soft_warn, hard_cap, tz
 * anchor). 404 if not yet configured — the page treats that as a
 * loading-state rather than an error.
 */
export async function fetchBudget(tenantId: string): Promise<TenantBudget> {
  const { data } = await apiClient.get(
    `/api/v1/admin/tenants/${tenantId}/budget`,
  );
  return TenantBudgetSchema.parse(data);
}

/**
 * POST /api/v1/admin/tenants/{tenant_id}/budget — upsert config.
 *
 * Tier 1 Task 1.4: a per-tenant admin can now edit the soft_warn and
 * hard_cap thresholds from the SPA. Backend enforces ``hard_cap >=
 * soft_warn`` and returns 422 if violated.
 */
export async function updateBudget(
  tenantId: string,
  input: BudgetUpdateInput,
): Promise<TenantBudget> {
  const { data } = await apiClient.post(
    `/api/v1/admin/tenants/${tenantId}/budget`,
    input,
  );
  return TenantBudgetSchema.parse(data);
}

/**
 * POST /api/v1/admin/tenants/{tenant_id}/credits — grant tokens.
 *
 * Super-admin only. Per-tenant admins get a 404 (anti-enumeration);
 * the dialog should be hidden for them via the `is_super_admin` gate
 * upstream (see `AdminBudgetPage`). Response is a single ``BudgetCredit``
 * (the just-inserted row).
 */
export async function grantCredit(
  tenantId: string,
  input: CreditGrantInput,
): Promise<BudgetCredit> {
  const { data } = await apiClient.post(
    `/api/v1/admin/tenants/${tenantId}/credits`,
    input,
  );
  return BudgetCreditSchema.parse(data);
}

/**
 * GET /api/v1/admin/tenants/{tenant_id}/budget/usage?breakdown=true
 *
 * Returns the live usage snapshot for the current period. Always
 * includes ``effective_cap`` + ``credits_total`` (Pack B #2); the
 * optional ``breakdown`` array is only present when ``?breakdown=true``
 * is passed (Pack B #5).
 */
export async function fetchBudgetUsage(tenantId: string): Promise<BudgetUsage> {
  const { data } = await apiClient.get(
    `/api/v1/admin/tenants/${tenantId}/budget/usage`,
    { params: { breakdown: true } },
  );
  return BudgetUsageSchema.parse(data);
}

/**
 * GET /api/v1/admin/tenants/{tenant_id}/credits?period=YYYY-MM
 *
 * Returns the chronological list of credit grants + the period
 * total. Super-admin only — per-tenant admins get a 404 and the
 * page degrades to an empty credits card.
 */
export async function fetchBudgetCredits(
  tenantId: string,
  period: string,
): Promise<BudgetCreditList> {
  const { data } = await apiClient.get(
    `/api/v1/admin/tenants/${tenantId}/credits`,
    { params: { period } },
  );
  return BudgetCreditListSchema.parse(data);
}

/**
 * Format an integer token count with a thin-space thousands separator
 * (zh-CN locale). Negative-cap input renders as ``"-"`` so the UI
 * degrades gracefully when the backend has not configured a tenant.
 */
export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return '-';
  return value.toLocaleString('zh-CN');
}

/**
 * Color the "已用" stat + the usage-rate bar against the configured
 * soft_warn / hard_cap thresholds.
 *
 * * red when tokens_used >= hard_cap_tokens (gated)
 * * yellow when tokens_used >= soft_warn_tokens (warning)
 * * green otherwise
 *
 * Falls back to a neutral ``text-emerald-600``/``text-yellow-600``/
 * ``text-destructive`` mapping when either threshold is null.
 */
export type ThresholdBand = 'ok' | 'warn' | 'over';

export function classifyUsageAgainstThresholds(
  tokensUsed: number,
  softWarn: number | null,
  hardCap: number | null,
): ThresholdBand {
  if (hardCap !== null && tokensUsed >= hardCap) return 'over';
  if (softWarn !== null && tokensUsed >= softWarn) return 'warn';
  return 'ok';
}

export const BAND_TEXT_CLASS: Record<ThresholdBand, string> = {
  ok: 'text-emerald-600',
  warn: 'text-yellow-600',
  over: 'text-destructive',
};

export const BAND_BAR_CLASS: Record<ThresholdBand, string> = {
  ok: 'bg-emerald-500',
  warn: 'bg-yellow-500',
  over: 'bg-destructive',
};
