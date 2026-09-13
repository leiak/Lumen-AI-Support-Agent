import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { ChannelRow } from '@/components/settings/channel-row';
import type { Channel } from '@/lib/settings';

const sampleChannel: Channel = {
  id: '01HZX8K1M5R7N3W2Q9P0CHANXYZ',
  type: 'feishu',
  name: '客户支持机器人',
  status: 'active',
  tenant_id: 'demo',
  created_at: '2026-09-08T10:00:00Z',
};

function renderRow(channel: Channel): void {
  render(<ChannelRow channel={channel} />);
}

describe('ChannelRow', () => {
  it('renders all channel fields', () => {
    renderRow(sampleChannel);
    const row = screen.getByTestId('channel-row');
    expect(row).toHaveAttribute('data-channel-id', sampleChannel.id);
    expect(row).toHaveAttribute('data-soft-deleted', 'false');

    expect(screen.getByText('客户支持机器人')).toBeInTheDocument();

    const typeBadge = screen.getByTestId('channel-type-badge');
    expect(typeBadge).toHaveTextContent('飞书');
    expect(typeBadge).toHaveAttribute('data-type', 'feishu');

    const statusBadge = screen.getByTestId('channel-status-badge');
    expect(statusBadge).toHaveTextContent('启用');
    expect(statusBadge).toHaveAttribute('data-status', 'active');
  });

  it('flags soft-deleted channels when status is disabled', () => {
    renderRow({ ...sampleChannel, id: '01HZX8K1M5R7N3W2Q9P0CHANXYZ', status: 'disabled' });
    const row = screen.getByTestId('channel-row');
    expect(row).toHaveAttribute('data-soft-deleted', 'true');
    expect(screen.getByTestId('channel-status-badge')).toHaveTextContent('已禁用');
    expect(screen.getByTestId('channel-status-badge')).toHaveAttribute('data-status', 'disabled');
  });
});