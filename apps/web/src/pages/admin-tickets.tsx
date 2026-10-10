import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { AxiosError } from 'axios';
import { ArrowUpRight } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  DEFAULT_TICKET_FILTERS,
  fetchTickets,
  ticketsQueryKey,
  type Ticket,
  type TicketFilters,
  type TicketPriority,
  type TicketStatus,
} from '@/lib/tickets';

// ---------------------------------------------------------------------------
// Display constants
// ---------------------------------------------------------------------------

/**
 * Status pill colors. Match the conversation status badges for visual
 * consistency across the SPA. The seven states map to:
 *   - new / triaged: blue (active, unstarted)
 *   - in_progress: amber (active work)
 *   - waiting_customer: violet (blocked on customer)
 *   - resolved: emerald (positive, still reversible)
 *   - closed: zinc (terminal, neutral)
 *   - cancelled: zinc + strikethrough feel via "destructive" outline
 */
const STATUS_LABEL: Record<TicketStatus, string> = {
  new: '新建',
  triaged: '已分诊',
  in_progress: '处理中',
  waiting_customer: '等待客户',
  resolved: '已解决',
  closed: '已关闭',
  cancelled: '已取消',
};

const STATUS_PILL: Record<TicketStatus, string> = {
  new: 'bg-blue-100 text-blue-900 dark:bg-blue-900/40 dark:text-blue-200',
  triaged: 'bg-blue-100 text-blue-900 dark:bg-blue-900/40 dark:text-blue-200',
  in_progress: 'bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200',
  waiting_customer: 'bg-violet-100 text-violet-900 dark:bg-violet-900/40 dark:text-violet-200',
  resolved: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200',
  closed: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700/60 dark:text-zinc-200',
  cancelled: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700/60 dark:text-zinc-200',
};

const PRIORITY_LABEL: Record<TicketPriority, string> = {
  P0: 'P0 紧急',
  P1: 'P1 高',
  P2: 'P2 中',
  P3: 'P3 低',
};

/**
 * Priority hues — P0/P1 are warm to draw attention, P2/P3 cool. Same
 * palette the rest of the SPA uses for severity.
 */
const PRIORITY_BADGE: Record<TicketPriority, string> = {
  P0: 'bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200',
  P1: 'bg-orange-100 text-orange-900 dark:bg-orange-900/40 dark:text-orange-200',
  P2: 'bg-blue-100 text-blue-900 dark:bg-blue-900/40 dark:text-blue-200',
  P3: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700/60 dark:text-zinc-200',
};

const ALL_STATUSES: TicketStatus[] = [
  'new',
  'triaged',
  'in_progress',
  'waiting_customer',
  'resolved',
  'closed',
  'cancelled',
];
const ALL_PRIORITIES: TicketPriority[] = ['P0', 'P1', 'P2', 'P3'];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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
    return error.message || '请求失败,请稍后再试。';
  }
  if (error instanceof Error && error.message) return error.message;
  return '请求失败,请稍后再试。';
}

/**
 * Format a timestamp as a short Chinese-style date+time. Falls back to
 * the raw string if the input isn't a valid ISO date.
 */
function formatDateTime(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('zh-CN', { hour12: false });
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

/**
 * Admin tickets list page. Shows every ticket for the caller's tenant
 * (the API is tenant-scoped via JWT). Filter bar at the top, table
 * below. Click a row to navigate to the detail page.
 *
 * NOTE: the backend currently has no `GET /api/v1/tickets` list endpoint.
 * See `lib/tickets.ts` for the fallback strategy. In practice the list
 * will be empty until the admin list endpoint is added server-side; the
 * empty-state copy says so explicitly.
 */
export function AdminTicketsPage(): JSX.Element {
  const navigate = useNavigate();
  const [statusFilter, setStatusFilter] = useState<TicketStatus | null>(
    DEFAULT_TICKET_FILTERS.status,
  );
  const [priorityFilter, setPriorityFilter] = useState<TicketPriority | null>(
    DEFAULT_TICKET_FILTERS.priority,
  );

  const filters: TicketFilters = { status: statusFilter, priority: priorityFilter };

  const ticketsQuery = useQuery({
    queryKey: ticketsQueryKey(filters),
    queryFn: () => fetchTickets(filters),
  });

  const tickets = ticketsQuery.data ?? [];

  const handleRowClick = (ticketId: string): void => {
    navigate(`/admin/tickets/${ticketId}`);
  };

  return (
    <div
      className="flex h-full flex-col gap-6 overflow-hidden p-6"
      data-testid="admin-tickets-page"
    >
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">工单</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          所有 tenant 下的工单(支持按状态/优先级过滤)
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <label htmlFor="tickets-filter-status" className="text-sm text-muted-foreground">
            状态
          </label>
          <select
            id="tickets-filter-status"
            data-testid="tickets-filter-status"
            value={statusFilter ?? 'all'}
            onChange={(e) => {
              const v = e.target.value;
              setStatusFilter(v === 'all' ? null : (v as TicketStatus));
            }}
            className="h-9 rounded-md border border-input bg-background px-2 text-sm"
          >
            <option value="all">全部</option>
            {ALL_STATUSES.map((s) => (
              <option key={s} value={s}>
                {STATUS_LABEL[s]}
              </option>
            ))}
          </select>
        </div>

        <div className="flex items-center gap-2">
          <label htmlFor="tickets-filter-priority" className="text-sm text-muted-foreground">
            优先级
          </label>
          <select
            id="tickets-filter-priority"
            data-testid="tickets-filter-priority"
            value={priorityFilter ?? 'all'}
            onChange={(e) => {
              const v = e.target.value;
              setPriorityFilter(v === 'all' ? null : (v as TicketPriority));
            }}
            className="h-9 rounded-md border border-input bg-background px-2 text-sm"
          >
            <option value="all">全部</option>
            {ALL_PRIORITIES.map((p) => (
              <option key={p} value={p}>
                {PRIORITY_LABEL[p]}
              </option>
            ))}
          </select>
        </div>

        <div className="ml-auto text-sm text-muted-foreground">
          {ticketsQuery.isSuccess ? `共 ${tickets.length} 条` : null}
        </div>
      </div>

      <Card className="flex flex-1 flex-col overflow-hidden">
        <CardHeader className="pb-2">
          <CardTitle className="text-lg">工单列表</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-1 flex-col overflow-hidden p-0">
          <div
            className="grid grid-cols-[100px_120px_1fr_140px_180px_180px] items-center gap-3 border-b bg-muted/30 px-4 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground"
            role="row"
          >
            <span>优先级</span>
            <span>状态</span>
            <span>主题</span>
            <span>受理人</span>
            <span>SLA 截止</span>
            <span>创建时间</span>
          </div>

          {ticketsQuery.isError ? (
            <div
              className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center"
              role="alert"
              data-testid="tickets-error"
            >
              <p className="text-sm text-destructive">
                {extractErrorMessage(ticketsQuery.error)}
              </p>
              <Button
                type="button"
                size="sm"
                onClick={() => void ticketsQuery.refetch()}
              >
                重试
              </Button>
            </div>
          ) : ticketsQuery.isPending ? (
            <div className="flex-1 overflow-auto" data-testid="tickets-loading">
              {Array.from({ length: 5 }).map((_, idx) => (
                <div
                  key={idx}
                  className="grid grid-cols-[100px_120px_1fr_140px_180px_180px] items-center gap-3 border-b px-4 py-3"
                >
                  <span className="h-5 w-14 animate-pulse rounded-full bg-muted" />
                  <span className="h-5 w-16 animate-pulse rounded-full bg-muted" />
                  <span className="h-4 w-3/4 animate-pulse rounded bg-muted" />
                  <span className="h-4 w-20 animate-pulse rounded bg-muted" />
                  <span className="h-4 w-32 animate-pulse rounded bg-muted" />
                  <span className="h-4 w-32 animate-pulse rounded bg-muted" />
                </div>
              ))}
            </div>
          ) : tickets.length === 0 ? (
            <div
              className="flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center text-sm text-muted-foreground"
              data-testid="tickets-empty"
            >
              <p className="text-base text-foreground">暂无工单</p>
              <p>当前筛选条件下没有匹配的工单。</p>
            </div>
          ) : (
            <div className="flex-1 overflow-auto" data-testid="tickets-list">
              {tickets.map((ticket) => (
                <TicketRow
                  key={ticket.id}
                  ticket={ticket}
                  onClick={() => handleRowClick(ticket.id)}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Row
// ---------------------------------------------------------------------------

interface TicketRowProps {
  ticket: Ticket;
  onClick: () => void;
}

function TicketRow({ ticket, onClick }: TicketRowProps): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={`ticket-row-${ticket.id}`}
      data-ticket-id={ticket.id}
      className="grid w-full grid-cols-[100px_120px_1fr_140px_180px_180px] items-center gap-3 border-b px-4 py-3 text-left text-sm transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <span
        className={`inline-flex w-fit items-center rounded-full px-2 py-0.5 text-xs font-medium ${PRIORITY_BADGE[ticket.priority]}`}
        data-testid="ticket-priority"
      >
        {PRIORITY_LABEL[ticket.priority]}
      </span>
      <span
        className={`inline-flex w-fit items-center rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_PILL[ticket.status]}`}
        data-testid="ticket-status"
      >
        {STATUS_LABEL[ticket.status]}
      </span>
      <span
        className="flex items-center gap-1 truncate font-medium text-foreground"
        data-testid="ticket-subject"
      >
        <span className="truncate">{ticket.subject}</span>
        <ArrowUpRight className="size-3.5 shrink-0 text-muted-foreground" />
      </span>
      <span
        className="truncate text-muted-foreground"
        data-testid="ticket-assignee"
      >
        {ticket.assignee_agent_id ?? '未分配'}
      </span>
      <span
        className="truncate text-muted-foreground"
        data-testid="ticket-sla"
      >
        {formatDateTime(ticket.sla_deadline_at)}
      </span>
      <span
        className="truncate text-muted-foreground"
        data-testid="ticket-created"
      >
        {formatDateTime(ticket.created_at)}
      </span>
    </button>
  );
}
