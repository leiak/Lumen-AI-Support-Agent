import { useState } from 'react';
import { AxiosError } from 'axios';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  approveDraft,
  rejectDraft,
  type KbDraft,
} from '@/lib/kb-drafts';
import { DraftDetailDialog } from '@/components/admin/draft-detail-dialog';

interface FetchErrorPayload {
  detail?: string;
}

function extractErrorMessage(error: unknown): string {
  if (error instanceof AxiosError) {
    const payload = error.response?.data as FetchErrorPayload | undefined;
    if (typeof payload?.detail === 'string') return payload.detail;
    return error.message;
  }
  return error instanceof Error ? error.message : '请求失败';
}

export interface DraftListProps {
  drafts: KbDraft[];
  onChange: () => void;
}

/**
 * Render the list of KB drafts as cards. Each card shows the title,
 * 200-char body preview, tags, and Approve / Reject buttons. The
 * "View" button opens the detail dialog with the full body.
 */
export function DraftList({ drafts, onChange }: DraftListProps): JSX.Element {
  const [openDraftId, setOpenDraftId] = useState<string | null>(null);
  const [busyDraftId, setBusyDraftId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleApprove = async (draftId: string): Promise<void> => {
    setBusyDraftId(draftId);
    setError(null);
    try {
      await approveDraft(draftId);
      onChange();
    } catch (e) {
      setError(extractErrorMessage(e));
    } finally {
      setBusyDraftId(null);
    }
  };

  const handleReject = async (draftId: string): Promise<void> => {
    setBusyDraftId(draftId);
    setError(null);
    try {
      await rejectDraft(draftId);
      onChange();
    } catch (e) {
      setError(extractErrorMessage(e));
    } finally {
      setBusyDraftId(null);
    }
  };

  if (drafts.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="drafts-empty">
        暂无待审核草稿。
      </p>
    );
  }

  return (
    <div className="space-y-3" data-testid="draft-list">
      {error ? (
        <div
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          role="alert"
          data-testid="draft-list-error"
        >
          {error}
        </div>
      ) : null}
      {drafts.map((draft) => (
        <Card key={draft.id} data-testid={`draft-card-${draft.id}`}>
          <CardHeader>
            <CardTitle>{draft.title}</CardTitle>
            <div className="flex flex-wrap gap-1 text-xs text-muted-foreground">
              <span>{draft.source_question_count} 个问题聚类</span>
              <span>·</span>
              <span>{new Date(draft.created_at).toLocaleString('zh-CN')}</span>
            </div>
          </CardHeader>
          <CardContent>
            <p className="line-clamp-2 text-sm text-muted-foreground">
              {draft.body_preview}
            </p>
            <div className="mt-3 flex flex-wrap gap-1">
              {draft.tags.map((tag) => (
                <span
                  key={tag}
                  className="rounded-full bg-secondary px-2 py-0.5 text-xs"
                >
                  {tag}
                </span>
              ))}
            </div>
            <div className="mt-4 flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setOpenDraftId(draft.id)}
                data-testid={`draft-view-${draft.id}`}
              >
                查看
              </Button>
              <Button
                size="sm"
                onClick={() => handleApprove(draft.id)}
                disabled={busyDraftId === draft.id}
                data-testid={`draft-approve-${draft.id}`}
              >
                批准
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={() => handleReject(draft.id)}
                disabled={busyDraftId === draft.id}
                data-testid={`draft-reject-${draft.id}`}
              >
                拒绝
              </Button>
            </div>
          </CardContent>
        </Card>
      ))}
      {openDraftId ? (
        <DraftDetailDialog
          draftId={openDraftId}
          onClose={() => setOpenDraftId(null)}
          onAction={() => {
            setOpenDraftId(null);
            onChange();
          }}
        />
      ) : null}
    </div>
  );
}