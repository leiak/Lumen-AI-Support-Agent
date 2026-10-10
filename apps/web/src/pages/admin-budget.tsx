import { useMemo, useState } from 'react';
import { AxiosError } from 'axios';
import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';

import { BudgetEditDialog } from '@/components/admin/budget-edit-dialog';
import { CreditGrantDialog } from '@/components/admin/credit-grant-dialog';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useCurrentUser } from '@/lib/use-current-user';
import {
  BAND_BAR_CLASS,
  BAND_TEXT_CLASS,
  BudgetUsageBreakdownItemSchema,
  classifyUsageAgainstThresholds,
  fetchBudget,
  fetchBudgetCredits,
  fetchBudgetUsage,
  formatTokens,
  type BudgetUsage,
  type BudgetCredit,
  type BudgetUsageBreakdownItem,
} from '@/lib/budget';

const BUDGET_QUERY_KEY = ['admin', 'budget'] as const;

interface FetchErrorPayload {
  detail?: string | Array<{ msg?: string }>;
}

/** Render an AxiosError / unknown into a UI-safe Chinese error string. */
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
    return error.message || '加载预算数据失败,请稍后再试。';
  }
  if (error instanceof Error && error.message) return error.message;
  return '加载预算数据失败,请稍后再试。';
}

/** Current UTC period (YYYY-MM). Matches ``budget.resolver._current_period``. */
function currentPeriod(now: Date = new Date()): string {
  return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, '0')}`;
}

interface StatTileProps {
  testId: string;
  label: string;
  value: string;
  subtitle?: string;
  valueClass?: string;
  /** 0..1 — when present, a thin progress bar is rendered below the value. */
  progress?: number;
  progressClass?: string;
}

function StatTile(props: StatTileProps): JSX.Element {
  const {
    testId,
    label,
    value,
    subtitle,
    valueClass = 'text-foreground',
    progress,
    progressClass = 'bg-emerald-500',
  } = props;

  // Clamp to 0..1 so a misconfigured server can't blow out the bar.
  const safeProgress =
    progress === undefined
      ? null
      : Math.max(0, Math.min(1, progress));

  return (
    <div
      data-testid={testId}
      className="flex flex-col gap-1 rounded-md border bg-background p-4"
    >
      <span className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span
        data-testid={`${testId}-value`}
        className={`text-2xl font-semibold tabular-nums ${valueClass}`}
      >
        {value}
      </span>
      {subtitle !== undefined && (
        <span
          data-testid={`${testId}-subtitle`}
          className="text-xs text-muted-foreground"
        >
          {subtitle}
        </span>
      )}
      {safeProgress !== null && (
        <div
          className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-muted"
          role="progressbar"
          aria-valuenow={Math.round(safeProgress * 100)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            data-testid={`${testId}-bar`}
            className={`h-full transition-all ${progressClass}`}
            style={{ width: `${safeProgress * 100}%` }}
          />
        </div>
      )}
    </div>
  );
}

interface BreakdownRowProps {
  item: BudgetUsageBreakdownItem;
  percentOfTotal: number;
}

function BreakdownRow({ item, percentOfTotal }: BreakdownRowProps): JSX.Element {
  return (
    <tr
      data-testid={`budget-breakdown-row-${item.model}`}
      className="border-b last:border-0"
    >
      <td className="py-2 align-top">
        <div className="font-medium">{item.model}</div>
        <div className="text-xs text-muted-foreground">{item.provider}</div>
      </td>
      <td className="py-2 tabular-nums align-top">
        {formatTokens(item.total_tokens)}
        <div className="text-xs text-muted-foreground">
          {formatTokens(item.prompt_tokens)} + {formatTokens(item.completion_tokens)}
        </div>
      </td>
      <td className="py-2 tabular-nums align-top">
        {percentOfTotal.toFixed(1)}%
        <div className="text-xs text-muted-foreground">
          {item.request_count} 次请求
        </div>
      </td>
    </tr>
  );
}

interface CreditRowProps {
  credit: BudgetCredit;
}

function CreditRow({ credit }: CreditRowProps): JSX.Element {
  return (
    <tr
      data-testid={`budget-credit-row-${credit.id}`}
      className="border-b last:border-0"
    >
      <td className="py-2 align-top">
        <div className="font-medium">{credit.note}</div>
        <div className="text-xs text-muted-foreground">{credit.created_at}</div>
      </td>
      <td className="py-2 tabular-nums align-top">
        +{formatTokens(credit.tokens)}
      </td>
      <td className="py-2 align-top">
        <code className="text-xs">{credit.granted_by}</code>
      </td>
    </tr>
  );
}

/**
 * Admin Budget Dashboard (M4.D + Pack B close-out).
 *
 * Reads the caller's tenant from ``useCurrentUser()`` and renders three
 * stacked cards:
 *
 * 1. Usage summary — 4 stat tiles (硬上限 / 已用 / 有效上限 / 使用率)
 *    colored against the soft_warn / hard_cap thresholds.
 * 2. Per-model breakdown — hidden when the usage response doesn't
 *    carry ``?breakdown=true`` (server returns ``breakdown: null``).
 * 3. Credits list — empty state when no grants; also empty when the
 *    per-tenant admin gets a 404 from the super-admin-only endpoint.
 *
 * No editing surface yet — this is the read-only close-out. Mutation
 * flows (set hard cap, grant credit) are operator-only API calls.
 */
export function AdminBudgetPage(): JSX.Element {
  const { user } = useCurrentUser();
  const tenantId = user?.tenant_id ?? '';
  const period = useMemo(() => currentPeriod(), []);

  // Tier 1 Task 1.4: dialog open state for budget edit + credit grant.
  const [editOpen, setEditOpen] = useState(false);
  const [grantOpen, setGrantOpen] = useState(false);

  const budgetQuery = useQuery({
    queryKey: [...BUDGET_QUERY_KEY, tenantId, 'config'],
    queryFn: () => fetchBudget(tenantId),
    enabled: Boolean(tenantId),
  });

  const usageQuery = useQuery({
    queryKey: [...BUDGET_QUERY_KEY, tenantId, 'usage'],
    queryFn: () => fetchBudgetUsage(tenantId),
    enabled: Boolean(tenantId),
  });

  const creditsQuery = useQuery({
    queryKey: [...BUDGET_QUERY_KEY, tenantId, 'credits', period],
    queryFn: () => fetchBudgetCredits(tenantId, period),
    // The credits endpoint is super-admin only. Per-tenant admins get
    // a 404; we don't want react-query to retry that 4 times before
    // giving up — disable retries so the empty state shows up fast.
    retry: false,
    enabled: Boolean(tenantId),
  });

  // -- Normalized view-model ------------------------------------------------
  const usage: BudgetUsage | null = usageQuery.data ?? null;
  const tokensUsed = usage?.tokens_used ?? 0;
  const hardCap = usage?.hard_cap_tokens ?? null;
  const softWarn = usage?.soft_warn_tokens ?? null;
  const effectiveCap = usage?.effective_cap ?? 0;
  const creditsTotal = usage?.credits_total ?? 0;
  const breakdown = usage?.breakdown ?? null;

  const band = classifyUsageAgainstThresholds(tokensUsed, softWarn, hardCap);
  const bandTextClass = BAND_TEXT_CLASS[band];
  const bandBarClass = BAND_BAR_CLASS[band];
  const percentFraction = effectiveCap > 0 ? tokensUsed / effectiveCap : 0;

  // Guard the breakdown array against a malformed server payload so a
  // shape mismatch doesn't crash the page. zod parse in production,
  // but a non-throwing fallback is cheap insurance.
  const safeBreakdown: BudgetUsageBreakdownItem[] = (() => {
    if (!breakdown) return [];
    const parsed = z.array(BudgetUsageBreakdownItemSchema).safeParse(breakdown);
    return parsed.success ? parsed.data : [];
  })();
  const breakdownTotal = safeBreakdown.reduce(
    (acc, b) => acc + b.total_tokens,
    0,
  );

  // Credits: empty array on either "no grants" OR "endpoint unreachable
  // for this role" — the page degrades gracefully in both cases.
  const credits: BudgetCredit[] = creditsQuery.data?.credits ?? [];

  // -- Render ---------------------------------------------------------------
  return (
    <div
      className="flex h-full w-full flex-col gap-4 overflow-auto p-4"
      data-testid="admin-budget-page"
    >
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">预算</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          租户月度 token 硬上限 + 软告警阈值
        </p>
      </header>

      {/* ----- 1. Usage summary ---------------------------------------- */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <CardTitle className="text-base">
            当期用量
            {usage ? `（${usage.period}）` : ''}
          </CardTitle>
          <Button
            type="button"
            size="sm"
            variant="outline"
            data-testid="budget-edit-open"
            onClick={() => setEditOpen(true)}
            disabled={!budgetQuery.data}
          >
            编辑预算
          </Button>
        </CardHeader>
        <CardContent>
          {usageQuery.isError ? (
            <p
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
              role="alert"
              data-testid="budget-usage-error"
            >
              加载用量失败:{extractErrorMessage(usageQuery.error)}
            </p>
          ) : usageQuery.isLoading || !usage ? (
            <p
              className="text-sm text-muted-foreground"
              data-testid="budget-usage-loading"
            >
              加载中…
            </p>
          ) : (
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatTile
                testId="budget-stat-hard-cap"
                label="硬上限"
                value={formatTokens(hardCap)}
              />
              <StatTile
                testId="budget-stat-tokens-used"
                label="已用"
                value={formatTokens(tokensUsed)}
                valueClass={bandTextClass}
              />
              <StatTile
                testId="budget-stat-effective-cap"
                label="有效上限"
                value={formatTokens(effectiveCap)}
                subtitle={`+${formatTokens(creditsTotal)} credits`}
              />
              <StatTile
                testId="budget-stat-percent"
                label="使用率"
                value={`${(percentFraction * 100).toFixed(1)}%`}
                progress={percentFraction}
                progressClass={bandBarClass}
              />
            </div>
          )}
        </CardContent>
      </Card>

      {/* ----- 2. Per-model breakdown (only when populated) ---------- */}
      {safeBreakdown.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">按模型用量</CardTitle>
          </CardHeader>
          <CardContent>
            <table className="w-full text-sm" data-testid="budget-breakdown-table">
              <thead>
                <tr className="border-b text-left">
                  <th className="pb-2 font-medium">模型</th>
                  <th className="pb-2 font-medium">tokens</th>
                  <th className="pb-2 font-medium">占比</th>
                </tr>
              </thead>
              <tbody>
                {safeBreakdown.map((item) => {
                  const pct = breakdownTotal > 0
                    ? (item.total_tokens / breakdownTotal) * 100
                    : 0;
                  return (
                    <BreakdownRow
                      key={`${item.provider}/${item.model}`}
                      item={item}
                      percentOfTotal={pct}
                    />
                  );
                })}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}

      {/* ----- 3. Credits list ---------------------------------------- */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <CardTitle className="text-base">Credits 历史</CardTitle>
          <Button
            type="button"
            size="sm"
            variant="outline"
            data-testid="credit-grant-open"
            onClick={() => setGrantOpen(true)}
          >
            发放 Credit
          </Button>
        </CardHeader>
        <CardContent>
          {creditsQuery.isLoading ? (
            <p
              className="text-sm text-muted-foreground"
              data-testid="budget-credits-loading"
            >
              加载中…
            </p>
          ) : credits.length === 0 ? (
            <p
              className="text-sm text-muted-foreground"
              data-testid="budget-credits-empty"
            >
              暂无 credits
            </p>
          ) : (
            <table className="w-full text-sm" data-testid="budget-credits-table">
              <thead>
                <tr className="border-b text-left">
                  <th className="pb-2 font-medium">原因</th>
                  <th className="pb-2 font-medium">数量</th>
                  <th className="pb-2 font-medium">授予人</th>
                </tr>
              </thead>
              <tbody>
                {credits.map((c) => (
                  <CreditRow key={c.id} credit={c} />
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      {/* ----- Optional config card (collapsed, for cross-checking) ---- */}
      {budgetQuery.data && (
        <p
          className="text-xs text-muted-foreground"
          data-testid="budget-config-meta"
        >
          周期锚点时区: {budgetQuery.data.period_anchor_tz} · 上次更新: {budgetQuery.data.updated_at}
        </p>
      )}

      {/* ----- Mutation dialogs (Tier 1 Task 1.4) ---------------------- */}
      <BudgetEditDialog
        tenantId={tenantId}
        current={budgetQuery.data ?? null}
        open={editOpen}
        onOpenChange={setEditOpen}
      />
      <CreditGrantDialog
        tenantId={tenantId}
        period={period}
        open={grantOpen}
        onOpenChange={setGrantOpen}
      />
    </div>
  );
}

// Re-export the zod schema so consumers that need to validate raw
// payloads (e.g. the test) can pull it from one place.
export { BudgetUsageBreakdownItemSchema };
