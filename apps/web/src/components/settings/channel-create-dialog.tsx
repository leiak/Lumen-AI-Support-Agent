import { useState } from 'react';

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
import { Textarea } from '@/components/ui/textarea';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createChannel, type ChannelCreateInput } from '@/lib/channels';
import { type ChannelType } from '@/lib/settings';

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const TYPE_OPTIONS: { value: ChannelType; label: string; hint: string }[] = [
  { value: 'web', label: 'Web Widget', hint: '嵌入网站聊天小部件' },
  { value: 'feishu', label: '飞书', hint: '飞书机器人消息通道' },
  { value: 'email', label: '邮件', hint: '邮件收件地址' },
];

export function ChannelCreateDialog({ open, onOpenChange }: Props): JSX.Element {
  const [type, setType] = useState<ChannelType>('web');
  const [name, setName] = useState('');
  const [credsJson, setCredsJson] = useState('{}');
  const [credsError, setCredsError] = useState<string | null>(null);
  const qc = useQueryClient();

  const mut = useMutation({
    mutationFn: createChannel,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['settings', 'channels'] });
      onOpenChange(false);
      setName('');
      setCredsJson('{}');
      setCredsError(null);
    },
  });

  function handleSubmit(): void {
    let creds: Record<string, unknown>;
    try {
      creds = JSON.parse(credsJson) as Record<string, unknown>;
    } catch {
      setCredsError('凭证必须是合法 JSON');
      return;
    }
    setCredsError(null);
    const input: ChannelCreateInput = { type, name: name.trim(), credentials: creds };
    mut.mutate(input);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="channel-create-dialog">
        <DialogHeader>
          <DialogTitle>新建渠道</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label htmlFor="ch-type">类型</Label>
            <select
              id="ch-type"
              data-testid="channel-create-type"
              className="mt-1 w-full rounded border px-2 py-1.5 text-sm"
              value={type}
              onChange={(e) => setType(e.target.value as ChannelType)}
            >
              {TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <p className="mt-1 text-xs text-muted-foreground">
              {TYPE_OPTIONS.find((o) => o.value === type)?.hint}
            </p>
          </div>
          <div>
            <Label htmlFor="ch-name">名称</Label>
            <Input
              id="ch-name"
              data-testid="channel-create-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={200}
            />
          </div>
          <div>
            <Label htmlFor="ch-creds">凭证 (JSON)</Label>
            <Textarea
              id="ch-creds"
              data-testid="channel-create-creds"
              value={credsJson}
              onChange={(e) => setCredsJson(e.target.value)}
              rows={4}
              className="font-mono text-xs"
            />
            <p className="mt-1 text-xs text-muted-foreground">
              飞书: <code>{'{ "app_id": "...", "app_secret": "..." }'}</code>
              <br />
              邮件: <code>{'{ "address": "support@yourdomain.com" }'}</code>
            </p>
            {credsError === null ? null : (
              <p className="mt-1 text-xs text-red-600" data-testid="channel-create-creds-error">
                {credsError}
              </p>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            data-testid="channel-create-submit"
            onClick={handleSubmit}
            disabled={mut.isPending || name.trim().length === 0}
          >
            {mut.isPending ? '创建中…' : '创建'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
