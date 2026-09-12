import { Separator } from '@/components/ui/separator';
import type { Conversation } from '@/lib/conversations';

export interface CustomerInfoPaneProps {
  conversation: Conversation;
  /** Optional human-readable channel name (e.g. "Web Chat"). */
  channelName?: string | null;
}

/**
 * Coarse relative-time formatter — kept inline to avoid pulling in
 * dayjs/date-fns for one pane. Matches the style used by
 * ``ConversationRow``.
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

function truncateMiddle(value: string, maxLen = 16): string {
  if (value.length <= maxLen) return value;
  const keep = Math.ceil((maxLen - 1) / 2);
  return `${value.slice(0, keep)}…${value.slice(-keep)}`;
}

/**
 * Left pane — static conversation metadata (channel / customer id /
 * AI handling flag / activity timestamps). Renders nothing when no
 * conversation is loaded yet; the parent shows a skeleton instead.
 */
export function CustomerInfoPane({
  conversation,
  channelName,
}: CustomerInfoPaneProps): JSX.Element {
  return (
    <aside
      className="flex w-full flex-col gap-4 border-r bg-muted/10 p-4 md:w-72 md:shrink-0"
      data-testid="customer-info-pane"
      aria-label="客户信息"
    >
      <section className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          渠道
        </h3>
        <div className="space-y-1">
          <p className="text-sm font-medium text-foreground" data-testid="channel-name">
            {channelName ?? '未命名渠道'}
          </p>
          <p
            className="font-mono text-xs text-muted-foreground"
            data-testid="channel-id"
            title={conversation.channel_id}
          >
            {truncateMiddle(conversation.channel_id, 16)}
          </p>
        </div>
      </section>

      <Separator />

      <section className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          客户
        </h3>
        <p
          className="font-mono text-sm text-foreground"
          data-testid="customer-id"
          title={conversation.customer_external_id}
        >
          {truncateMiddle(conversation.customer_external_id, 16)}
        </p>
      </section>

      <Separator />

      <section className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          AI 处理
        </h3>
        <div
          className="flex items-center gap-2 text-sm"
          data-testid="ai-handling"
          data-enabled={conversation.ai_handling ? 'true' : 'false'}
        >
          <span
            className={
              conversation.ai_handling
                ? 'h-2 w-2 rounded-full bg-emerald-500'
                : 'h-2 w-2 rounded-full bg-zinc-400'
            }
            aria-hidden
          />
          <span className="text-foreground">
            {conversation.ai_handling ? '开启' : '关闭'}
          </span>
        </div>
      </section>

      <Separator />

      <section className="space-y-2 text-sm">
        <Row label="最后活动">
          {formatRelativeTime(conversation.last_activity_at)}
        </Row>
        <Row label="创建时间">
          {formatRelativeTime(conversation.opened_at)}
        </Row>
      </section>
    </aside>
  );
}

interface RowProps {
  label: string;
  children: React.ReactNode;
}

function Row({ label, children }: RowProps): JSX.Element {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className="text-foreground">{children}</span>
    </div>
  );
}
