import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

import { MessageStream } from '@/components/conversation/message-stream';
import type { Message } from '@/lib/messages';

function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: `01HZX8K1M5R7N3W2Q9P${(Math.random() * 1e6).toFixed(0).padStart(6, '0')}`,
    conversation_id: '01HZX8K1M5R7N3W2Q9P0CONVABCD',
    role: 'customer',
    content_text: 'Hello',
    sender_id: null,
    created_at: '2026-09-10T10:00:00Z',
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

describe('MessageStream', () => {
  it('renders messages with role badges', () => {
    const messages: Message[] = [
      makeMessage({ role: 'customer', content_text: 'Hi' }),
      makeMessage({ role: 'agent', content_text: 'Hello, how can I help?' }),
      makeMessage({ role: 'ai', content_text: 'Auto-reply text' }),
    ];
    render(<MessageStream messages={messages} isLoading={false} />);
    expect(screen.getAllByTestId('message-row')).toHaveLength(3);
    expect(screen.getAllByTestId('role-badge')[0]).toHaveTextContent('客户');
    expect(screen.getAllByTestId('role-badge')[1]).toHaveTextContent('坐席');
    expect(screen.getAllByTestId('role-badge')[2]).toHaveTextContent('AI');
  });

  it('groups messages by day with separator headers', () => {
    // Use today / yesterday / day-before so the test is stable
    // regardless of when it runs (the formatter maps today → "今天").
    const today = new Date();
    const yesterday = new Date(today.getTime() - 86_400_000);
    const dayBefore = new Date(today.getTime() - 2 * 86_400_000);
    const messages: Message[] = [
      makeMessage({ created_at: dayBefore.toISOString(), content_text: 'Old' }),
      makeMessage({ created_at: yesterday.toISOString(), content_text: 'Yesterday' }),
      makeMessage({ created_at: today.toISOString(), content_text: 'Today' }),
    ];
    render(<MessageStream messages={messages} isLoading={false} />);
    expect(screen.getAllByTestId('day-separator')).toHaveLength(3);
    expect(screen.getByText('今天')).toBeInTheDocument();
    expect(screen.getByText('昨天')).toBeInTheDocument();
  });

  it('renders loading skeletons when isLoading is true', () => {
    render(<MessageStream messages={[]} isLoading />);
    expect(screen.getByTestId('message-stream-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('message-stream')).not.toBeInTheDocument();
  });

  it('renders the empty state when there are no messages', () => {
    render(<MessageStream messages={[]} isLoading={false} />);
    expect(screen.getByTestId('message-stream-empty')).toHaveTextContent('暂无消息');
  });
});
