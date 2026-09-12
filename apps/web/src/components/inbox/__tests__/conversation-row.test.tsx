import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useNavigate } from 'react-router-dom';

import { ConversationRow } from '@/components/inbox/conversation-row';
import type { Conversation } from '@/lib/conversations';

// Stub the navigate implementation so we can assert it was invoked.
import type * as ReactRouterDom from 'react-router-dom';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof ReactRouterDom>('react-router-dom');
  return {
    ...actual,
    useNavigate: vi.fn(),
  };
});

const baseConversation: Conversation = {
  id: '01HZX8K1M5R7N3W2Q9P0ABCDEF',
  tenant_id: 'demo',
  channel_id: '01HZX8K1M5R7N3W2Q9P0CHANXYZ',
  customer_external_id: 'cust-99',
  status: 'open',
  assigned_agent_id: 'agent-7',
  ai_handling: false,
  opened_at: '2026-09-01T10:00:00Z',
  last_activity_at: '2026-09-10T12:00:00Z',
};

function renderRow(props: Partial<React.ComponentProps<typeof ConversationRow>> = {}): {
  navigate: ReturnType<typeof useNavigate>;
} {
  const navigateMock = vi.fn();
  vi.mocked(useNavigate).mockReturnValue(navigateMock);
  render(
    <MemoryRouter>
      <ConversationRow conversation={baseConversation} {...props} />
    </MemoryRouter>,
  );
  return { navigate: navigateMock };
}

describe('ConversationRow', () => {
  it('renders all fields correctly', () => {
    renderRow({ assignedAgentEmail: 'agent@example.com' });
    const row = screen.getByTestId('conversation-row');

    expect(row).toHaveAttribute('data-conversation-id', baseConversation.id);
    // Truncated id appears in the first column — assert via the
    // accessible `title` attribute which carries the full value.
    expect(row.querySelector('span[title]')).toHaveAttribute(
      'title',
      baseConversation.id,
    );
    // Status badge label is in Chinese.
    expect(screen.getByTestId('conversation-status')).toHaveTextContent('进行中');
    // Assignee email wins over the raw id when supplied.
    expect(row).toHaveTextContent('agent@example.com');
    // Customer column surfaces the opaque external id.
    expect(row).toHaveTextContent('cust-99');
  });

  it('calls navigate on click', async () => {
    const user = userEvent.setup();
    const { navigate } = renderRow();
    await user.click(screen.getByTestId('conversation-row'));
    expect(navigate).toHaveBeenCalledWith(`/inbox/${baseConversation.id}`);
  });

  it('shows 未分配 when assigned_agent_id is null', () => {
    renderRow({
      conversation: { ...baseConversation, assigned_agent_id: null },
    });
    expect(screen.getByTestId('conversation-row')).toHaveTextContent('未分配');
  });
});