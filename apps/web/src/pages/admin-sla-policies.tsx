import { useState } from 'react';
import { Clock, Sun } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  deleteSlaPolicy,
  fetchSlaPolicies,
  patchSlaPolicy,
  upsertSlaPolicy,
  type SlaPolicy,
  type SlaPolicyCreateInput,
  type SlaPriority,
} from '@/lib/sla-policies';
import { useCurrentUser } from '@/lib/use-current-user';
import { cn } from '@/lib/utils';

// SLA Policies admin page — per-priority SLA targets with full CRUD.
//
// Backend:
//   GET    /api/v1/admin/tenants/{tenant_id}/sla-policies
//   POST   /api/v1/admin/tenants/{tenant_id}/sla-policies      (Tier 2 Task 2.1)
//   PATCH  /api/v1/admin/tenants/{tenant_id}/sla-policies/{id}
//   DELETE /api/v1/admin/tenants/{tenant_id}/sla-policies/{id}
//
// The ticket service stamps ``tickets.sla_deadline_at`` by looking up
// the policy matching the ticket's priority at create time, so the
// page is the only place admins can audit which buckets are configured
// and how aggressive each is.

const SLA_POLICIES_QUERY_KEY = ['admin', 'sla-policies'] as const;

const PRIORITY_OPTIONS: SlaPriority[] = ['P0', 'P1', 'P2', 'P3'];

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

function PolicyEditorRow({
  policy,
  tenantId,
}: {
  policy: SlaPolicy;
  tenantId: string;
}): JSX.Element {
  const qc = useQueryClient();
  const [firstRespMin, setFirstRespMin] = useState(policy.first_response_minutes);
  const [resolutionMin, setResolutionMin] = useState(policy.resolution_minutes);
  const [hoursOnly, setHoursOnly] = useState(policy.business_hours_only);

  const dirty =
    firstRespMin !== policy.first_response_minutes ||
    resolutionMin !== policy.resolution_minutes ||
    hoursOnly !== policy.business_hours_only;

  const patchMut = useMutation({
    mutationFn: () =>
      patchSlaPolicy(tenantId, policy.id, {
        first_response_minutes: firstRespMin,
        resolution_minutes: resolutionMin,
        business_hours_only: hoursOnly,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...SLA_POLICIES_QUERY_KEY, tenantId] });
    },
  });

  const deleteMut = useMutation({
    mutationFn: () => deleteSlaPolicy(tenantId, policy.id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...SLA_POLICIES_QUERY_KEY, tenantId] });
    },
  });

  function handleDelete(): void {
    if (!window.confirm(`删除 SLA 策略 “${policy.name}”?`)) return;
    deleteMut.mutate();
  }

  return (
    <tr
      className="border-b"
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
        <input
          type="number"
          min={1}
          className="w-20 rounded border px-2 py-1 text-sm tabular-nums"
          data-testid={`sla-policy-edit-response-${policy.priority}`}
          value={firstRespMin}
          onChange={(e) => setFirstRespMin(Number.parseInt(e.target.value, 10) || 0)}
        />
        <span className="ml-1 text-xs text-muted-foreground">分钟</span>
      </td>
      <td className="px-4 py-3 align-middle">
        <input
          type="number"
          min={1}
          className="w-24 rounded border px-2 py-1 text-sm tabular-nums"
          data-testid={`sla-policy-edit-resolution-${policy.priority}`}
          value={resolutionMin}
          onChange={(e) => setResolutionMin(Number.parseInt(e.target.value, 10) || 0)}
        />
        <span className="ml-1 text-xs text-muted-foreground">分钟</span>
      </td>
      <td className="px-4 py-3 align-middle">
        <label className="inline-flex items-center gap-1 text-xs">
          <input
            type="checkbox"
            data-testid={`sla-policy-edit-hours-${policy.priority}`}
            checked={hoursOnly}
            onChange={(e) => setHoursOnly(e.target.checked)}
          />
          仅工作时段
        </label>
      </td>
      <td className="px-4 py-3 align-middle">
        <div className="flex gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            data-testid={`sla-policy-save-${policy.priority}`}
            onClick={() => patchMut.mutate()}
            disabled={!dirty || patchMut.isPending}
          >
            {patchMut.isPending ? '保存中…' : '保存'}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            data-testid={`sla-policy-delete-${policy.priority}`}
            onClick={handleDelete}
            disabled={deleteMut.isPending}
          >
            删除
          </Button>
        </div>
      </td>
    </tr>
  );
}

function CreatePolicyForm({ tenantId }: { tenantId: string }): JSX.Element {
  const qc = useQueryClient();
  const [name, setName] = useState('');
  const [priority, setPriority] = useState<SlaPriority>('P2');
  const [firstRespMin, setFirstRespMin] = useState(30);
  const [resolutionMin, setResolutionMin] = useState(240);
  const [hoursOnly, setHoursOnly] = useState(false);

  const upsertMut = useMutation({
    mutationFn: (input: SlaPolicyCreateInput) => upsertSlaPolicy(tenantId, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...SLA_POLICIES_QUERY_KEY, tenantId] });
      setName('');
      setFirstRespMin(30);
      setResolutionMin(240);
      setHoursOnly(false);
    },
  });

  function handleSubmit(): void {
    if (name.trim().length === 0) return;
    upsertMut.mutate({
      name: name.trim(),
      priority,
      first_response_minutes: firstRespMin,
      resolution_minutes: resolutionMin,
      business_hours_only: hoursOnly,
    });
  }

  return (
    <Card data-testid="sla-policy-create-card">
      <CardHeader>
        <CardTitle className="text-base">新建 / 更新 SLA 策略</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-5">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">名称</span>
            <input
              className="rounded border px-2 py-1.5"
              data-testid="sla-policy-create-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={128}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">优先级</span>
            <select
              className="rounded border px-2 py-1.5"
              data-testid="sla-policy-create-priority"
              value={priority}
              onChange={(e) => setPriority(e.target.value as SlaPriority)}
            >
              {PRIORITY_OPTIONS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">首响 (分钟)</span>
            <input
              type="number"
              min={1}
              className="rounded border px-2 py-1.5 tabular-nums"
              data-testid="sla-policy-create-response"
              value={firstRespMin}
              onChange={(e) => setFirstRespMin(Number.parseInt(e.target.value, 10) || 0)}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">解决 (分钟)</span>
            <input
              type="number"
              min={1}
              className="rounded border px-2 py-1.5 tabular-nums"
              data-testid="sla-policy-create-resolution"
              value={resolutionMin}
              onChange={(e) => setResolutionMin(Number.parseInt(e.target.value, 10) || 0)}
            />
          </label>
          <label className="flex flex-col items-start gap-1 text-sm">
            <span className="text-muted-foreground">仅工作时段</span>
            <input
              type="checkbox"
              className="mt-2"
              data-testid="sla-policy-create-hours"
              checked={hoursOnly}
              onChange={(e) => setHoursOnly(e.target.checked)}
            />
          </label>
        </div>
        <div className="mt-3 flex justify-end">
          <Button
            type="button"
            size="sm"
            data-testid="sla-policy-create-submit"
            onClick={handleSubmit}
            disabled={upsertMut.isPending || name.trim().length === 0}
          >
            {upsertMut.isPending ? '保存中…' : '保存'}
          </Button>
        </div>
      </CardContent>
    </Card>
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

      <CreatePolicyForm tenantId={tenantId} />

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
          暂无 SLA 策略 — 上面填一张表提交即创建。
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
                <th className="px-4 py-2 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {policiesQuery.data!.map((policy) => (
                <PolicyEditorRow
                  key={policy.id}
                  policy={policy}
                  tenantId={tenantId}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Footer hint — icons reused from the original spec for visual continuity. */}
      <div className="flex gap-4 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <Clock className="h-3 w-3" /> 工单创建时按 priority 查找策略
        </span>
        <span className="inline-flex items-center gap-1.5">
          <Sun className="h-3 w-3" /> 仅工作时段 — 仅 9-18 (UTC+8) 计入 SLA
        </span>
      </div>
    </div>
  );
}