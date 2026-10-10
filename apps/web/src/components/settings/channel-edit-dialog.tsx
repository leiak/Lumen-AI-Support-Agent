import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { updateChannel, type ChannelUpdateInput } from '@/lib/channels';
import { type Channel } from '@/lib/settings';

interface Props {
  channel: Channel | null;
  onOpenChange: (open: boolean) => void;
}

export function ChannelEditDialog({ channel, onOpenChange }: Props): JSX.Element {
  const [name, setName] = useState('');
  const [status, setStatus] = useState<'active' | 'disabled'>('active');
  const qc = useQueryClient();

  useEffect(() => {
    if (channel !== null) {
      setName(channel.name);
      setStatus(channel.status);
    }
  }, [channel]);

  const mut = useMutation({
    mutationFn: (patch: ChannelUpdateInput) => {
      if (channel === null) throw new Error('no channel');
      return updateChannel(channel.id, patch);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['settings', 'channels'] });
      onOpenChange(false);
    },
  });

  function handleSubmit(): void {
    if (channel === null) return;
    const patch: ChannelUpdateInput = { name: name.trim(), status };
    mut.mutate(patch);
  }

  if (channel === null) return <></>;
  return (
    <Dialog open={channel !== null} onOpenChange={onOpenChange}>
      <DialogContent data-testid="channel-edit-dialog">
        <DialogHeader>
          <DialogTitle>编辑渠道 — {channel.name}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label htmlFor="ch-edit-name">名称</Label>
            <Input
              id="ch-edit-name"
              data-testid="channel-edit-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={200}
            />
          </div>
          <div>
            <Label htmlFor="ch-edit-status">状态</Label>
            <select
              id="ch-edit-status"
              data-testid="channel-edit-status"
              className="mt-1 w-full rounded border px-2 py-1.5 text-sm"
              value={status}
              onChange={(e) => setStatus(e.target.value as 'active' | 'disabled')}
            >
              <option value="active">启用</option>
              <option value="disabled">已禁用</option>
            </select>
          </div>
          <p className="text-xs text-muted-foreground">
            凭证字段不可编辑 — 如需轮换,删除后重新创建。
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            data-testid="channel-edit-submit"
            onClick={handleSubmit}
            disabled={mut.isPending || name.trim().length === 0}
          >
            {mut.isPending ? '保存中…' : '保存'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
