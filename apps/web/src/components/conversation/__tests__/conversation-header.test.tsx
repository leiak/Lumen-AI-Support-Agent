import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ConversationHeader } from '@/components/conversation/conversation-header';
import { JWT_STORAGE_KEY } from '@/lib/api-client';
import type { Conversation } from '@/lib/conversations';
import type { UseCurrentUserResult } from '@/lib/use-current-user';

vi.mock('@/lib/use-current-user', () => ({
  useCurrentUser: vi.fn(),
}));

import { useCurrentUser } from '@/lib/use-current-user';

const baseConversation: Conversation = {
  id: '01HZX8K1M5R7N3W2Q9P0ABCDEF',
  tenant_id: 'demo',
  channel_id: '01HZX8K1M5R7N3W2Q9P0CHANXYZ',
  customer_external_id: 'cust-99',
  status: 'open',
  assigned_agent_id: null,
  ai_handling: true,
  opened_at: '2026-09-01T10:00:00Z',
  last_activity_at: '2026-09-10T12:00:00Z',
};

function mockUser(overrides: Partial<UseCurrentUserResult> = {}): void {
  vi.mocked(useCurrentUser).mockReturnValue({
    user: {
      user_id: 'u_agent_1',
      email: 'agent@example.com',
      tenant_id: 'demo',
      tenant_name: 'Demo',
      role: 'agent',
    },
    isLoading: false,
    isError: false,
    ...overrides,
  });
}

function renderHeader(
  conversation: Conversation = baseConversation,
  props: Partial<React.ComponentProps<typeof ConversationHeader>> = {},
): void {
  render(
    <ConversationHeader
      conversation={conversation}
      onActionComplete={vi.fn()}
      {...props}
    />,
  );
}

beforeEach(() => {
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  vi.mocked(useCurrentUser).mockReset();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('ConversationHeader', () => {
  it('renders the conversation id (truncated), status badge, and assignee', () => {
    mockUser();
    renderHeader({ ...baseConversation, assigned_agent_id: 'agent-7' });
    expect(screen.getByTestId('conversation-header')).toBeInTheDocument();
    expect(screen.getByTestId('header-status')).toHaveTextContent('进行中');
    expect(screen.getByTestId('header-assignee')).toHaveTextContent('agent-7');
  });

  it('shows 未分配 when assigned_agent_id is null', () => {
    mockUser();
    renderHeader({ ...baseConversation, assigned_agent_id: null });
    expect(screen.getByTestId('header-assignee')).toHaveTextContent('未分配');
  });

  it('shows the 认领 button when status is pending AND assigned_agent_id is null', () => {
    mockUser();
    renderHeader({
      ...baseConversation,
      status: 'pending',
      assigned_agent_id: null,
    });
    expect(screen.getByTestId('action-claim')).toBeInTheDocument();
  });

  it('hides the 认领 button when the conversation is already assigned', () => {
    mockUser();
    renderHeader({
      ...baseConversation,
      status: 'pending',
      assigned_agent_id: 'u_agent_1',
    });
    expect(screen.queryByTestId('action-claim')).not.toBeInTheDocument();
  });

  it('hides the 认领 button when status is open (only pending is claimable)', () => {
    mockUser();
    renderHeader({ ...baseConversation, status: 'open', assigned_agent_id: null });
    expect(screen.queryByTestId('action-claim')).not.toBeInTheDocument();
  });

  it('renders the WS connection indicator when wsStatus is provided', () => {
    mockUser();
    renderHeader(baseConversation, { wsStatus: 'connected' });
    const indicator = screen.getByTestId('ws-status');
    expect(indicator).toBeInTheDocument();
    expect(indicator.dataset.status).toBe('connected');
  });

  it('exposes a copy button for the conversation id', async () => {
    mockUser();
    renderHeader(baseConversation);
    // The copy button is rendered with a `title` attribute so the
    // user can hover for the affordance text. Clicking it triggers
    // navigator.clipboard.writeText — we don't assert on the
    // clipboard call here because jsdom 25's navigator.clipboard
    // getter doesn't honour Object.defineProperty overrides. The
    // browser-side behaviour is exercised in the e2e suite.
    const button = screen.getByTestId('copy-conversation-id');
    expect(button).toHaveAttribute('title', '复制完整会话 ID');
    expect(button).toHaveAttribute('aria-label', '复制完整会话 ID');
    const user = userEvent.setup();
    await user.click(button);
  });
});
