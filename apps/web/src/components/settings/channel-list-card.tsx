import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { ChannelRow } from '@/components/settings/channel-row';
import type { Channel } from '@/lib/settings';

export interface ChannelListCardProps {
  channels: Channel[];
  isLoading?: boolean | undefined;
  onCreate?: (() => void) | undefined;
  onEdit?: ((channel: Channel) => void) | undefined;
  onDelete?: ((channel: Channel) => void) | undefined;
}

const EMPTY_MESSAGE = '暂无渠道 — 在右下角"新建渠道"中创建 Web Widget / 飞书 / 邮件。';

export function ChannelListCard({
  channels,
  isLoading,
  onCreate,
  onEdit,
  onDelete,
}: ChannelListCardProps): JSX.Element {
  return (
    <Card data-testid="channel-list-card">
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="text-lg">渠道 (Channels)</CardTitle>
        <Button
          size="sm"
          data-testid="channel-create-button"
          onClick={() => onCreate?.()}
        >
          + 新建渠道
        </Button>
      </CardHeader>
      <CardContent className="p-0">
        {isLoading === true ? (
          <div className="flex flex-col" data-testid="channel-list-loading">
            {Array.from({ length: 3 }).map((_, idx) => (
              <div
                key={idx}
                className="grid grid-cols-[minmax(0,1fr)_120px_100px_140px_120px] items-center gap-3 border-b px-4 py-3"
              >
                <Skeleton className="h-4 w-2/3" />
                <Skeleton className="h-5 w-16 rounded-full" />
                <Skeleton className="h-5 w-16 rounded-full" />
                <Skeleton className="h-4 w-24" />
                <Skeleton className="h-6 w-16" />
              </div>
            ))}
          </div>
        ) : channels.length === 0 ? (
          <div
            className="flex flex-col items-center justify-center gap-2 p-6 text-center text-sm text-muted-foreground"
            data-testid="channel-list-empty"
          >
            <p className="text-base text-foreground">暂无渠道</p>
            <p>{EMPTY_MESSAGE}</p>
          </div>
        ) : (
          <div className="flex flex-col" data-testid="channel-list">
            <div
              role="row"
              className="grid grid-cols-[minmax(0,1fr)_120px_100px_140px_120px] items-center gap-3 border-b bg-muted/30 px-4 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground"
            >
              <span>渠道名称</span>
              <span>类型</span>
              <span>状态</span>
              <span>创建时间</span>
              <span>操作</span>
            </div>
            {channels.map((channel) => (
              <ChannelRow
                key={channel.id}
                channel={channel}
                onEdit={onEdit}
                onDelete={onDelete}
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
