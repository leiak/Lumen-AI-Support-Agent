import { useState } from 'react';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

export interface KbActionsProps {
  /** Display name of the KB being deleted; surfaces in the confirm message. */
  kbName: string;
  /** Triggered when the user confirms the deletion. */
  onConfirm: () => void | Promise<void>;
  /**
   * Disables the action while the parent mutation is in flight, so the
   * dialog can't double-fire.
   */
  busy?: boolean;
}

/**
 * Delete button + confirmation dialog. Confirmation copy explicitly
 * warns the user that the cascade drops every article + vector in
 * the KB; we always require a manual click to commit.
 */
export function KbActions({
  kbName,
  onConfirm,
  busy,
}: KbActionsProps): JSX.Element {
  const [open, setOpen] = useState(false);

  const handleConfirm = async (): Promise<void> => {
    await onConfirm();
    setOpen(false);
  };

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => setOpen(true)}
        disabled={busy === true}
        data-testid="kb-delete-trigger"
      >
        删除
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent data-testid="kb-delete-dialog">
          <DialogHeader>
            <DialogTitle>确认删除知识库</DialogTitle>
            <DialogDescription>
              确认删除知识库 <span className="font-medium text-foreground">{kbName}</span> ? 这会删除所有文章和向量。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setOpen(false)}
              disabled={busy === true}
              data-testid="kb-delete-cancel"
            >
              取消
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => {
                void handleConfirm();
              }}
              disabled={busy === true}
              data-testid="kb-delete-confirm"
            >
              {busy === true ? '删除中…' : '确认删除'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
