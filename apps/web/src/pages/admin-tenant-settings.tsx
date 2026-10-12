import { useEffect, useState } from 'react';
import { Building2, UserCheck } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  ASSIGNMENT_STRATEGIES,
  fetchAssignmentPolicy,
  patchAssignmentPolicy,
  type AssignmentPolicy,
  type AssignmentStrategy,
} from '@/lib/assignment-policy';
import {
  fetchTenantSettings,
  patchTenantSettings,
  TENANT_PLANS,
  TENANT_STATUSES,
  type TenantPlan,
  type TenantSettings,
  type TenantStatus,
} from '@/lib/tenant-settings';
import { useCurrentUser } from '@/lib/use-current-user';
import { cn } from '@/lib/utils';

// Tenant self-settings admin page — view + edit the current tenant's
// name / plan / status.
//
// Backend:
//   GET    /api/v1/admin/tenants/{tid}/settings
//   PATCH  /api/v1/admin/tenants/{tid}/settings
//
// Scope: a tenant-scoped admin can only edit their own tenant (the
// server enforces claims['tenant_id'] == path tenant_id, anti-
// enumeration). Listing all tenants would require a super-admin role
// that does not exist in the current JWT model — that's deliberately
// out of scope.

const TENANT_SETTINGS_QUERY_KEY = ['admin', 'tenant-settings'] as const;

const PLAN_STYLES: Record<TenantPlan, string> = {
  free: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-200',
  pro: 'bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200',
  enterprise:
    'bg-violet-100 text-violet-900 dark:bg-violet-900/40 dark:text-violet-200',
};

const STATUS_STYLES: Record<TenantStatus, string> = {
  active:
    'bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200',
  suspended:
    'bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200',
};

function PlanBadge({ plan }: { plan: TenantPlan }): JSX.Element {
  return (
    <Badge
      variant="outline"
      className={cn('border-transparent font-semibold', PLAN_STYLES[plan])}
      data-testid={`tenant-plan-badge-${plan}`}
    >
      {plan}
    </Badge>
  );
}

function StatusBadge({ status }: { status: TenantStatus }): JSX.Element {
  return (
    <Badge
      variant="outline"
      className={cn('border-transparent font-semibold', STATUS_STYLES[status])}
      data-testid={`tenant-status-badge-${status}`}
    >
      {status}
    </Badge>
  );
}

function TenantSettingsEditor({
  initial,
  tenantId,
}: {
  initial: TenantSettings;
  tenantId: string;
}): JSX.Element {
  const qc = useQueryClient();
  const [name, setName] = useState(initial.name);
  const [plan, setPlan] = useState<TenantPlan>(initial.plan);
  const [status, setStatus] = useState<TenantStatus>(initial.status);

  // Reset local state when the server-side row changes (e.g. after a
  // successful PATCH followed by invalidation).
  useEffect(() => {
    setName(initial.name);
    setPlan(initial.plan);
    setStatus(initial.status);
  }, [initial.name, initial.plan, initial.status]);

  const dirty =
    name !== initial.name || plan !== initial.plan || status !== initial.status;

  const patchMut = useMutation({
    mutationFn: () => patchTenantSettings(tenantId, { name, plan, status }),
    onSuccess: (next) => {
      void qc.setQueryData(
        [...TENANT_SETTINGS_QUERY_KEY, tenantId],
        next,
      );
    },
  });

  const nameInvalid = name.trim().length === 0;

  return (
    <Card data-testid="tenant-settings-card">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Building2 className="h-4 w-4" />
          编辑租户设置
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">名称</span>
            <input
              className={cn(
                'rounded border px-2 py-1.5',
                nameInvalid && 'border-destructive',
              )}
              data-testid="tenant-settings-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={200}
            />
            {nameInvalid && (
              <span
                className="text-xs text-destructive"
                data-testid="tenant-settings-name-error"
              >
                名称不能为空
              </span>
            )}
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">套餐</span>
            <select
              className="rounded border px-2 py-1.5"
              data-testid="tenant-settings-plan"
              value={plan}
              onChange={(e) => setPlan(e.target.value as TenantPlan)}
            >
              {TENANT_PLANS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">状态</span>
            <select
              className="rounded border px-2 py-1.5"
              data-testid="tenant-settings-status"
              value={status}
              onChange={(e) => setStatus(e.target.value as TenantStatus)}
            >
              {TENANT_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="mt-3 flex justify-end">
          <Button
            type="button"
            size="sm"
            data-testid="tenant-settings-save"
            onClick={() => patchMut.mutate()}
            disabled={!dirty || nameInvalid || patchMut.isPending}
          >
            {patchMut.isPending ? '保存中…' : '保存'}
          </Button>
        </div>
        {patchMut.isError && (
          <p
            className="mt-2 text-xs text-destructive"
            role="alert"
            data-testid="tenant-settings-error"
          >
            保存失败:{' '}
            {patchMut.error instanceof Error
              ? patchMut.error.message
              : '未知错误'}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

const ASSIGNMENT_POLICY_QUERY_KEY = ['admin', 'assignment-policy'] as const;

function AssignmentPolicyEditor({
  initial,
  tenantId,
}: {
  initial: AssignmentPolicy;
  tenantId: string;
}): JSX.Element {
  const qc = useQueryClient();
  const [enabled, setEnabled] = useState(initial.enabled);
  const [strategy, setStrategy] = useState<AssignmentStrategy>(initial.strategy);

  // Re-sync local state when the underlying query refetches (e.g.
  // after a successful PATCH followed by invalidation).
  useEffect(() => {
    setEnabled(initial.enabled);
    setStrategy(initial.strategy);
  }, [initial.enabled, initial.strategy]);

  const dirty = enabled !== initial.enabled || strategy !== initial.strategy;

  const patchMut = useMutation({
    mutationFn: () => patchAssignmentPolicy(tenantId, { enabled, strategy }),
    onSuccess: (next) => {
      void qc.setQueryData(
        [...ASSIGNMENT_POLICY_QUERY_KEY, tenantId],
        next,
      );
    },
  });

  return (
    <Card data-testid="assignment-policy-card">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <UserCheck className="h-4 w-4" />
          自动分配策略
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">策略</span>
            <select
              className="rounded border px-2 py-1.5"
              data-testid="assignment-policy-strategy"
              value={strategy}
              onChange={(e) =>
                setStrategy(e.target.value as AssignmentStrategy)
              }
            >
              {ASSIGNMENT_STRATEGIES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
            <span
              className="text-xs text-muted-foreground"
              data-testid="assignment-policy-hint"
            >
              {ASSIGNMENT_STRATEGIES.find((s) => s.value === strategy)?.hint}
            </span>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">启用</span>
            <label
              className="flex items-center gap-2 rounded border px-2 py-1.5"
              data-testid="assignment-policy-enabled-row"
            >
              <input
                type="checkbox"
                data-testid="assignment-policy-enabled"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
              />
              <span>{enabled ? '已启用' : '未启用'}</span>
            </label>
            <span className="text-xs text-muted-foreground">
              未启用时 escalate 后保留 PENDING+NULL,坐席手动认领
            </span>
          </label>
        </div>
        <div className="mt-3 flex justify-end">
          <Button
            type="button"
            size="sm"
            data-testid="assignment-policy-save"
            onClick={() => patchMut.mutate()}
            disabled={!dirty || patchMut.isPending}
          >
            {patchMut.isPending ? '保存中…' : '保存'}
          </Button>
        </div>
        {patchMut.isError && (
          <p
            className="mt-2 text-xs text-destructive"
            role="alert"
            data-testid="assignment-policy-error"
          >
            保存失败:{' '}
            {patchMut.error instanceof Error
              ? patchMut.error.message
              : '未知错误'}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

export function AdminTenantSettingsPage(): JSX.Element {
  const { user } = useCurrentUser();
  const tenantId = user?.tenant_id ?? '';

  const settingsQuery = useQuery({
    queryKey: [...TENANT_SETTINGS_QUERY_KEY, tenantId],
    queryFn: () => fetchTenantSettings(tenantId),
    enabled: Boolean(tenantId),
  });

  const policyQuery = useQuery({
    queryKey: [...ASSIGNMENT_POLICY_QUERY_KEY, tenantId],
    queryFn: () => fetchAssignmentPolicy(tenantId),
    enabled: Boolean(tenantId),
  });

  return (
    <div
      className="flex h-full flex-col gap-6 p-6"
      data-testid="admin-tenant-settings-page"
    >
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">租户设置</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          查看 / 编辑当前租户的展示名称、套餐、状态与自动分配策略。
        </p>
      </div>

      {settingsQuery.isLoading ? (
        <p
          className="text-sm text-muted-foreground"
          data-testid="tenant-settings-loading"
        >
          加载中…
        </p>
      ) : settingsQuery.isError ? (
        <p
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          role="alert"
          data-testid="tenant-settings-error-loading"
        >
          加载失败:{' '}
          {settingsQuery.error instanceof Error
            ? settingsQuery.error.message
            : '未知错误'}
        </p>
      ) : settingsQuery.data ? (
        <>
          <Card data-testid="tenant-settings-summary">
            <CardHeader>
              <CardTitle className="text-base">{settingsQuery.data.name}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex flex-wrap items-center gap-2">
                <PlanBadge plan={settingsQuery.data.plan} />
                <StatusBadge status={settingsQuery.data.status} />
                <span
                  className="text-xs text-muted-foreground"
                  data-testid="tenant-settings-id"
                >
                  ID: {settingsQuery.data.id}
                </span>
              </div>
            </CardContent>
          </Card>
          <TenantSettingsEditor
            initial={settingsQuery.data}
            tenantId={tenantId}
          />
          {policyQuery.data ? (
            <AssignmentPolicyEditor
              initial={policyQuery.data}
              tenantId={tenantId}
            />
          ) : policyQuery.isError ? (
            <p
              className="text-xs text-muted-foreground"
              data-testid="assignment-policy-load-error"
            >
              自动分配策略加载失败,继续编辑租户设置即可。
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}