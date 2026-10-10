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
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  type CreditGrantInput,
  grantCredit,
} from '@/lib/budget';

interface Props {
  tenantId: string;
  period: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function CreditGrantDialog({
  tenantId,
  period,
  open,
  onOpenChange,
}: Props): JSX.Element {
  const [tokens, setTokens] = useState('5000');
  const [note, setNote] = useState('');
  const qc = useQueryClient();

  const mut = useMutation({
    mutationFn: (input: CreditGrantInput) => grantCredit(tenantId, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['admin', 'budget', tenantId] });
      onOpenChange(false);
      setTokens('5000');
      setNote('');
    },
  });

  function handleSubmit(): void {
    const t = Number.parseInt(tokens, 10);
    if (Number.isNaN(t) || t <= 0) return;
    if (note.trim().length === 0) return;
    const input: CreditGrantInput = { tokens: t, note: note.trim(), period };
    mut.mutate(input);
  }

  const tNum = Number.parseInt(tokens, 10);
  const invalid = Number.isNaN(tNum) || tNum <= 0 || note.trim().length === 0;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="credit-grant-dialog">
        <DialogHeader>
          <DialogTitle>发放 Credit</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <p className="text-xs text-muted-foreground">
            周期: <code className="font-mono">{period}</code>
            <br />
            仅 super-admin 可发放。普通租户管理员调用会被后端 404 拒绝(防枚举)。
          </p>
          <div>
            <Label htmlFor="credit-tokens">tokens</Label>
            <Input
              id="credit-tokens"
              data-testid="credit-grant-tokens"
              type="number"
              min={1}
              value={tokens}
              onChange={(e) => setTokens(e.target.value)}
            />
          </div>
          <div>
            <Label htmlFor="credit-note">原因</Label>
            <Input
              id="credit-note"
              data-testid="credit-grant-note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={500}
              placeholder="例如:补偿 Q3 outage 期间的额外用量"
            />
          </div>
          {invalid ? (
            <p
              className="text-xs text-red-600"
              data-testid="credit-grant-error"
            >
              tokens 必须是正整数,note 必填
            </p>
          ) : null}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            data-testid="credit-grant-submit"
            onClick={handleSubmit}
            disabled={mut.isPending || invalid}
          >
            {mut.isPending ? '发放中…' : '发放'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
