import { Clock, Sun } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';

import { Badge } from '@/components/ui/badge';
import { fetchSlaPolicies, type SlaPolicy, type SlaPriority } from '@/lib/sla-policies';
import { useCurrentUser } from '@/lib/use-current-user';
import { cn } from '@/lib/utils';

// SLA Policies admin page — read-only listing of per-priority SLA
// targets. The ticket service stamps ``tickets.sla_deadline_at`` by
// looking up the policy matching the ticket's priority at create time,
// so the page is the only place admins can audit which buckets are
// configured and how aggressive each is.
//
// Pairs with the new endpoint at
// GET /api/v1/admin/tenants/{tenant_id}/sla-policies.

const SLA_POLICIES_QUERY_KEY = ['admin', 'sla-policies'] as const;

/** Map each priority to a Tailwind hue pair used by the badge. */
const PRIORITY_STYLES: Record<SlaPriority, string> = {
  P0: 'bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200',
  P1: 'bg-orange-100 text-orange-900 dark:bg-orange-900/40 dark:text-orange-200',
  P2: 'bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200',
  P3: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700/60 dark:text-zinc-200',
};

function PriorityBadge({ priority }: { priority: SlaPriority }): JSX.Element {
  return (
    <Badge
      variant="outline"
      className={cn(
        'border-transparent font-semibold',
        PRIORITY_STYLES[priority],
      )}
      data-testid={`sla-priority-badge-${priority}`}
    >
      {priority}
    </Badge>
  );
}

function PolicyRow({ policy }: { policy: SlaPolicy }): JSX.Element {
  return (
    <tr
      className="border-b transition-colors hover:bg-muted/40"
      data-testid={`sla-policy-row-${policy.priority}`}
    >
      <td className="px-4 py-3 align-middle">
        <PriorityBadge priority={policy.priority} />
      </td>
      <td className="px-4 py-3 align-middle">
        <span
          className="text-sm font-medium"
          data-testid={`sla-policy-name-${policy.priority}`}
        >
          {policy.name}
        </span>
      </td>
      <td className="px-4 py-3 align-middle">
        <span
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground"
          data-testid={`sla-policy-response-${policy.priority}`}
        >
          <Clock className="h-3.5 w-3.5" aria-hidden="true" />
          首响 {policy.first_response_minutes} 分钟
        </span>
      </td>
      <td className="px-4 py-3 align-middle">
        <span
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground"
          data-testid={`sla-policy-resolution-${policy.priority}`}
        >
          <Clock className="h-3.5 w-3.5" aria-hidden="true" />
          解决 {policy.resolution_minutes} 分钟
        </span>
      </td>
      <td className="px-4 py-3 align-middle">
        {policy.business_hours_only ? (
          <span
            className="inline-flex items-center gap-1.5 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900 dark:bg-amber-900/40 dark:text-amber-200"
            data-testid={`sla-policy-hours-${policy.priority}`}
          >
            <Sun className="h-3 w-3" aria-hidden="true" />
            仅工作时间
          </span>
        ) : (
          <span
            className="inline-flex items-center gap-1.5 rounded-full bg-zinc-100 px-2 py-0.5 text-xs font-medium text-zinc-700 dark:bg-zinc-700/40 dark:text-zinc-200"
            data-testid={`sla-policy-hours-${policy.priority}`}
          >
            全天
          </span>
        )}
      </td>
    </tr>
  );
}

export function AdminSlaPoliciesPage(): JSX.Element {
  const { user } = useCurrentUser();
  const tenantId = user?.tenant_id ?? '';

  const policiesQuery = useQuery({
    queryKey: [...SLA_POLICIES_QUERY_KEY, tenantId],
    queryFn: () => fetchSlaPolicies(tenantId),
    enabled: Boolean(tenantId),
  });

  return (
    <div
      className="flex h-full flex-col gap-6 p-6"
      data-testid="admin-sla-policies-page"
    >
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">SLA 策略</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          每个优先级一条策略,工单创建时按 priority 匹配。
        </p>
      </div>

      {policiesQuery.isLoading ? (
        <p
          className="text-sm text-muted-foreground"
          data-testid="sla-policies-loading"
        >
          加载中…
        </p>
      ) : policiesQuery.isError ? (
        <p
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          role="alert"
          data-testid="sla-policies-error"
        >
          加载失败:{' '}
          {policiesQuery.error instanceof Error
            ? policiesQuery.error.message
            : '未知错误'}
        </p>
      ) : (policiesQuery.data?.length ?? 0) === 0 ? (
        <p
          className="text-sm text-muted-foreground"
          data-testid="sla-policies-empty"
        >
          暂无 SLA 策略。
        </p>
      ) : (
        <div
          className="overflow-hidden rounded-md border bg-card"
          data-testid="sla-policies-table-wrapper"
        >
          <table className="w-full text-left text-sm">
            <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">优先级</th>
                <th className="px-4 py-2 font-medium">名称</th>
                <th className="px-4 py-2 font-medium">首响时间</th>
                <th className="px-4 py-2 font-medium">解决时间</th>
                <th className="px-4 py-2 font-medium">生效时段</th>
              </tr>
            </thead>
            <tbody>
              {policiesQuery.data!.map((policy) => (
                <PolicyRow key={policy.id} policy={policy} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
