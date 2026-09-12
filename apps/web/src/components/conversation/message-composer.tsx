import { useEffect, useRef, useState } from 'react';
import { AxiosError } from 'axios';

import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { sendAgentMessage } from '@/lib/messages';

interface ApiErrorPayload {
  detail?: string;
}

function extractErrorMessage(error: unknown): string {
  if (error instanceof AxiosError) {
    const payload = error.response?.data as ApiErrorPayload | undefined;
    if (payload?.detail) return payload.detail;
    return error.message || '发送失败';
  }
  if (error instanceof Error) return error.message;
  return '发送失败';
}

export interface MessageComposerProps {
  conversationId: string;
  /**
   * Imperative handle for the parent's "apply suggestion" button —
   * the parent sets the textarea value directly through the same
   * ref so the composer can stay agnostic about suggestion state.
   */
  registerApplyHandler?: (apply: (text: string) => void) => void;
  /**
   * Triggered after a successful send so the parent can refetch
   * conversations and any dependent lists.
   */
  onSent?: () => void;
  /**
   * Optional callback for the AI-suggest button — the parent owns
   * the suggestion fetch + state.
   */
  onSuggest?: () => void;
}

/**
 * Auto-resize the textarea to fit its content. Caps at 240px so
 * very long messages don't push the composer off-screen.
 */
const MAX_TEXTAREA_HEIGHT = 240;

export function MessageComposer({
  conversationId,
  registerApplyHandler,
  onSent,
  onSuggest,
}: MessageComposerProps): JSX.Element {
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Expose an imperative handle so the AI-suggestion pane can copy
  // the suggested text into the textarea without re-rendering the
  // composer (which would steal focus).
  useEffect(() => {
    if (!registerApplyHandler) return;
    const apply = (next: string): void => {
      setText(next);
      const el = textareaRef.current;
      if (el) {
        el.focus();
        // Place cursor at end so the agent can keep typing.
        const length = next.length;
        el.setSelectionRange(length, length);
      }
    };
    registerApplyHandler(apply);
  }, [registerApplyHandler]);

  // Auto-resize on text change.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  }, [text]);

  const trimmed = text.trim();
  const canSend = trimmed.length > 0 && !sending;

  const handleSend = async (): Promise<void> => {
    if (!canSend) return;
    setSending(true);
    setError(null);
    try {
      await sendAgentMessage(conversationId, trimmed);
      setText('');
      onSent?.();
    } catch (err) {
      setError(extractErrorMessage(err));
    } finally {
      setSending(false);
    }
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    // Enter sends, Shift+Enter inserts a newline (matches Slack /
    // most chat composer conventions).
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void handleSend();
    }
  };

  return (
    <div
      className="flex flex-col gap-2 border-t bg-background p-4"
      data-testid="message-composer"
    >
      <Textarea
        ref={textareaRef}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="输入回复内容,Enter 发送,Shift+Enter 换行"
        aria-label="回复内容"
        data-testid="composer-textarea"
        rows={2}
      />
      <div className="flex items-center gap-2">
        <Button
          type="button"
          size="sm"
          onClick={handleSend}
          disabled={!canSend}
          data-testid="composer-send"
        >
          发送
        </Button>
        {onSuggest ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={onSuggest}
            data-testid="composer-suggest"
          >
            ✨ 拿AI建议
          </Button>
        ) : null}
        {error !== null ? (
          <span className="text-xs text-destructive" role="alert">
            {error}
          </span>
        ) : null}
      </div>
    </div>
  );
}
