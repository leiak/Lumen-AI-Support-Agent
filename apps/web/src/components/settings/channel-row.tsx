import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import type { Channel } from '@/lib/settings';
import { CHANNEL_STATUS_LABEL, CHANNEL_TYPE_LABEL } from '@/lib/settings';

export interface ChannelRowProps {
  channel: Channel;
  onEdit?: ((channel: Channel) => void) | undefined;
  onDelete?: ((channel: Channel) => void) | undefined;
}

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

function truncateMiddle(value: string, maxLen = 12): string {
  if (value.length <= maxLen) return value;
  const keep = Math.ceil((maxLen - 1) / 2);
  return `${value.slice(0, keep)}…${value.slice(-keep)}`;
}

/**
 * One row per channel. Action buttons (edit + disable) are wired via
 * props so the parent page owns the mutation flow + dialog state.
 * Soft-delete is a window.confirm() to keep the dependency surface
 * small (no AlertDialog component in shadcn/ui install).
 */
export function ChannelRow({ channel, onEdit, onDelete }: ChannelRowProps): JSX.Element {
  const isSoftDeleted = channel.status === 'disabled';
  const typeLabel =
    CHANNEL_TYPE_LABEL[channel.type as keyof typeof CHANNEL_TYPE_LABEL] ?? '其他';
  const statusLabel = CHANNEL_STATUS_LABEL[channel.status];
  const statusVariant = isSoftDeleted ? 'secondary' : 'open';

  return (
    <div
      role="row"
      data-testid="channel-row"
      data-channel-id={channel.id}
      data-soft-deleted={isSoftDeleted ? 'true' : 'false'}
      className="grid grid-cols-[minmax(0,1fr)_120px_100px_140px_120px] items-center gap-3 border-b px-4 py-3 text-sm"
    >
      <span className="truncate text-foreground" title={channel.name}>
        {channel.name}
      </span>
      <span>
        <Badge
          variant="outline"
          data-testid="channel-type-badge"
          data-type={channel.type}
        >
          {typeLabel}
        </Badge>
      </span>
      <span>
        <Badge
          variant={statusVariant}
          data-testid="channel-status-badge"
          data-status={channel.status}
        >
          {statusLabel}
        </Badge>
      </span>
      <span className="text-xs text-muted-foreground">
        <span
          className="font-mono"
          title={channel.created_at}
          data-testid="channel-created-at"
        >
          {truncateMiddle(channel.id, 8)}
        </span>
        {' · '}
        {formatRelativeTime(channel.created_at)}
      </span>
      <span className="flex gap-1">
        <Button
          variant="ghost"
          size="sm"
          data-testid={`channel-row-edit-${channel.id}`}
          onClick={() => onEdit?.(channel)}
        >
          编辑
        </Button>
        {isSoftDeleted ? null : (
          <Button
            variant="ghost"
            size="sm"
            data-testid={`channel-row-delete-${channel.id}`}
            onClick={() => onDelete?.(channel)}
          >
            禁用
          </Button>
        )}
      </span>
    </div>
  );
}
