import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// Tier 2 Task 2.4 — Widget appearance config admin page.
//
// Backend (apps/api/src/admin/api.py):
//   GET    /api/v1/admin/tenants/{tid}/widget-config
//   PATCH  /api/v1/admin/tenants/{tid}/widget-config
//   DELETE /api/v1/admin/tenants/{tid}/widget-config
//
// Defaults match the M2.A baseline so a freshly-seeded tenant
// looks identical before any admin edits.

export const WIDGET_THEMES = ['light', 'dark', 'auto'] as const;
export const WIDGET_POSITIONS = ['bottom-right', 'bottom-left'] as const;

export type WidgetTheme = (typeof WIDGET_THEMES)[number];
export type WidgetPosition = (typeof WIDGET_POSITIONS)[number];

export const DEFAULT_PRIMARY_COLOR = '#2563eb';
export const DEFAULT_WIDGET_TITLE = '在线客服';
export const DEFAULT_WELCOME_MESSAGE =
  '您好!请描述您的问题,我们会尽快为您解答。';

const HEX_COLOR = /^#[0-9A-Fa-f]{6}$/;

export const WidgetConfigSchema = z.object({
  tenant_id: z.string(),
  theme: z.enum(WIDGET_THEMES),
  primary_color: z.string().regex(HEX_COLOR),
  position: z.enum(WIDGET_POSITIONS),
  widget_title: z.string().min(1).max(64),
  welcome_message: z.string().min(1).max(500),
  logo_url: z.string().nullable(),
  enabled: z.boolean(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type WidgetConfig = z.infer<typeof WidgetConfigSchema>;

export const WidgetConfigUpdateInputSchema = z.object({
  theme: z.enum(WIDGET_THEMES).optional(),
  primary_color: z.string().regex(HEX_COLOR).optional(),
  position: z.enum(WIDGET_POSITIONS).optional(),
  widget_title: z.string().min(1).max(64).optional(),
  welcome_message: z.string().min(1).max(500).optional(),
  logo_url: z.string().max(500).nullable().optional(),
  enabled: z.boolean().optional(),
});
export type WidgetConfigUpdateInput = z.infer<
  typeof WidgetConfigUpdateInputSchema
>;

/** GET /api/v1/admin/tenants/{tid}/widget-config */
export async function fetchWidgetConfig(
  tenantId: string,
): Promise<WidgetConfig> {
  const { data } = await apiClient.get(
    `/api/v1/admin/tenants/${tenantId}/widget-config`,
  );
  return WidgetConfigSchema.parse(data);
}

/** PATCH /api/v1/admin/tenants/{tid}/widget-config — upsert. */
export async function patchWidgetConfig(
  tenantId: string,
  input: WidgetConfigUpdateInput,
): Promise<WidgetConfig> {
  const { data } = await apiClient.patch(
    `/api/v1/admin/tenants/${tenantId}/widget-config`,
    input,
  );
  return WidgetConfigSchema.parse(data);
}

/** DELETE /api/v1/admin/tenants/{tid}/widget-config — idempotent. */
export async function resetWidgetConfig(tenantId: string): Promise<void> {
  await apiClient.delete(
    `/api/v1/admin/tenants/${tenantId}/widget-config`,
  );
}