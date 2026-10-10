import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AxiosError } from 'axios';
import { ArrowLeft, Bot, CircleAlert, CircleCheck, MessageCircle, RefreshCw, User } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { MessageStream } from '@/components/conversation/message-stream';
import {
  fetchTicket,
  fetchTicketEvents,
  nextTicketStatuses,
  ticketDetailQueryKey,
  ticketEventsQueryKey,
  transitionTicket,
  type Ticket,
  type TicketEvent,
  type TicketStatus,
} from '@/lib/tickets';
import { fetchMessages, messagesQueryKey } from '@/lib/messages';

// ---------------------------------------------------------------------------
// Display constants (mirror admin-tickets.tsx for visual consistency)
// ---------------------------------------------------------------------------

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

const PRIORITY_LABEL: Record<Ticket['priority'], string> = {
  P0: 'P0 紧急',
  P1: 'P1 高',
  P2: 'P2 中',
  P3: 'P3 低',
};

const PRIORITY_BADGE: Record<Ticket['priority'], string> = {
  P0: 'bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200',
  P1: 'bg-orange-100 text-orange-900 dark:bg-orange-900/40 dark:text-orange-200',
  P2: 'bg-blue-100 text-blue-900 dark:bg-blue-900/40 dark:text-blue-200',
  P3: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700/60 dark:text-zinc-200',
};

const ACTOR_ICON: Record<string, JSX.Element> = {
  system: <Bot className="size-3.5" />,
  agent: <User className="size-3.5" />,
  admin: <User className="size-3.5" />,
  customer: <MessageCircle className="size-3.5" />,
};

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
    return error.message || '请求失败';
  }
  if (error instanceof Error && error.message) return error.message;
  return '请求失败';
}

function formatDateTime(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('zh-CN', { hour12: false });
}

function relativeTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diffSec < 60) return '刚刚';
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  return `${Math.floor(diffSec / 86400)} 天前`;
}

function summarisePayload(payload: Record<string, unknown>): string {
  const keys = Object.keys(payload);
  if (keys.length === 0) return '—';
  return keys
    .map((k) => {
      const v = payload[k];
      if (v === null || v === undefined) return `${k}=`;
      if (typeof v === 'string') return `${k}=${v}`;
      if (typeof v === 'object') return `${k}=${JSON.stringify(v)}`;
      return `${k}=${String(v)}`;
    })
    .join(' · ');
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

/**
 * Admin ticket detail page. Two-column layout (mirrors inbox-detail):
 *   - left: subject, badges, metadata, transition controls, event timeline
 *   - right: linked conversation message thread
 *
 * State machine transitions are derived from `nextTicketStatuses` —
 * NO hardcoded list. CLOSED / CANCELLED render an empty controls
 * section (terminal states have no legal outbound transitions).
 */
export function AdminTicketDetailPage(): JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { id } = useParams<{ id: string }>();
  const ticketId = id ?? '';

  const ticketQuery = useQuery({
    queryKey: ticketDetailQueryKey(ticketId),
    queryFn: () => fetchTicket(ticketId),
    enabled: Boolean(ticketId),
  });

  const eventsQuery = useQuery({
    queryKey: ticketEventsQueryKey(ticketId),
    queryFn: () => fetchTicketEvents(ticketId),
    enabled: Boolean(ticketId),
  });

  const conversationId = ticketQuery.data?.conversation_id ?? '';
  const messagesQuery = useQuery({
    queryKey: messagesQueryKey(conversationId),
    queryFn: () => fetchMessages(conversationId, 100),
    enabled: Boolean(conversationId),
  });

  const [transitionError, setTransitionError] = useState<string | null>(null);

  const transitionMutation = useMutation({
    mutationFn: (toStatus: TicketStatus) =>
      transitionTicket(ticketId, toStatus, 'admin'),
    onSuccess: (updated) => {
      setTransitionError(null);
      // Patch the cached ticket in place so the UI flips without a
      // refetch round-trip, then invalidate ONLY the list cache (not
      // the detail cache we just patched) and the events list (which
      // will get a new "status_changed" row).
      queryClient.setQueryData(ticketDetailQueryKey(ticketId), updated);
      void queryClient.invalidateQueries({
        queryKey: ticketEventsQueryKey(ticketId),
      });
      // The list query key is `['admin', 'tickets', status, priority]`
      // — a sub-key under `['admin', 'tickets', ticketId]`. We use
      // `exact: false` from the `ticketsQueryKey()` shape and target
      // all sibling list queries (not the detail one we just patched).
      void queryClient.invalidateQueries({
        queryKey: ['admin', 'tickets', 'list'],
      });
      // Also re-fetch the linked conversation messages — a transition
      // may have been triggered by an agent reply that just landed.
      if (conversationId) {
        void queryClient.invalidateQueries({
          queryKey: messagesQueryKey(conversationId),
        });
      }
    },
    onError: (err) => {
      setTransitionError(extractErrorMessage(err));
    },
  });

  const handleBack = (): void => {
    navigate('/admin/tickets');
  };

  const handleRefresh = (): void => {
    void queryClient.invalidateQueries({
      queryKey: ticketDetailQueryKey(ticketId),
    });
    void queryClient.invalidateQueries({
      queryKey: ticketEventsQueryKey(ticketId),
    });
    if (conversationId) {
      void queryClient.invalidateQueries({
        queryKey: messagesQueryKey(conversationId),
      });
    }
  };

  if (ticketQuery.isError) {
    return (
      <div className="flex h-full flex-col p-6" data-testid="admin-ticket-detail-error">
        <div className="flex items-center gap-2">
          <Button type="button" size="sm" variant="outline" onClick={handleBack}>
            <ArrowLeft />
            返回列表
          </Button>
        </div>
        <div className="mt-6 flex flex-1 flex-col items-center justify-center gap-3 text-center">
          <p className="text-sm text-destructive" role="alert">
            {extractErrorMessage(ticketQuery.error)}
          </p>
          <Button type="button" size="sm" onClick={() => void ticketQuery.refetch()}>
            重试
          </Button>
        </div>
      </div>
    );
  }

  if (ticketQuery.isPending || !ticketQuery.data) {
    return (
      <div
        className="flex h-full items-center justify-center"
        data-testid="admin-ticket-detail-loading"
      >
        <p className="text-sm text-muted-foreground">加载中…</p>
      </div>
    );
  }

  const ticket = ticketQuery.data;
  const legalNext = nextTicketStatuses(ticket.status);
  const events = eventsQuery.data ?? [];

  return (
    <div
      className="flex h-full w-full flex-col overflow-hidden"
      data-testid="admin-ticket-detail-page"
    >
      <div className="flex items-center gap-2 border-b bg-background px-4 py-2">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={handleBack}
          data-testid="back-to-tickets"
        >
          <ArrowLeft />
          返回
        </Button>
        <div className="ml-auto">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={handleRefresh}
            data-testid="ticket-refresh"
          >
            <RefreshCw />
            刷新
          </Button>
        </div>
      </div>

      <div className="grid flex-1 grid-cols-1 gap-4 overflow-hidden p-4 lg:grid-cols-[1fr_360px]">
        {/* Left main column */}
        <div className="flex min-h-0 flex-col gap-4 overflow-auto">
          <Card>
            <CardHeader className="pb-3">
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${PRIORITY_BADGE[ticket.priority]}`}
                  data-testid="ticket-detail-priority"
                >
                  {PRIORITY_LABEL[ticket.priority]}
                </span>
                <span
                  className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_PILL[ticket.status]}`}
                  data-testid="ticket-detail-status"
                >
                  {STATUS_LABEL[ticket.status]}
                </span>
                {ticket.category ? (
                  <span
                    className="inline-flex items-center rounded-full border border-border bg-background px-2.5 py-0.5 text-xs"
                    data-testid="ticket-detail-category"
                  >
                    分类: {ticket.category}
                  </span>
                ) : null}
              </div>
              <CardTitle className="mt-2 text-2xl" data-testid="ticket-detail-subject">
                {ticket.subject}
              </CardTitle>
            </CardHeader>
            <CardContent className="grid grid-cols-1 gap-3 text-sm md:grid-cols-2">
              <MetadataItem label="工单 ID" value={ticket.id} mono />
              <MetadataItem label="关联会话 ID" value={ticket.conversation_id} mono />
              <MetadataItem label="SLA 策略" value={ticket.category ?? '—'} />
              <MetadataItem label="受理人" value={ticket.assignee_agent_id ?? '未分配'} />
              <MetadataItem
                label="SLA 截止"
                value={formatDateTime(ticket.sla_deadline_at)}
              />
              <MetadataItem
                label="首响时间"
                value={formatDateTime(ticket.first_response_at)}
              />
              <MetadataItem
                label="解决时间"
                value={formatDateTime(ticket.resolved_at)}
              />
              <MetadataItem
                label="关闭时间"
                value={formatDateTime(ticket.closed_at)}
              />
              <MetadataItem
                label="创建时间"
                value={formatDateTime(ticket.created_at)}
              />
              <MetadataItem
                label="更新时间"
                value={formatDateTime(ticket.updated_at)}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">状态流转</CardTitle>
            </CardHeader>
            <CardContent>
              {transitionError ? (
                <div
                  className="mb-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
                  role="alert"
                  data-testid="ticket-transition-error"
                >
                  {transitionError}
                </div>
              ) : null}
              {legalNext.length === 0 ? (
                <p
                  className="text-sm text-muted-foreground"
                  data-testid="ticket-no-transitions"
                >
                  终态工单,无可用状态流转。
                </p>
              ) : (
                <div
                  className="flex flex-wrap gap-2"
                  data-testid="ticket-transition-controls"
                >
                  {legalNext.map((next) => (
                    <Button
                      key={next}
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={transitionMutation.isPending}
                      onClick={() => transitionMutation.mutate(next)}
                      data-testid={`ticket-transition-${next}`}
                    >
                      转为 {STATUS_LABEL[next]}
                    </Button>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="flex-1">
            <CardHeader className="pb-3">
              <CardTitle className="text-base">事件时间线</CardTitle>
            </CardHeader>
            <CardContent>
              {eventsQuery.isPending ? (
                <div className="flex flex-col gap-3" data-testid="ticket-events-loading">
                  {Array.from({ length: 3 }).map((_, idx) => (
                    <div key={idx} className="flex flex-col gap-1">
                      <Skeleton className="h-4 w-32" />
                      <Skeleton className="h-3 w-3/4" />
                    </div>
                  ))}
                </div>
              ) : eventsQuery.isError ? (
                <p className="text-sm text-destructive" role="alert">
                  {extractErrorMessage(eventsQuery.error)}
                </p>
              ) : events.length === 0 ? (
                <p
                  className="text-sm text-muted-foreground"
                  data-testid="ticket-events-empty"
                >
                  暂无事件。
                </p>
              ) : (
                <ol
                  className="flex flex-col gap-3"
                  data-testid="ticket-events-list"
                >
                  {events.map((evt) => (
                    <EventRow key={evt.id} event={evt} />
                  ))}
                </ol>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Right sidebar — linked conversation messages */}
        <Card className="flex min-h-0 flex-col overflow-hidden">
          <CardHeader className="pb-3">
            <CardTitle className="text-base">关联会话</CardTitle>
            {conversationId ? (
              <p className="font-mono text-xs text-muted-foreground">
                {conversationId}
              </p>
            ) : null}
          </CardHeader>
          <CardContent className="flex min-h-0 flex-1 flex-col overflow-hidden p-0">
            <div
              className="flex min-h-0 flex-1 flex-col"
              data-testid="ticket-conversation-messages"
            >
              <MessageStream
                messages={messagesQuery.data ?? []}
                isLoading={messagesQuery.isPending}
              />
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

interface MetadataItemProps {
  label: string;
  value: string;
  mono?: boolean;
}

function MetadataItem({ label, value, mono }: MetadataItemProps): JSX.Element {
  return (
    <div className="flex flex-col gap-0.5" data-testid="ticket-metadata-item">
      <span className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span
        className={mono ? 'font-mono text-xs' : 'text-sm'}
        data-testid="ticket-metadata-value"
      >
        {value}
      </span>
    </div>
  );
}

interface EventRowProps {
  event: TicketEvent;
}

function EventRow({ event }: EventRowProps): JSX.Element {
  const icon = ACTOR_ICON[event.actor_type] ?? <CircleAlert className="size-3.5" />;
  return (
    <li
      className="flex flex-col gap-1 rounded-md border bg-card p-3 text-sm"
      data-testid={`ticket-event-row-${event.id}`}
      data-event-type={event.event_type}
    >
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1 rounded-full border bg-background px-2 py-0.5">
          {icon}
          {event.actor_type}
          {event.actor_id ? `: ${event.actor_id}` : ''}
        </span>
        <span className="rounded-full border bg-background px-2 py-0.5">
          {event.event_type}
        </span>
        <span>{relativeTime(event.created_at)}</span>
        <span className="ml-auto" title={formatDateTime(event.created_at)}>
          <CircleCheck className="size-3" />
        </span>
      </div>
      {Object.keys(event.payload).length > 0 ? (
        <p
          className="font-mono text-xs text-muted-foreground"
          data-testid="ticket-event-payload"
        >
          {summarisePayload(event.payload)}
        </p>
      ) : null}
    </li>
  );
}
