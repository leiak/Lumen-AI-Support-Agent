import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AxiosError } from 'axios';

import { apiClient } from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';
import type { Conversation } from '@/lib/conversations';
import {
  fetchTenantUsers,
  type TenantUser,
} from '@/lib/tenant-users';
import { Button } from '@/components/ui/button';

/**
 * Conversation action bar — claim / close / return-to-AI / reassign buttons.
 *
 * Visibility rules (verified against backend spec):
 * * **claim** — only when ``status === 'pending'`` AND
 *   ``assigned_agent_id === null``.
 * * **return-to-ai** — only when the calling agent owns the row
 *   (``assigned_agent_id === claims.sub``). Admin can always return
 *   to AI.
 * * **close** — always visible to admin; agents see it only after
 *   they own the row.
 * * **reassign** — admin-only. Opens an inline ``<select>`` populated
 *   with the tenant's active ``agent`` / ``supervisor`` users fetched
 *   from ``GET /api/v1/admin/tenants/{tid}/users``. Submitting calls
 *   ``POST /api/v1/conversations/{id}/assign`` (admin-only on the
 *   backend). We deliberately filter out ``admin``/``viewer`` roles
 *   from the dropdown because transferring to a non-frontline user
 *   defeats the point of the action.
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

const ASSIGNABLE_ROLES: ReadonlySet<TenantUser['role']> = new Set(['agent', 'supervisor']);

export function ConversationActions({
  conversation,
  onActionComplete,
}: ConversationActionsProps): JSX.Element | null {
  const { user } = useCurrentUser();
  const [pending, setPending] = useState<'claim' | 'close' | 'return-to-ai' | 'reassign' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reassignOpen, setReassignOpen] = useState(false);
  const [selectedAgentId, setSelectedAgentId] = useState<string>('');

  // Admin reassignment needs a roster. We only fetch when the
  // reassign panel is open (and the caller is admin) to avoid a
  // blanket user-list call on every detail page mount for non-admins.
  const reassignQuery = useQuery({
    queryKey: ['admin', 'tenant-users', user?.tenant_id ?? null],
    queryFn: () => fetchTenantUsers(user!.tenant_id),
    enabled: reassignOpen && user?.role === 'admin' && Boolean(user?.tenant_id),
  });
  const assignableUsers: TenantUser[] = (reassignQuery.data ?? [])
    .filter((u) => u.is_active && ASSIGNABLE_ROLES.has(u.role))
    .sort((a, b) => a.email.localeCompare(b.email));

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
  const canReassign = isAdmin;

  if (!canClaim && !canClose && !canReturnToAi && !canReassign) {
    return null;
  }

  const runAction = async (
    type: 'claim' | 'close' | 'return-to-ai' | 'reassign',
    request: () => Promise<unknown>,
  ): Promise<void> => {
    setError(null);
    setPending(type);
    try {
      await request();
      if (type === 'reassign') {
        setReassignOpen(false);
      }
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

  const handleReassign = (): Promise<void> => {
    if (!selectedAgentId) {
      setError('请选择要分配给的坐席');
      return Promise.resolve();
    }
    return runAction('reassign', () =>
      apiClient.post(`/api/v1/conversations/${conversation.id}/assign`, {
        agent_id: selectedAgentId,
      }),
    );
  };

  const handleToggleReassign = (): void => {
    setError(null);
    if (!reassignOpen) {
      // Pre-select the current assignee if there is one, so the admin
      // sees who's currently on the case before deciding to swap.
      setSelectedAgentId(conversation.assigned_agent_id ?? '');
    }
    setReassignOpen((open) => !open);
  };

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
      {canReassign ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={handleToggleReassign}
          disabled={pending !== null}
          data-testid="action-reassign-toggle"
        >
          {reassignOpen ? '取消分配' : '分配给…'}
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
      {reassignOpen && canReassign ? (
        <div className="flex items-center gap-2" data-testid="reassign-panel">
          <label htmlFor="reassign-select" className="text-xs text-muted-foreground">
            分配给
          </label>
          <select
            id="reassign-select"
            className="h-8 rounded border bg-background px-2 text-xs"
            value={selectedAgentId}
            onChange={(e) => setSelectedAgentId(e.target.value)}
            disabled={reassignQuery.isLoading}
            data-testid="reassign-select"
          >
            <option value="" disabled>
              {reassignQuery.isLoading ? '加载中…' : '请选择'}
            </option>
            {assignableUsers.map((u) => (
              <option key={u.id} value={u.id}>
                {u.full_name ? `${u.full_name} <${u.email}>` : u.email}
                {u.role === 'supervisor' ? ' (主管)' : ''}
              </option>
            ))}
          </select>
          <Button
            type="button"
            size="sm"
            onClick={handleReassign}
            disabled={pending !== null || !selectedAgentId || reassignQuery.isLoading}
            data-testid="action-reassign-confirm"
          >
            确认分配
          </Button>
        </div>
      ) : null}
      {error !== null ? (
        <span className="text-xs text-destructive" role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}
