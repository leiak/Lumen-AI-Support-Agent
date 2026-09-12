import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

import { CustomerInfoPane } from '@/components/conversation/customer-info-pane';
import type { Conversation } from '@/lib/conversations';

const baseConversation: Conversation = {
  id: '01HZX8K1M5R7N3W2Q9P0ABCDEF',
  tenant_id: 'demo',
  channel_id: '01HZX8K1M5R7N3W2Q9P0CHANXYZ',
  customer_external_id: 'cust-99',
  status: 'open',
  assigned_agent_id: 'agent-7',
  ai_handling: true,
  opened_at: '2026-09-01T10:00:00Z',
  last_activity_at: '2026-09-10T12:00:00Z',
};

afterEach(() => {
  cleanup();
});

describe('CustomerInfoPane', () => {
  it('renders channel id, customer id, and timestamps', () => {
    render(<CustomerInfoPane conversation={baseConversation} channelName="Web Chat" />);

    expect(screen.getByTestId('customer-info-pane')).toBeInTheDocument();
    expect(screen.getByTestId('channel-name')).toHaveTextContent('Web Chat');
    // The channel id and customer id are middle-truncated; assert
    // via the `title` attribute which carries the raw value.
    expect(screen.getByTestId('channel-id').getAttribute('title')).toBe(
      baseConversation.channel_id,
    );
    expect(screen.getByTestId('customer-id').getAttribute('title')).toBe(
      baseConversation.customer_external_id,
    );
    // Both timestamps render as relative-time strings — we only
    // assert they're present (the formatter is exercised in the
    // inbox tests).
    expect(screen.getByText('最后活动')).toBeInTheDocument();
    expect(screen.getByText('创建时间')).toBeInTheDocument();
  });

  it('shows the AI handling indicator with the correct enabled state', () => {
    const { rerender } = render(
      <CustomerInfoPane conversation={baseConversation} />,
    );
    let indicator = screen.getByTestId('ai-handling');
    expect(indicator.dataset.enabled).toBe('true');
    expect(indicator).toHaveTextContent('开启');

    rerender(
      <CustomerInfoPane
        conversation={{ ...baseConversation, ai_handling: false }}
      />,
    );
    indicator = screen.getByTestId('ai-handling');
    expect(indicator.dataset.enabled).toBe('false');
    expect(indicator).toHaveTextContent('关闭');
  });
});
