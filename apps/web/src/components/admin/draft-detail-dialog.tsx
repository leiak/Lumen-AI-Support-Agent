import { useEffect, useState } from 'react';
import { AxiosError } from 'axios';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  approveDraft,
  fetchDraft,
  rejectDraft,
  type KbDraftDetail,
} from '@/lib/kb-drafts';

export interface DraftDetailDialogProps {
  draftId: string;
  onClose: () => void;
  onAction: () => void;
}

/**
 * Modal dialog showing the full draft body (no 200-char truncation)
 * plus Approve / Reject buttons. Lazy-loads the detail on open.
 */
export function DraftDetailDialog({
  draftId,
  onClose,
  onAction,
}: DraftDetailDialogProps): JSX.Element {
  const [draft, setDraft] = useState<KbDraftDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchDraft(draftId)
      .then((d) => {
        if (!cancelled) setDraft(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          const detail =
            (e instanceof AxiosError &&
              (e.response?.data as { detail?: string } | undefined)?.detail) ||
            '加载草稿失败';
          setError(detail);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [draftId]);

  const handleApprove = async (): Promise<void> => {
    setBusy(true);
    try {
      await approveDraft(draftId);
      onAction();
    } catch (e) {
      const detail =
        (e instanceof AxiosError &&
          (e.response?.data as { detail?: string } | undefined)?.detail) ||
        '批准失败';
      setError(detail);
    } finally {
      setBusy(false);
    }
  };

  const handleReject = async (): Promise<void> => {
    setBusy(true);
    try {
      await rejectDraft(draftId);
      onAction();
    } catch (e) {
      const detail =
        (e instanceof AxiosError &&
          (e.response?.data as { detail?: string } | undefined)?.detail) ||
        '拒绝失败';
      setError(detail);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        className="max-w-2xl"
        data-testid={`draft-detail-dialog-${draftId}`}
      >
        <DialogHeader>
          <DialogTitle>{draft?.title ?? '加载中…'}</DialogTitle>
          {draft ? (
            <DialogDescription>
              {draft.source_question_count} 个问题聚类 ·{' '}
              {new Date(draft.created_at).toLocaleString('zh-CN')}
            </DialogDescription>
          ) : null}
        </DialogHeader>

        {error ? (
          <div
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            role="alert"
          >
            {error}
          </div>
        ) : null}

        <div className="max-h-96 overflow-auto rounded-md border bg-muted/30 p-4 text-sm">
          {draft ? (
            <pre className="whitespace-pre-wrap font-sans">{draft.body}</pre>
          ) : (
            <p className="text-muted-foreground">加载中…</p>
          )}
        </div>

        <div className="flex flex-wrap gap-1">
          {draft?.tags.map((tag) => (
            <span
              key={tag}
              className="rounded-full bg-secondary px-2 py-0.5 text-xs"
            >
              {tag}
            </span>
          ))}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            关闭
          </Button>
          <Button
            variant="destructive"
            onClick={handleReject}
            disabled={busy || !draft || draft.status !== 'DRAFT'}
          >
            拒绝
          </Button>
          <Button
            onClick={handleApprove}
            disabled={busy || !draft || draft.status !== 'DRAFT'}
          >
            批准
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}