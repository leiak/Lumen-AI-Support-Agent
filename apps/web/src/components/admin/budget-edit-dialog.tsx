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
import {
  type BudgetUpdateInput,
  type TenantBudget,
  updateBudget,
} from '@/lib/budget';

interface Props {
  tenantId: string;
  current: TenantBudget | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function BudgetEditDialog({
  tenantId,
  current,
  open,
  onOpenChange,
}: Props): JSX.Element {
  const [softWarn, setSoftWarn] = useState('10000');
  const [hardCap, setHardCap] = useState('20000');
  const [tz, setTz] = useState('UTC');
  const qc = useQueryClient();

  useEffect(() => {
    if (current !== null) {
      setSoftWarn(String(current.soft_warn_tokens ?? 10000));
      setHardCap(String(current.hard_cap_tokens ?? 20000));
      setTz(current.period_anchor_tz);
    }
  }, [current]);

  const mut = useMutation({
    mutationFn: (input: BudgetUpdateInput) => updateBudget(tenantId, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['admin', 'budget', tenantId] });
      onOpenChange(false);
    },
  });

  function handleSubmit(): void {
    const sw = Number.parseInt(softWarn, 10);
    const hc = Number.parseInt(hardCap, 10);
    if (Number.isNaN(sw) || Number.isNaN(hc)) return;
    if (hc < sw) return; // disabled at button level too
    const input: BudgetUpdateInput = {
      soft_warn_tokens: sw,
      hard_cap_tokens: hc,
      period_anchor_tz: tz.trim() || 'UTC',
    };
    mut.mutate(input);
  }

  const swNum = Number.parseInt(softWarn, 10);
  const hcNum = Number.parseInt(hardCap, 10);
  const invalid = Number.isNaN(swNum) || Number.isNaN(hcNum) || hcNum < swNum;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="budget-edit-dialog">
        <DialogHeader>
          <DialogTitle>编辑预算</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label htmlFor="budget-soft-warn">软告警阈值 (tokens)</Label>
            <Input
              id="budget-soft-warn"
              data-testid="budget-edit-soft-warn"
              type="number"
              min={1}
              value={softWarn}
              onChange={(e) => setSoftWarn(e.target.value)}
            />
          </div>
          <div>
            <Label htmlFor="budget-hard-cap">硬上限 (tokens)</Label>
            <Input
              id="budget-hard-cap"
              data-testid="budget-edit-hard-cap"
              type="number"
              min={1}
              value={hardCap}
              onChange={(e) => setHardCap(e.target.value)}
            />
            {invalid ? (
              <p
                className="mt-1 text-xs text-red-600"
                data-testid="budget-edit-error"
              >
                硬上限必须 ≥ 软告警阈值,且均为正整数
              </p>
            ) : null}
          </div>
          <div>
            <Label htmlFor="budget-tz">周期锚点时区</Label>
            <Input
              id="budget-tz"
              data-testid="budget-edit-tz"
              value={tz}
              onChange={(e) => setTz(e.target.value)}
              maxLength={64}
            />
            <p className="mt-1 text-xs text-muted-foreground">
              IANA 时区名,如 UTC / Asia/Shanghai
            </p>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            data-testid="budget-edit-submit"
            onClick={handleSubmit}
            disabled={mut.isPending || invalid}
          >
            {mut.isPending ? '保存中…' : '保存'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
