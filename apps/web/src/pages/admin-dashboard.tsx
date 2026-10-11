import { useMemo } from 'react';
import { BarChart3, MessageSquare, Ticket, Users, Wallet } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  fetchDashboard,
  formatTokenCount,
  topBucket,
  type DashboardRead,
  type StatusCount,
} from '@/lib/dashboard';
import { useCurrentUser } from '@/lib/use-current-user';

// Admin Dashboard page — Tier 2 Task 2.5.
//
// Renders KPI tiles for tickets today + LLM tokens this month + open
// conversations + active users, plus small status/priority breakdowns.
//
// Backend:
//   GET /api/v1/admin/tenants/{tenant_id}/dashboard
//
// The page polls on focus (react-query default refetchOnWindowFocus)
// so the tiles stay close-to-live without hammering the API. The
// payload is counts only — no PII.

const DASHBOARD_QUERY_KEY = ['admin', 'dashboard'] as const;

interface KpiTileProps {
  testId: string;
  label: string;
  value: string;
  subtitle?: string;
}

function KpiTile({ testId, label, value, subtitle }: KpiTileProps): JSX.Element {
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
        className="text-2xl font-semibold tabular-nums"
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
    </div>
  );
}

interface BreakdownListProps {
  testId: string;
  title: string;
  rows: readonly StatusCount[];
  totalLabel: string;
  total: number;
  emptyText: string;
}

function BreakdownList({
  testId,
  title,
  rows,
  totalLabel,
  total,
  emptyText,
}: BreakdownListProps): JSX.Element {
  const sorted = useMemo(
    () => [...rows].sort((a, b) => b.count - a.count),
    [rows],
  );
  const top = topBucket(sorted);
  return (
    <Card data-testid={testId}>
      <CardHeader>
        <CardTitle className="flex items-center justify-between text-base">
          <span>{title}</span>
          <Badge variant="outline" data-testid={`${testId}-total`}>
            {totalLabel} · {total}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {sorted.length === 0 ? (
          <p
            className="text-sm text-muted-foreground"
            data-testid={`${testId}-empty`}
          >
            {emptyText}
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {sorted.map((row) => {
              const pct = total > 0 ? (row.count / total) * 100 : 0;
              const isTop = top !== null && row.key === top.key;
              return (
                <li
                  key={row.key}
                  className="flex items-center justify-between gap-3 text-sm"
                  data-testid={`${testId}-row-${row.key}`}
                >
                  <span className="flex items-center gap-2">
                    <span
                      className={
                        isTop ? 'font-semibold' : 'font-normal'
                      }
                    >
                      {row.key}
                    </span>
                    {isTop && (
                      <Badge variant="secondary" className="text-xs">
                        最高
                      </Badge>
                    )}
                  </span>
                  <span className="flex items-center gap-2 tabular-nums">
                    <span
                      className="inline-block h-1.5 rounded-full bg-primary/70"
                      style={{ width: `${Math.max(2, pct)}px` }}
                      aria-hidden
                    />
                    <span>{row.count}</span>
                    <span className="text-xs text-muted-foreground">
                      {pct.toFixed(0)}%
                    </span>
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function DashboardBody({
  data,
  refresh,
}: {
  data: DashboardRead;
  refresh: () => void;
}): JSX.Element {
  const totalTickets = data.tickets_by_status.reduce(
    (sum, row) => sum + row.count,
    0,
  );
  const totalPriority = data.tickets_by_priority.reduce(
    (sum, row) => sum + row.count,
    0,
  );
  return (
    <div
      className="flex flex-col gap-6"
      data-testid="admin-dashboard-body"
    >
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <KpiTile
          testId="dashboard-tile-tickets-today"
          label="今日工单"
          value={formatTokenCount(data.tickets_today)}
          subtitle={`自 ${data.today_utc_start.slice(0, 10)} UTC`}
        />
        <KpiTile
          testId="dashboard-tile-conversations-open"
          label="进行中会话"
          value={formatTokenCount(data.conversations_open)}
          subtitle="open + pending"
        />
        <KpiTile
          testId="dashboard-tile-llm-tokens"
          label="本月 LLM tokens"
          value={formatTokenCount(data.llm_tokens_this_period)}
          subtitle={`周期 ${data.period}`}
        />
        <KpiTile
          testId="dashboard-tile-users"
          label="活跃用户"
          value={formatTokenCount(data.active_users)}
          subtitle={`共 ${formatTokenCount(data.total_users)} 人`}
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <BreakdownList
          testId="dashboard-breakdown-status"
          title="工单状态分布"
          rows={data.tickets_by_status}
          totalLabel="总工单"
          total={totalTickets}
          emptyText="暂无工单"
        />
        <BreakdownList
          testId="dashboard-breakdown-priority"
          title="工单优先级分布"
          rows={data.tickets_by_priority}
          totalLabel="总工单"
          total={totalPriority}
          emptyText="暂无工单"
        />
      </div>

      <div className="flex justify-end">
        <button
          type="button"
          className="text-xs text-muted-foreground underline"
          onClick={refresh}
          data-testid="dashboard-refresh"
        >
          刷新
        </button>
      </div>
    </div>
  );
}

export function AdminDashboardPage(): JSX.Element {
  const { user } = useCurrentUser();
  const tenantId = user?.tenant_id ?? '';

  const query = useQuery({
    queryKey: [...DASHBOARD_QUERY_KEY, tenantId],
    queryFn: () => fetchDashboard(tenantId),
    enabled: Boolean(tenantId),
    refetchInterval: 60_000,
  });

  return (
    <div
      className="flex h-full flex-col gap-6 p-6"
      data-testid="admin-dashboard-page"
    >
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <BarChart3 className="h-5 w-5" />
          仪表盘
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          当前租户的关键指标 — 工单、对话、LLM 用量、用户。
        </p>
      </div>

      {query.isLoading ? (
        <p
          className="text-sm text-muted-foreground"
          data-testid="dashboard-loading"
        >
          加载中…
        </p>
      ) : query.isError ? (
        <p
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          role="alert"
          data-testid="dashboard-error"
        >
          加载失败:{' '}
          {query.error instanceof Error
            ? query.error.message
            : '未知错误'}
        </p>
      ) : query.data ? (
        <DashboardBody data={query.data} refresh={() => void query.refetch()} />
      ) : null}

      <DevIconLegend />
    </div>
  );
}

// Inline legend that explains the icons used on the page; rendered
// at the bottom so a screenshot of the dashboard is self-explanatory.
function DevIconLegend(): JSX.Element {
  return (
    <div
      className="mt-auto flex flex-wrap items-center gap-3 text-xs text-muted-foreground"
      data-testid="dashboard-icon-legend"
    >
      <span className="inline-flex items-center gap-1">
        <Ticket className="h-3 w-3" /> 工单
      </span>
      <span className="inline-flex items-center gap-1">
        <MessageSquare className="h-3 w-3" /> 会话
      </span>
      <span className="inline-flex items-center gap-1">
        <Wallet className="h-3 w-3" /> 预算
      </span>
      <span className="inline-flex items-center gap-1">
        <Users className="h-3 w-3" /> 用户
      </span>
    </div>
  );
}