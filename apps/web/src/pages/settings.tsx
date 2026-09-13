import { AxiosError } from 'axios';
import { RefreshCw } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';

import { BrandingPlaceholder } from '@/components/settings/branding-placeholder';
import { ChannelListCard } from '@/components/settings/channel-list-card';
import { TenantInfoCard } from '@/components/settings/tenant-info-card';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { useCurrentUser } from '@/lib/use-current-user';
import {
  listChannels,
  type TenantOut,
} from '@/lib/settings';

interface FetchErrorPayload {
  detail?: string | Array<{ msg?: string }>;
}

/** Convert an unknown thrown value into a UI-safe Chinese error string. */
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
 * Settings page wiring. M1 is intentionally read-only: it shows the
 * caller's tenant identity, the channels registered for this tenant, and
 * a placeholder for branding controls (which become editable in M2).
 *
 * Tenant shape: the M1 backend doesn't yet expose `GET /api/v1/tenants/me`.
 * We derive the tenant id + name from `useCurrentUser()` (which reads
 * `GET /agents/me`) and only attempt the dedicated tenant call once that
 * endpoint ships — see `lib/settings.ts` for the forward-compatible
 * `TenantOut` schema.
 *
 * Channels: fetched via `GET /api/v1/channels`. Admin-only at the route
 * level; the page mounts behind the workspace shell's admin gate.
 */
export function SettingsPage(): JSX.Element {
  const { user } = useCurrentUser();

  const channelsQuery = useQuery({
    queryKey: [...SETTINGS_QUERY_KEY, 'channels'],
    queryFn: listChannels,
    staleTime: 30_000,
  });

  const handleRetry = (): void => {
    void channelsQuery.refetch();
  };

  // Compose a synthetic TenantOut from the JWT-derived identity so the
  // tenant-info card renders a coherent surface even before the
  // dedicated endpoint ships. `created_at` / `plan` are unknown so we
  // surface a neutral fallback that the card can render without breaking.
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
        <ChannelListCard channels={channelsQuery.data ?? []} />
      )}

      <BrandingPlaceholder />
    </div>
  );
}