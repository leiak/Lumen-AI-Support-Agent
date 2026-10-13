import { useCallback, useRef } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AxiosError } from 'axios';
import { ArrowLeft } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { AiSuggestionPane } from '@/components/conversation/ai-suggestion-pane';
import { ConversationHeader } from '@/components/conversation/conversation-header';
import { CustomerInfoPane } from '@/components/conversation/customer-info-pane';
import { MessageComposer } from '@/components/conversation/message-composer';
import { MessageStream } from '@/components/conversation/message-stream';
import { useConversationWebSocket } from '@/hooks/use-conversation-websocket';
import { fetchConversation } from '@/lib/conversations';
import { fetchMessages, messagesQueryKey } from '@/lib/messages';

interface FetchErrorPayload {
  detail?: string;
}

function extractErrorMessage(error: unknown): string {
  if (error instanceof AxiosError) {
    const payload = error.response?.data as FetchErrorPayload | undefined;
    if (payload?.detail) return payload.detail;
    return error.message || '请求失败';
  }
  if (error instanceof Error) return error.message;
  return '请求失败';
}

function conversationQueryKey(id: string): readonly unknown[] {
  return ['conversation', id] as const;
}

export function InboxDetailPage(): JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { id } = useParams<{ id: string }>();
  const conversationId = id ?? '';

  // Imperative handles — the suggestion pane writes into the
  // composer (apply), and the composer triggers the suggestion
  // pane (trigger). Keeping these as refs avoids re-rendering on
  // every keystroke or suggestion fetch.
  const applyRef = useRef<((text: string) => void) | null>(null);
  const triggerSuggestRef = useRef<(() => void) | null>(null);
  const registerApplyHandler = useCallback((apply: (text: string) => void) => {
    applyRef.current = apply;
  }, []);
  const registerSuggestTrigger = useCallback((trigger: () => void) => {
    triggerSuggestRef.current = trigger;
  }, []);

  // Direct GET /api/v1/conversations/{id} — works for both agent and
  // admin roles (the backend enforces row-level access). Replaces the
  // pre-2026-10-13 list-endpoint fan-out which coupled this query to
  // ``useIsAdmin()`` and broke whenever the cached current-user role
  // was stale (switching admin <-> agent inside the 30 s TanStack
  // staleTime window). The query key no longer encodes the role.
  const conversationQuery = useQuery({
    queryKey: conversationQueryKey(conversationId),
    queryFn: () => fetchConversation(conversationId),
    enabled: Boolean(conversationId),
  });

  const messagesQuery = useQuery({
    queryKey: messagesQueryKey(conversationId),
    queryFn: () => fetchMessages(conversationId),
    enabled: Boolean(conversationId),
  });

  const { status: wsStatus } = useConversationWebSocket(
    conversationId,
    { enabled: Boolean(conversationId) && !conversationQuery.isError },
  );

  const handleBack = (): void => {
    navigate('/inbox');
  };

  const refreshAll = useCallback((): void => {
    queryClient.invalidateQueries({ queryKey: ['conversations'] });
    queryClient.invalidateQueries({ queryKey: conversationQueryKey(conversationId) });
    queryClient.invalidateQueries({ queryKey: messagesQueryKey(conversationId) });
  }, [queryClient, conversationId]);

  const handleActionComplete = useCallback((): void => {
    refreshAll();
  }, [refreshAll]);

  const handleSent = useCallback((): void => {
    // Send → invalidates messages; the server also broadcasts
    // `message.created` so the WS hook will invalidate again. The
    // double-invalidate is harmless (TanStack dedupes).
    refreshAll();
  }, [refreshAll]);

  const handleApplySuggestion = useCallback((text: string) => {
    applyRef.current?.(text);
  }, []);

  const handleSuggest = useCallback((): void => {
    triggerSuggestRef.current?.();
  }, []);

  if (conversationQuery.isError || (!conversationQuery.isLoading && !conversationQuery.data)) {
    return (
      <div className="flex h-full flex-col p-6" data-testid="inbox-detail-error">
        <div className="flex items-center gap-2">
          <Button type="button" size="sm" variant="outline" onClick={handleBack}>
            <ArrowLeft />
            返回收件箱
          </Button>
        </div>
        <div className="mt-6 flex flex-1 flex-col items-center justify-center gap-3 text-center">
          <p className="text-sm text-destructive" role="alert">
            {extractErrorMessage(conversationQuery.error ?? new Error('会话不存在'))}
          </p>
          <Button type="button" size="sm" onClick={() => void conversationQuery.refetch()}>
            重试
          </Button>
        </div>
      </div>
    );
  }

  if (conversationQuery.isLoading || !conversationQuery.data) {
    return (
      <div
        className="flex h-full items-center justify-center"
        data-testid="inbox-detail-loading"
      >
        <p className="text-sm text-muted-foreground">加载中…</p>
      </div>
    );
  }

  const conversation = conversationQuery.data;

  return (
    <div className="flex h-full w-full flex-col overflow-hidden" data-testid="inbox-detail">
      <div className="flex items-center gap-2 border-b bg-background px-4 py-2">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={handleBack}
          data-testid="back-to-inbox"
        >
          <ArrowLeft />
          返回
        </Button>
      </div>

      <ConversationHeader
        conversation={conversation}
        wsStatus={wsStatus}
        onActionComplete={handleActionComplete}
      />

      <div className="flex flex-1 overflow-hidden">
        <CustomerInfoPane conversation={conversation} />

        <div className="flex flex-1 flex-col overflow-hidden">
          <MessageStream
            messages={messagesQuery.data ?? []}
            isLoading={messagesQuery.isLoading}
          />
          <MessageComposer
            conversationId={conversationId}
            registerApplyHandler={registerApplyHandler}
            onSent={handleSent}
            onSuggest={handleSuggest}
          />
        </div>

        <AiSuggestionPane
          conversationId={conversationId}
          onApply={handleApplySuggestion}
          registerTrigger={registerSuggestTrigger}
        />
      </div>
    </div>
  );
}
