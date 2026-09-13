import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { ChannelListCard } from '@/components/settings/channel-list-card';
import type { Channel } from '@/lib/settings';

const sampleFeishu: Channel = {
  id: '01HZX8K1M5R7N3W2Q9P0CHANFEISHU',
  type: 'feishu',
  name: '飞书支持',
  status: 'active',
  tenant_id: 'demo',
  created_at: '2026-09-08T10:00:00Z',
};

const sampleWidget: Channel = {
  id: '01HZX8K1M5R7N3W2Q9P0CHANWIDGET',
  type: 'web',
  name: '网站嵌入',
  status: 'active',
  tenant_id: 'demo',
  created_at: '2026-09-07T10:00:00Z',
};

const sampleEmail: Channel = {
  id: '01HZX8K1M5R7N3W2Q9P0CHANEMAIL',
  type: 'email',
  name: '邮件支持',
  status: 'active',
  tenant_id: 'demo',
  created_at: '2026-09-06T10:00:00Z',
};

// Used only for the badge-variant test below — synthesised because the
// backend enum does not currently include 'other', but the row component
// must still render a sensible fallback for unknown types.
const sampleOther = {
  ...sampleFeishu,
  id: '01HZX8K1M5R7N3W2Q9P0CHANOTHER',
  type: 'other' as unknown as Channel['type'],
  name: '未来渠道',
} satisfies Channel;

describe('ChannelListCard', () => {
  it('renders one row per channel', () => {
    render(
      <ChannelListCard
        channels={[sampleFeishu, sampleWidget, sampleEmail]}
      />,
    );
    expect(screen.getByTestId('channel-list')).toBeInTheDocument();
    expect(screen.getAllByTestId('channel-row')).toHaveLength(3);
    expect(screen.getByText('飞书支持')).toBeInTheDocument();
    expect(screen.getByText('网站嵌入')).toBeInTheDocument();
    expect(screen.getByText('邮件支持')).toBeInTheDocument();
  });

  it('renders loading and empty states', () => {
    const { rerender } = render(<ChannelListCard channels={[]} isLoading />);
    expect(screen.getByTestId('channel-list-loading')).toBeInTheDocument();

    rerender(<ChannelListCard channels={[]} />);
    expect(screen.getByTestId('channel-list-empty')).toBeInTheDocument();
    expect(screen.getByTestId('channel-list-empty')).toHaveTextContent('暂无渠道');
  });

  it('shows a 类型 badge per channel type', () => {
    render(
      <ChannelListCard
        channels={[sampleFeishu, sampleWidget, sampleEmail, sampleOther]}
      />,
    );
    const typeBadges = screen.getAllByTestId('channel-type-badge');
    expect(typeBadges).toHaveLength(4);
    const dataTypes = typeBadges.map((b) => b.getAttribute('data-type'));
    expect(dataTypes).toEqual(['feishu', 'web', 'email', 'other']);

    // Each badge carries the human-readable label.
    expect(typeBadges[0]).toHaveTextContent('飞书');
    expect(typeBadges[1]).toHaveTextContent('Web Widget');
    expect(typeBadges[2]).toHaveTextContent('邮件');
    // The 'other' branch is a forward-compatible fallback (the M1 backend
    // enum does not include it) — render some neutral label rather than
    // the raw type string.
    expect(typeBadges[3]).not.toHaveTextContent('other');
  });
});