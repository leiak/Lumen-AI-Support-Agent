import { Copy } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { TENANT_PLAN_LABEL, type TenantOut } from '@/lib/settings';

export interface TenantInfoCardProps {
  /** Tenant info to render. When `null` we show the skeleton state. */
  tenant: TenantOut | null;
  /** Optional override for the displayed name (e.g. from useCurrentUser). */
  displayName?: string | null;
}

function truncateUlid(value: string, chars = 8): string {
  if (value.length <= chars) return value;
  return `${value.slice(0, chars)}…`;
}

function formatDate(iso: string): string {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return iso;
  return then.toISOString().slice(0, 10);
}

/**
 * Read-only summary card for the caller's tenant. Shows the tenant ID (with
 * copy button), name, creation date, and plan. Editing is M2+ — this card
 * is intentionally action-free.
 *
 * For M1 the backend does not yet expose `GET /api/v1/tenants/me`, so the
 * parent page passes `tenant={null}` and we render a skeleton. The fallback
 * `displayName` prop lets the page inject the tenant name from
 * `useCurrentUser()` so the user still sees something useful.
 */
export function TenantInfoCard({
  tenant,
  displayName,
}: TenantInfoCardProps): JSX.Element {
  if (tenant === null) {
    return (
      <Card data-testid="tenant-info-card-loading">
        <CardHeader>
          <CardTitle className="text-lg">租户信息</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3" data-testid="tenant-info-skeleton">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-4 w-1/3" />
        </CardContent>
      </Card>
    );
  }

  const planLabel = TENANT_PLAN_LABEL[tenant.plan];
  const resolvedName = displayName ?? tenant.name;

  const handleCopy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(tenant.id);
    } catch {
      // Best-effort: clipboard write may be blocked (e.g. insecure context
      // or missing permission). We silently swallow — the full id is still
      // visible in the copy button's tooltip on hover.
    }
  };

  return (
    <Card data-testid="tenant-info-card">
      <CardHeader>
        <CardTitle className="text-lg">租户信息</CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-1 gap-3 sm:grid-cols-[160px_1fr]">
        <span className="text-sm text-muted-foreground">租户 ID</span>
        <div className="flex items-center gap-2">
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  className="font-mono text-xs text-foreground"
                  data-testid="tenant-id"
                  title={tenant.id}
                >
                  {truncateUlid(tenant.id, 8)}
                </span>
              </TooltipTrigger>
              <TooltipContent>{tenant.id}</TooltipContent>
            </Tooltip>
          </TooltipProvider>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={handleCopy}
            aria-label="复制租户 ID"
            title="复制租户 ID"
            data-testid="tenant-id-copy"
          >
            <Copy />
          </Button>
        </div>

        <span className="text-sm text-muted-foreground">租户名称</span>
        <span
          className="text-sm text-foreground"
          data-testid="tenant-name"
          title={resolvedName}
        >
          {resolvedName}
        </span>

        <span className="text-sm text-muted-foreground">创建时间</span>
        <span
          className="text-sm text-foreground"
          data-testid="tenant-created-at"
          title={tenant.created_at}
        >
          {formatDate(tenant.created_at)}
        </span>

        <span className="text-sm text-muted-foreground">套餐</span>
        <span data-testid="tenant-plan">
          <Badge variant="secondary" data-plan={tenant.plan}>
            {planLabel}
          </Badge>
        </span>
      </CardContent>
    </Card>
  );
}