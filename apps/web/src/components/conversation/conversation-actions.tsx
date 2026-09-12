import { useState } from 'react';
import { AxiosError } from 'axios';

import { apiClient } from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';
import type { Conversation } from '@/lib/conversations';
import { Button } from '@/components/ui/button';

/**
 * Conversation action bar — claim / close / return-to-AI buttons.
 *
 * Visibility rules (verified against backend spec):
 * * **claim** — only when ``status === 'pending'`` AND
 *   ``assigned_agent_id === null``.
 * * **return-to-ai** — only when the calling agent owns the row
 *   (``assigned_agent_id === claims.sub``). Admin can always return
 *   to AI.
 * * **close** — always visible to admin; agents see it only after
 *   they own the row.
 *
 * On every successful action we invalidate the conversation + its
 * messages query so dependent panes refetch. The parent (``InboxDetailPage``)
 * additionally invalidates ``['conversations']`` so the inbox list
 * reflects the new state.
 */
export interface ConversationActionsProps {
  conversation: Conversation;
  onActionComplete?: () => void;
}

interface ApiErrorPayload {
  detail?: string;
}

function extractErrorMessage(error: unknown): string {
  if (error instanceof AxiosError) {
    const payload = error.response?.data as ApiErrorPayload | undefined;
    if (payload?.detail) return payload.detail;
    return error.message || '操作失败';
  }
  if (error instanceof Error) return error.message;
  return '操作失败';
}

export function ConversationActions({
  conversation,
  onActionComplete,
}: ConversationActionsProps): JSX.Element | null {
  const { user } = useCurrentUser();
  const [pending, setPending] = useState<'claim' | 'close' | 'return-to-ai' | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!user) return null;

  const role = user.role;
  const isAdmin = role === 'admin' || role === 'owner';
  const isOwningAgent =
    conversation.assigned_agent_id !== null &&
    conversation.assigned_agent_id === user.user_id;

  const canClaim =
    conversation.status === 'pending' && conversation.assigned_agent_id === null;
  const canClose = isAdmin || isOwningAgent;
  const canReturnToAi = isAdmin || isOwningAgent;

  if (!canClaim && !canClose && !canReturnToAi) {
    return null;
  }

  const runAction = async (
    type: 'claim' | 'close' | 'return-to-ai',
    request: () => Promise<unknown>,
  ): Promise<void> => {
    setError(null);
    setPending(type);
    try {
      await request();
      onActionComplete?.();
    } catch (err) {
      setError(extractErrorMessage(err));
    } finally {
      setPending(null);
    }
  };

  const handleClaim = (): Promise<void> =>
    runAction('claim', () =>
      apiClient.post(`/api/v1/agents/conversations/${conversation.id}/claim`),
    );

  const handleClose = (): Promise<void> =>
    runAction('close', () =>
      apiClient.post(`/api/v1/conversations/${conversation.id}/close`),
    );

  const handleReturnToAi = (): Promise<void> =>
    runAction('return-to-ai', () =>
      apiClient.post(`/api/v1/conversations/${conversation.id}/return-to-ai`),
    );

  return (
    <div className="flex items-center gap-2" data-testid="conversation-actions">
      {canClaim ? (
        <Button
          type="button"
          size="sm"
          onClick={handleClaim}
          disabled={pending !== null}
          data-testid="action-claim"
        >
          认领
        </Button>
      ) : null}
      {canReturnToAi ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={handleReturnToAi}
          disabled={pending !== null}
          data-testid="action-return-to-ai"
        >
          返回 AI
        </Button>
      ) : null}
      {canClose ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={handleClose}
          disabled={pending !== null}
          data-testid="action-close"
        >
          关闭
        </Button>
      ) : null}
      {error !== null ? (
        <span className="text-xs text-destructive" role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}
