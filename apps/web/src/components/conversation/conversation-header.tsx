import { Copy } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { TooltipProvider } from '@/components/ui/tooltip';
import type { Conversation, ConversationStatus } from '@/lib/conversations';
import { ConversationActions } from '@/components/conversation/conversation-actions';
import { cn } from '@/lib/utils';

const STATUS_LABEL: Record<ConversationStatus, string> = {
  pending: '待处理',
  open: '进行中',
  closed: '已关闭',
};

const STATUS_VARIANT = {
  pending: 'pending',
  open: 'open',
  closed: 'closed',
} as const satisfies Record<ConversationStatus, 'pending' | 'open' | 'closed'>;

export interface ConversationHeaderProps {
  conversation: Conversation;
  wsStatus?: 'connecting' | 'connected' | 'reconnecting' | 'disconnected';
  onActionComplete?: () => void;
}

/**
 * Truncate a long ULID-shaped id into a readable middle-truncated
 * form. Mirrors the convention used by ``ConversationRow`` so the
 * whole app reads as one system.
 */
function truncateMiddle(value: string, maxLen = 14): string {
  if (value.length <= maxLen) return value;
  const keep = Math.ceil((maxLen - 1) / 2);
  return `${value.slice(0, keep)}…${value.slice(-keep)}`;
}

/**
 * Header strip for the conversation detail page. Surfaces the
 * truncated id + copy button, the conversation status, the current
 * assignee, the WS connection indicator, and the action bar
 * (claim / close / return-to-AI) when applicable.
 */
export function ConversationHeader({
  conversation,
  wsStatus,
  onActionComplete,
}: ConversationHeaderProps): JSX.Element {
  const assignee = conversation.assigned_agent_id ?? '未分配';
  const variant = STATUS_VARIANT[conversation.status];

  return (
    <TooltipProvider delayDuration={200}>
      <header
        className="flex flex-wrap items-center gap-3 border-b bg-background px-6 py-3"
        data-testid="conversation-header"
      >
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 gap-1 px-2 font-mono text-xs"
          data-testid="copy-conversation-id"
          onClick={() => {
            const cb = navigator.clipboard;
            if (cb && typeof cb.writeText === 'function') {
              void cb.writeText(conversation.id);
            }
          }}
          aria-label="复制完整会话 ID"
          title="复制完整会话 ID"
        >
          {truncateMiddle(conversation.id, 16)}
          <Copy className="h-3 w-3" />
        </Button>

        <Badge variant={variant} data-testid="header-status">
          {STATUS_LABEL[conversation.status]}
        </Badge>

        <span
          className="text-sm text-muted-foreground"
          data-testid="header-assignee"
          data-assigned={conversation.assigned_agent_id === null ? 'false' : 'true'}
        >
          分配人:<span className="ml-1 font-medium text-foreground">{assignee}</span>
        </span>

        {wsStatus !== undefined ? <ConnectionIndicator status={wsStatus} /> : null}

        <div className="ml-auto flex items-center gap-2">
          {onActionComplete ? (
            <ConversationActions
              conversation={conversation}
              onActionComplete={onActionComplete}
            />
          ) : (
            <ConversationActions conversation={conversation} />
          )}
        </div>
      </header>
    </TooltipProvider>
  );
}

interface ConnectionIndicatorProps {
  status: 'connecting' | 'connected' | 'reconnecting' | 'disconnected';
}

function ConnectionIndicator({ status }: ConnectionIndicatorProps): JSX.Element {
  const labelMap = {
    connecting: '连接中',
    connected: '已连接',
    reconnecting: '重连中',
    disconnected: '已断开',
  } as const satisfies Record<ConnectionIndicatorProps['status'], string>;

  const dotClass = {
    connecting: 'bg-amber-400',
    connected: 'bg-emerald-500',
    reconnecting: 'bg-amber-400',
    disconnected: 'bg-zinc-400',
  } as const satisfies Record<ConnectionIndicatorProps['status'], string>;

  return (
    <span
      className="flex items-center gap-1.5 text-xs text-muted-foreground"
      data-testid="ws-status"
      data-status={status}
    >
      <span className={cn('h-2 w-2 rounded-full', dotClass[status])} aria-hidden />
      {labelMap[status]}
    </span>
  );
}

// (no icon re-exports — keep the module surface minimal)
