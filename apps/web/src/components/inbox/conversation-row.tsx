import { useNavigate } from 'react-router-dom';

import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import type { Conversation } from '@/lib/conversations';

export interface ConversationRowProps {
  conversation: Conversation;
  /** Optional assignee email to display when the conversation has an agent id. */
  assignedAgentEmail?: string | null;
}

/**
 * Format an ISO timestamp as a coarse relative-time Chinese string.
 *
 * Intentionally simple — no dayjs/date-fns dependency for M1. The format
 * matches what the design calls for ("2 分钟前") and degrades gracefully
 * for older timestamps ("3 天前", then falls back to the raw ISO date).
 */
function formatRelativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso);
  const diffMs = now.getTime() - then.getTime();
  if (Number.isNaN(diffMs)) return iso;
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  return then.toISOString().slice(0, 10);
}

const STATUS_LABEL: Record<string, string> = {
  pending: '待处理',
  open: '进行中',
  closed: '已关闭',
};

const STATUS_BADGE_VARIANT = {
  pending: 'pending',
  open: 'open',
  closed: 'closed',
} as const;

function truncateMiddle(value: string, maxLen = 12): string {
  if (value.length <= maxLen) return value;
  const keep = Math.ceil((maxLen - 1) / 2);
  return `${value.slice(0, keep)}…${value.slice(-keep)}`;
}

/**
 * One row in the conversation list table. Renders all five M1 columns
 * and navigates to the conversation detail page on click.
 *
 * `assignedAgentEmail` is OPTIONAL — when not provided we render `未分配`
 * for conversations with `assigned_agent_id === null` and the raw id
 * otherwise. The inbox page (Stage 9.4) doesn't yet resolve agent ids to
 * emails; that's a Stage 9.5+ concern.
 */
export function ConversationRow({
  conversation,
  assignedAgentEmail,
}: ConversationRowProps): JSX.Element {
  const navigate = useNavigate();
  const handleClick = (): void => {
    navigate(`/inbox/${conversation.id}`);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      handleClick();
    }
  };

  const assignee = conversation.assigned_agent_id
    ? (assignedAgentEmail ?? conversation.assigned_agent_id)
    : '未分配';

  const statusVariant =
    STATUS_BADGE_VARIANT[conversation.status as keyof typeof STATUS_BADGE_VARIANT] ??
    'secondary';
  const statusLabel =
    STATUS_LABEL[conversation.status] ?? conversation.status;

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={handleClick}
      onKeyDown={handleKeyDown}
      data-testid="conversation-row"
      data-conversation-id={conversation.id}
      className={cn(
        'grid cursor-pointer grid-cols-[140px_120px_100px_1fr_140px_140px] items-center gap-3 border-b px-4 py-3 text-sm transition-colors',
        'hover:bg-accent/50 focus-visible:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
      )}
    >
      <span
        className="font-mono text-xs text-muted-foreground"
        title={conversation.id}
      >
        {truncateMiddle(conversation.id, 12)}
      </span>
      <span
        className="font-mono text-xs text-muted-foreground"
        title={conversation.channel_id}
      >
        {truncateMiddle(conversation.channel_id, 12)}
      </span>
      <span>
        <Badge variant={statusVariant} data-testid="conversation-status">
          {statusLabel}
        </Badge>
      </span>
      <span className="truncate text-foreground" title={assignee}>
        {assignee}
      </span>
      <span className="text-xs text-muted-foreground">
        {formatRelativeTime(conversation.last_activity_at)}
      </span>
      <span
        className="truncate font-mono text-xs text-muted-foreground"
        title={conversation.customer_external_id}
      >
        {truncateMiddle(conversation.customer_external_id, 16)}
      </span>
    </div>
  );
}