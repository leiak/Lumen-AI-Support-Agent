import { z } from 'zod';

import { apiClient } from '@/lib/api-client';
import { ChannelSchema, ChannelTypeSchema, type Channel } from '@/lib/settings';

export const ChannelCreateInputSchema = z.object({
  type: ChannelTypeSchema,
  name: z.string().min(1).max(200),
  credentials: z.record(z.string(), z.unknown()),
});
export type ChannelCreateInput = z.infer<typeof ChannelCreateInputSchema>;

export const ChannelUpdateInputSchema = z.object({
  name: z.string().min(1).max(200).optional(),
  status: z.enum(['active', 'disabled']).optional(),
  credentials: z.record(z.string(), z.unknown()).optional(),
});
export type ChannelUpdateInput = z.infer<typeof ChannelUpdateInputSchema>;

/** POST /api/v1/channels — create a new channel for the caller's tenant. */
export async function createChannel(input: ChannelCreateInput): Promise<Channel> {
  const { data } = await apiClient.post('/api/v1/channels', input);
  return ChannelSchema.parse(data);
}

/** PATCH /api/v1/channels/{id} — partial update. */
export async function updateChannel(
  channelId: string,
  patch: ChannelUpdateInput,
): Promise<Channel> {
  const { data } = await apiClient.patch(`/api/v1/channels/${channelId}`, patch);
  return ChannelSchema.parse(data);
}

/** DELETE /api/v1/channels/{id} — soft-delete (status=disabled server-side). */
export async function deleteChannel(channelId: string): Promise<void> {
  await apiClient.delete(`/api/v1/channels/${channelId}`);
}
