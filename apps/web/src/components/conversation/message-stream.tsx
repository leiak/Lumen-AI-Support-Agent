import { useEffect, useMemo, useRef } from 'react';

import { Skeleton } from '@/components/ui/skeleton';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';
import type { Message, MessageRole } from '@/lib/messages';

const ROLE_LABEL: Record<MessageRole, string> = {
  customer: '客户',
  agent: '坐席',
  ai: 'AI',
  system: '系统',
  tool: '工具',
};

/**
 * Each role gets a distinct hue, kept neutral (light + dark mode
 * safe) to match the shadcn badge-variants convention used by the
 * conversation status badges.
 */
const ROLE_VARIANT: Record<MessageRole, string> = {
  customer: 'bg-blue-100 text-blue-900 dark:bg-blue-900/40 dark:text-blue-200',
  agent: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200',
  ai: 'bg-violet-100 text-violet-900 dark:bg-violet-900/40 dark:text-violet-200',
  system: 'bg-zinc-200 text-zinc-700 dark:bg-zinc-700/60 dark:text-zinc-200',
  tool: 'bg-orange-100 text-orange-900 dark:bg-orange-900/40 dark:text-orange-200',
};

const ROLE_ALIGN: Record<MessageRole, 'left' | 'right'> = {
  customer: 'left',
  agent: 'right',
  ai: 'left',
  system: 'left',
  tool: 'left',
};

export interface MessageStreamProps {
  messages: Message[];
  isLoading: boolean;
}

/**
 * Group messages by calendar day so the conversation reads as
 * discrete sessions rather than a single rolling timeline.
 */
function groupByDay(messages: Message[]): Array<{ day: string; items: Message[] }> {
  const groups: Array<{ day: string; items: Message[] }> = [];
  for (const m of messages) {
    const day = m.created_at.slice(0, 10);
    const last = groups[groups.length - 1];
    if (last && last.day === day) {
      last.items.push(m);
    } else {
      groups.push({ day, items: [m] });
    }
  }
  return groups;
}

function formatDay(day: string, now: Date = new Date()): string {
  const today = now.toISOString().slice(0, 10);
  const yesterday = new Date(now.getTime() - 86_400_000).toISOString().slice(0, 10);
  if (day === today) return '今天';
  if (day === yesterday) return '昨天';
  return day;
}

function formatTime(iso: string): string {
  // Render local HH:MM — keeps the row tidy without forcing a
  // dayjs dependency. The tooltip carries the full ISO timestamp.
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

export function MessageStream({ messages, isLoading }: MessageStreamProps): JSX.Element {
  const scrollRef = useRef<HTMLDivElement>(null);
  const groups = useMemo(() => groupByDay(messages), [messages]);

  // Auto-scroll to the bottom when new messages arrive. We only
  // trigger when the message count changes so users who scroll up
  // to read history aren't yanked back down.
  const lastCountRef = useRef(messages.length);
  useEffect(() => {
    if (messages.length !== lastCountRef.current) {
      lastCountRef.current = messages.length;
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }
  }, [messages.length]);

  if (isLoading) {
    return (
      <div
        className="flex flex-1 flex-col gap-3 overflow-auto p-4"
        data-testid="message-stream-loading"
      >
        {Array.from({ length: 4 }).map((_, idx) => (
          <div key={idx} className="flex flex-col gap-2">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="h-10 w-2/3" />
          </div>
        ))}
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <div
        className="flex flex-1 items-center justify-center p-6 text-sm text-muted-foreground"
        data-testid="message-stream-empty"
      >
        暂无消息
      </div>
    );
  }

  return (
    <TooltipProvider delayDuration={150}>
      <div
        ref={scrollRef}
        className="flex-1 overflow-auto p-4"
        data-testid="message-stream"
        data-message-count={messages.length}
      >
        {groups.map((group) => (
          <section key={group.day} className="mb-6">
            <div
              className="mb-3 flex items-center justify-center"
              data-testid="day-separator"
            >
              <span className="rounded-full bg-muted px-3 py-0.5 text-xs text-muted-foreground">
                {formatDay(group.day)}
              </span>
            </div>
            <div className="flex flex-col gap-3">
              {group.items.map((msg) => (
                <MessageRow key={msg.id} message={msg} />
              ))}
            </div>
          </section>
        ))}
      </div>
    </TooltipProvider>
  );
}

interface MessageRowProps {
  message: Message;
}

function MessageRow({ message }: MessageRowProps): JSX.Element {
  const align = ROLE_ALIGN[message.role];
  const isAgent = message.role === 'agent';

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div
          className={cn(
            'flex max-w-[80%] flex-col gap-1 rounded-lg border bg-card p-3 text-sm shadow-sm',
            align === 'right' ? 'self-end' : 'self-start',
            isAgent ? 'border-emerald-200 dark:border-emerald-900' : 'border-border',
          )}
          data-testid="message-row"
          data-role={message.role}
        >
          <div className="flex items-center gap-2">
            <span
              className={cn(
                'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium',
                ROLE_VARIANT[message.role],
              )}
              data-testid="role-badge"
            >
              {ROLE_LABEL[message.role]}
            </span>
            <span className="text-xs text-muted-foreground">{formatTime(message.created_at)}</span>
          </div>
          <p className="whitespace-pre-wrap break-words text-foreground">
            {message.content_text}
          </p>
        </div>
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-0.5">
          <p>{new Date(message.created_at).toLocaleString()}</p>
          <p className="font-mono text-[10px] text-muted-foreground">{message.id}</p>
        </div>
      </TooltipContent>
    </Tooltip>
  );
}
