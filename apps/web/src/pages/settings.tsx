import { useState } from 'react';
import { AxiosError } from 'axios';
import { RefreshCw } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { BrandingPlaceholder } from '@/components/settings/branding-placeholder';
import { ChannelCreateDialog } from '@/components/settings/channel-create-dialog';
import { ChannelEditDialog } from '@/components/settings/channel-edit-dialog';
import { ChannelListCard } from '@/components/settings/channel-list-card';
import { TenantInfoCard } from '@/components/settings/tenant-info-card';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { deleteChannel } from '@/lib/channels';
import { useCurrentUser } from '@/lib/use-current-user';
import {
  listChannels,
  type Channel,
  type TenantOut,
} from '@/lib/settings';

interface FetchErrorPayload {
  detail?: string | Array<{ msg?: string }>;
}

function extractErrorMessage(error: unknown): string {
  if (error instanceof AxiosError) {
    const payload = error.response?.data as FetchErrorPayload | undefined;
    const detail = payload?.detail;
    if (typeof detail === 'string' && detail.trim().length > 0) return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0];
      if (first && typeof first === 'object' && typeof first.msg === 'string') {
        return first.msg;
      }
    }
    return error.message || '加载租户设置失败,请稍后再试。';
  }
  if (error instanceof Error && error.message) return error.message;
  return '加载租户设置失败,请稍后再试。';
}

const SETTINGS_QUERY_KEY = ['settings'] as const;

/**
 * Settings page — tenant identity, channels (full CRUD), branding placeholder.
 *
 * Channels: the unified `/api/v1/channels` API supports POST/GET/PATCH/DELETE.
 * We use the read-only `listChannels` for the initial fetch, then layer
 * mutations via `useMutation` + `qc.invalidateQueries(['settings','channels'])`
 * so the card refetches after each successful create/edit/delete.
 *
 * Tier 1 Task 1.2 (2026-10-10): the channel card was previously read-only;
 * this page now wires create + edit dialogs and a window.confirm() delete.
 */
export function SettingsPage(): JSX.Element {
  const { user } = useCurrentUser();
  const qc = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<Channel | null>(null);

  const channelsQuery = useQuery({
    queryKey: [...SETTINGS_QUERY_KEY, 'channels'],
    queryFn: listChannels,
    staleTime: 30_000,
  });

  const deleteMut = useMutation({
    mutationFn: (channelId: string) => deleteChannel(channelId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...SETTINGS_QUERY_KEY, 'channels'] });
    },
  });

  const handleRetry = (): void => {
    void channelsQuery.refetch();
  };

  const handleDelete = (channel: Channel): void => {
    if (!window.confirm(`禁用渠道 “${channel.name}”? 该渠道将停止接收新消息,但可后续在编辑中重新启用。`)) {
      return;
    }
    deleteMut.mutate(channel.id);
  };

  const tenant: TenantOut | null =
    user === null
      ? null
      : {
          id: user.tenant_id,
          name: user.tenant_name,
          plan: 'free',
          status: 'active',
          created_at: new Date(0).toISOString(),
        };

  return (
    <div className="flex h-full w-full flex-col gap-4 overflow-auto p-4">
      <header className="flex items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight">租户设置</h1>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={handleRetry}
          disabled={channelsQuery.isFetching}
          data-testid="settings-refresh"
        >
          <RefreshCw />
          刷新
        </Button>
      </header>

      <TenantInfoCard
        tenant={tenant}
        displayName={user?.tenant_name ?? null}
      />

      {channelsQuery.isError ? (
        <Card data-testid="settings-error">
          <CardContent className="flex flex-col items-center justify-center gap-3 p-6 text-center">
            <p className="text-sm text-destructive" role="alert">
              {extractErrorMessage(channelsQuery.error)}
            </p>
            <Button type="button" size="sm" onClick={handleRetry}>
              重试
            </Button>
          </CardContent>
        </Card>
      ) : channelsQuery.isLoading ? (
        <ChannelListCard
          channels={[]}
          isLoading
        />
      ) : (
        <ChannelListCard
          channels={channelsQuery.data ?? []}
          onCreate={() => setCreateOpen(true)}
          onEdit={setEditing}
          onDelete={handleDelete}
        />
      )}

      <ChannelCreateDialog open={createOpen} onOpenChange={setCreateOpen} />
      <ChannelEditDialog
        channel={editing}
        onOpenChange={(o) => {
          if (!o) setEditing(null);
        }}
      />

      <BrandingPlaceholder />
    </div>
  );
}
