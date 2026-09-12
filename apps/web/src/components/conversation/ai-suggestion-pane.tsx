import { useEffect, useState } from 'react';
import { AxiosError } from 'axios';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { fetchSuggestion, type Suggestion } from '@/lib/suggest';

export interface AiSuggestionPaneProps {
  conversationId: string;
  /**
   * Imperative handler registered by the parent so the
   * "应用到输入框" button can route the suggested text into the
   * composer without lifting state up.
   */
  onApply: (text: string) => void;
  /**
   * Optional imperative trigger — the page passes a ref-typed
   * callback so the composer's "✨ 拿AI建议" button can fire a
   * suggestion request without lifting state up.
   */
  registerTrigger?: (trigger: () => void) => void;
}

const TURN_LABEL: Record<Suggestion['turn_kind'], string> = {
  rag_hit: 'RAG 命中',
  no_rag: '无 RAG',
  no_customer_message: '暂无客户消息',
  llm_unavailable: 'LLM 不可用',
};

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

type State =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'success'; suggestion: Suggestion }
  | { kind: 'error'; message: string };

/**
 * Right pane — read-only AI suggestion preview.
 *
 * Renders three primary states:
 * * **idle** — empty placeholder prompting the agent to click
 *   "拿AI建议".
 * * **success** — quoted suggested text + citation chips +
 *   turn_kind badge.
 * * **loading / error** — minimal status row with retry.
 *
 * The parent owns the click trigger (the "✨ 拿AI建议" button in
 * the composer); this pane only owns the result rendering.
 */
export function AiSuggestionPane({
  conversationId,
  onApply,
  registerTrigger,
}: AiSuggestionPaneProps): JSX.Element {
  const [state, setState] = useState<State>({ kind: 'idle' });

  const runSuggest = async (): Promise<void> => {
    setState({ kind: 'loading' });
    try {
      const suggestion = await fetchSuggestion(conversationId);
      setState({ kind: 'success', suggestion });
    } catch (err) {
      setState({ kind: 'error', message: extractErrorMessage(err) });
    }
  };

  useEffect(() => {
    if (!registerTrigger) return;
    registerTrigger(() => {
      void runSuggest();
    });
    // runSuggest is intentionally not a dependency — the closure
    // is captured fresh on every render and we only want to
    // re-register when the parent passes a new registerTrigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [registerTrigger]);

  return (
    <aside
      className="flex w-full flex-col gap-4 border-l bg-muted/10 p-4 md:w-80 md:shrink-0"
      data-testid="ai-suggestion-pane"
      aria-label="AI 建议回复"
    >
      <header className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-foreground">AI 建议回复</h3>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => void runSuggest()}
          disabled={state.kind === 'loading'}
          data-testid="suggestion-regenerate"
        >
          {state.kind === 'success' ? '重新生成' : '拿AI建议'}
        </Button>
      </header>
      <Separator />

      {state.kind === 'idle' ? (
        <p
          className="text-sm text-muted-foreground"
          data-testid="suggestion-empty"
        >
          暂无建议 — 点击 [✨ 拿AI建议] 拿一条。
        </p>
      ) : null}

      {state.kind === 'loading' ? (
        <div
          className="text-sm text-muted-foreground"
          data-testid="suggestion-loading"
        >
          正在生成建议…
        </div>
      ) : null}

      {state.kind === 'error' ? (
        <div className="flex flex-col gap-2" data-testid="suggestion-error">
          <p className="text-sm text-destructive" role="alert">
            {state.message}
          </p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => void runSuggest()}
          >
            重试
          </Button>
        </div>
      ) : null}

      {state.kind === 'success' ? (
        <div className="flex flex-col gap-3" data-testid="suggestion-content">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary" data-testid="turn-kind">
              {TURN_LABEL[state.suggestion.turn_kind]}
            </Badge>
            {state.suggestion.warning !== null ? (
              <Badge variant="outline" data-testid="suggestion-warning">
                {state.suggestion.warning}
              </Badge>
            ) : null}
          </div>

          <blockquote
            className="rounded-md border border-dashed bg-background p-3 text-sm text-foreground"
            data-testid="suggested-text"
          >
            {state.suggestion.suggested_text.length > 0
              ? state.suggestion.suggested_text
              : '(空)'}
          </blockquote>

          {state.suggestion.citations.length > 0 ? (
            <div className="flex flex-col gap-2">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                引用 ({state.suggestion.citations.length})
              </h4>
              <div className="flex flex-wrap gap-1.5" data-testid="suggestion-citations">
                {state.suggestion.citations.map((c, idx) => (
                  <Badge
                    key={`${c.article_id}-${c.chunk_index}-${idx}`}
                    variant="outline"
                    className="font-mono text-[10px]"
                    title={c.text}
                  >
                    article {c.article_id.slice(0, 8)} chunk {c.chunk_index}
                  </Badge>
                ))}
              </div>
            </div>
          ) : null}

          <Button
            type="button"
            size="sm"
            onClick={() => onApply(state.suggestion.suggested_text)}
            disabled={state.suggestion.suggested_text.length === 0}
            data-testid="suggestion-apply"
          >
            应用到输入框
          </Button>
        </div>
      ) : null}
    </aside>
  );
}

// useState is imported at the top — keeping the React import surface
// minimal and explicit.
