import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AxiosError } from 'axios';

import { AiSuggestionPane } from '@/components/conversation/ai-suggestion-pane';
import type { Suggestion } from '@/lib/suggest';

const CONVERSATION_ID = '01HZX8K1M5R7N3W2Q9P0CONVABCD';

const sampleSuggestion: Suggestion = {
  conversation_id: CONVERSATION_ID,
  suggested_text: '这是 AI 生成的回复草稿。',
  citations: [
    { article_id: '01ARTCL00000000000000000AA', chunk_index: 0, text: 'chunk 0', score: 0.91 },
    { article_id: '01ARTCL00000000000000000BB', chunk_index: 2, text: 'chunk 2', score: 0.78 },
  ],
  retrieval_score_max: 0.91,
  warning: null,
  turn_kind: 'rag_hit',
};

function SuggestionHarness({
  onApply = vi.fn(),
}: {
  onApply?: (text: string) => void;
}): JSX.Element {
  return (
    <AiSuggestionPane conversationId={CONVERSATION_ID} onApply={onApply} />
  );
}

afterEach(() => {
  cleanup();
});

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('AiSuggestionPane', () => {
  it('shows the initial empty state with the trigger prompt', () => {
    render(<SuggestionHarness />);
    expect(screen.getByTestId('suggestion-empty')).toHaveTextContent('暂无建议');
  });

  it('renders the suggested text, citations, and turn_kind after a successful fetch', async () => {
    // Mock fetchSuggestion by spying on apiClient.post (the
    // suggest-reply endpoint uses POST).
    const apiClientModule = await import('@/lib/api-client');
    vi.spyOn(apiClientModule.apiClient, 'post').mockResolvedValue({
      data: sampleSuggestion,
    });

    const user = userEvent.setup();
    render(<SuggestionHarness />);
    await user.click(screen.getByTestId('suggestion-regenerate'));

    expect(await screen.findByTestId('suggestion-content')).toBeInTheDocument();
    expect(screen.getByTestId('suggested-text')).toHaveTextContent('这是 AI 生成的回复草稿。');
    expect(screen.getByTestId('turn-kind')).toHaveTextContent('RAG 命中');
    const citations = screen.getByTestId('suggestion-citations');
    expect(citations.children).toHaveLength(2);
    expect(citations.textContent).toMatch(/article 01ARTCL/);
  });

  it('shows the turn_kind badge for llm_unavailable with a warning', async () => {
    const apiClientModule = await import('@/lib/api-client');
    vi.spyOn(apiClientModule.apiClient, 'post').mockResolvedValue({
      data: {
        ...sampleSuggestion,
        turn_kind: 'llm_unavailable',
        suggested_text: '兜底回复',
        warning: 'llm_unavailable',
      },
    });

    const user = userEvent.setup();
    render(<SuggestionHarness />);
    await user.click(screen.getByTestId('suggestion-regenerate'));

    expect(await screen.findByTestId('turn-kind')).toHaveTextContent('LLM 不可用');
    expect(screen.getByTestId('suggestion-warning')).toHaveTextContent('llm_unavailable');
  });

  it('invokes the onApply callback when "应用到输入框" is clicked', async () => {
    const apiClientModule = await import('@/lib/api-client');
    vi.spyOn(apiClientModule.apiClient, 'post').mockResolvedValue({
      data: sampleSuggestion,
    });
    const onApply = vi.fn();
    const user = userEvent.setup();
    render(<SuggestionHarness onApply={onApply} />);
    await user.click(screen.getByTestId('suggestion-regenerate'));

    const applyButton = await screen.findByTestId('suggestion-apply');
    await user.click(applyButton);
    expect(onApply).toHaveBeenCalledWith(sampleSuggestion.suggested_text);
  });

  it('shows an error state with a retry button on failure', async () => {
    const apiClientModule = await import('@/lib/api-client');
    const axiosError = new AxiosError('service unavailable');
    axiosError.response = {
      status: 503,
      data: { detail: 'service unavailable' },
      statusText: 'Service Unavailable',
      headers: {},
      config: {} as never,
    };
    vi.spyOn(apiClientModule.apiClient, 'post').mockRejectedValueOnce(axiosError);
    const user = userEvent.setup();
    render(<SuggestionHarness />);
    await user.click(screen.getByTestId('suggestion-regenerate'));

    expect(await screen.findByTestId('suggestion-error')).toHaveTextContent('service unavailable');
    // Clicking retry triggers a second request.
    vi.spyOn(apiClientModule.apiClient, 'post').mockResolvedValueOnce({
      data: sampleSuggestion,
    });
    const retryButton = screen.getByRole('button', { name: '重试' });
    await user.click(retryButton);
    await waitFor(() =>
      expect(screen.queryByTestId('suggestion-error')).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId('suggestion-content')).toBeInTheDocument();
  });
});
