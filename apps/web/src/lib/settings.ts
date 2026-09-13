import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// ---------------------------------------------------------------------------
// Channel — verified against apps/api/src/channel/api.py (`ChannelOut`):
//
//   class ChannelOut(BaseModel):
//     id: str
//     type: ChannelType          # feishu | web | email
//     name: str
//     status: ChannelStatus      # active | disabled
//     tenant_id: str
//     created_at: datetime
//
// Soft-delete contract: the channel row is never hard-deleted. The DELETE
// endpoint flips `status` to "disabled"; see `soft_delete` in
// `apps/api/src/channel/service.py`. There is no `deleted_at` column on the
// Channel model — the `status === "disabled"` branch IS the soft-delete
// indicator for M1.
// ---------------------------------------------------------------------------

export const ChannelTypeSchema = z.enum(['feishu', 'web', 'email']);
export type ChannelType = z.infer<typeof ChannelTypeSchema>;

export const ChannelStatusSchema = z.enum(['active', 'disabled']);
export type ChannelStatus = z.infer<typeof ChannelStatusSchema>;

export const ChannelSchema = z.object({
  id: z.string(),
  type: ChannelTypeSchema,
  name: z.string(),
  status: ChannelStatusSchema,
  tenant_id: z.string(),
  created_at: z.string(),
});
export type Channel = z.infer<typeof ChannelSchema>;

/** Display label per channel type. Used by the channel-row badge. */
export const CHANNEL_TYPE_LABEL: Record<ChannelType, string> = {
  feishu: '飞书',
  web: 'Web Widget',
  email: '邮件',
};

/** Display label per channel status. */
export const CHANNEL_STATUS_LABEL: Record<ChannelStatus, string> = {
  active: '启用',
  disabled: '已禁用',
};

// ---------------------------------------------------------------------------
// Tenant — forward-compatible schema for `GET /api/v1/tenants/me`.
//
// The M1 backend does NOT yet expose this endpoint. The settings page
// derives `tenant_id` + `tenant_name` from `useCurrentUser()` and treats
// `plan` / `status` / `created_at` as M2 hooks. The schema here mirrors the
// ORM (`apps/api/src/tenant/models.py`) and the enums in
// `apps/api/src/tenant/enums.py` so when the endpoint ships the frontend
// only has to wire `getCurrentTenant()` into the page.
// ---------------------------------------------------------------------------

export const TenantPlanSchema = z.enum(['free', 'pro', 'enterprise']);
export type TenantPlan = z.infer<typeof TenantPlanSchema>;

export const TenantStatusSchema = z.enum(['active', 'suspended', 'deleted']);
export type TenantStatus = z.infer<typeof TenantStatusSchema>;

export const TenantOutSchema = z.object({
  id: z.string(),
  name: z.string(),
  plan: TenantPlanSchema,
  status: TenantStatusSchema,
  created_at: z.string(),
});
export type TenantOut = z.infer<typeof TenantOutSchema>;

/** Display label per tenant plan. */
export const TENANT_PLAN_LABEL: Record<TenantPlan, string> = {
  free: '基础版',
  pro: '专业版',
  enterprise: '企业版',
};

/**
 * GET /api/v1/tenants/me — fetch the caller's tenant.
 *
 * NOTE: M1 does not yet implement this endpoint. The settings page derives
 * the minimum surface (`id`, `name`) from `useCurrentUser()` so the card
 * renders even when the call fails. The schema + function are kept here so
 * the M2 swap is a one-line change in `pages/settings.tsx`.
 */
export async function fetchCurrentTenant(): Promise<TenantOut> {
  const { data } = await apiClient.get('/api/v1/tenants/me');
  return TenantOutSchema.parse(data);
}

/**
 * GET /api/v1/channels — list channels for the caller's tenant.
 *
 * Admin-only (the endpoint is gated by `require_admin` server-side). The
 * settings page is mounted on the workspace shell which already enforces
 * admin auth at the route level.
 */
export async function listChannels(): Promise<Channel[]> {
  const { data } = await apiClient.get('/api/v1/channels');
  return z.array(ChannelSchema).parse(data);
}

/**
 * GET /api/v1/channels/{id} — fetch one channel. Not currently called by
 * the M1 settings page (the row is read-only), but exported so the M2
 * edit dialog can reuse this client without a second zod schema.
 */
export async function getChannel(channelId: string): Promise<Channel> {
  const { data } = await apiClient.get(`/api/v1/channels/${channelId}`);
  return ChannelSchema.parse(data);
}