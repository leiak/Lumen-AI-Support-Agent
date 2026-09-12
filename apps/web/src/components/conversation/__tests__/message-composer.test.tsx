import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AxiosError } from 'axios';

import { MessageComposer } from '@/components/conversation/message-composer';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import type * as ApiClient from '@/lib/api-client';

vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof ApiClient>('@/lib/api-client');
  return {
    ...actual,
    apiClient: {
      post: vi.fn(),
      get: vi.fn(),
    },
  };
});

const CONVERSATION_ID = '01HZX8K1M5R7N3W2Q9P0CONVABCD';

beforeEach(() => {
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  vi.mocked(apiClient.post).mockReset();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('MessageComposer', () => {
  it('disables the send button when the textarea is empty', async () => {
    render(<MessageComposer conversationId={CONVERSATION_ID} />);
    expect(screen.getByTestId('composer-send')).toBeDisabled();

    // Whitespace-only input also keeps the button disabled.
    const user = userEvent.setup();
    const textarea = screen.getByTestId('composer-textarea');
    await user.type(textarea, '   ');
    expect(screen.getByTestId('composer-send')).toBeDisabled();
  });

  it('calls sendAgentMessage when the user submits non-empty text', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({
      data: {
        id: '01HZX8K1M5R7N3W2Q9P0MSG00001',
        conversation_id: CONVERSATION_ID,
        role: 'agent',
        content_text: 'reply',
        sender_id: 'u_agent',
        created_at: '2026-09-10T12:00:00Z',
      },
    });
    const onSent = vi.fn();
    render(
      <MessageComposer conversationId={CONVERSATION_ID} onSent={onSent} />,
    );
    const user = userEvent.setup();
    const textarea = screen.getByTestId('composer-textarea');
    await user.type(textarea, 'reply');
    await user.click(screen.getByTestId('composer-send'));

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledWith(
        `/api/v1/conversations/${CONVERSATION_ID}/messages`,
        { content_text: 'reply' },
      );
    });
    expect(onSent).toHaveBeenCalled();
  });

  it('triggers the suggest callback when the AI-suggest button is clicked', async () => {
    const onSuggest = vi.fn();
    render(
      <MessageComposer
        conversationId={CONVERSATION_ID}
        onSuggest={onSuggest}
      />,
    );
    const user = userEvent.setup();
    await user.click(screen.getByTestId('composer-suggest'));
    expect(onSuggest).toHaveBeenCalledTimes(1);
  });

  it('surfaces an error message when the API rejects', async () => {
    const axiosError = new AxiosError('send failed');
    axiosError.response = {
      status: 500,
      data: { detail: 'internal error' },
      statusText: 'Internal Server Error',
      headers: {},
      config: {} as never,
    };
    vi.mocked(apiClient.post).mockRejectedValueOnce(axiosError);
    render(<MessageComposer conversationId={CONVERSATION_ID} />);
    const user = userEvent.setup();
    const textarea = screen.getByTestId('composer-textarea');
    await user.type(textarea, 'reply');
    await user.click(screen.getByTestId('composer-send'));
    expect(await screen.findByRole('alert')).toHaveTextContent('internal error');
  });
});
