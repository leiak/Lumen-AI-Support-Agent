import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { TenantInfoCard } from '@/components/settings/tenant-info-card';
import type { TenantOut } from '@/lib/settings';

const sampleTenant: TenantOut = {
  id: '01HZX8K1M5R7N3W2Q9P0ABCDEF',
  name: 'Demo Tenant',
  plan: 'free',
  status: 'active',
  created_at: '2026-09-01T10:00:00Z',
};

describe('TenantInfoCard', () => {
  it('renders all tenant fields', () => {
    render(<TenantInfoCard tenant={sampleTenant} />);
    const card = screen.getByTestId('tenant-info-card');
    expect(card).toBeInTheDocument();

    // Truncated id surfaces in the visible label; the tooltip carries the full id.
    expect(screen.getByTestId('tenant-id')).toHaveTextContent('01HZX8K1');
    expect(screen.getByTestId('tenant-name')).toHaveTextContent('Demo Tenant');
    expect(screen.getByTestId('tenant-created-at')).toHaveTextContent('2026-09-01');
    expect(screen.getByTestId('tenant-plan')).toHaveTextContent('基础版');
    expect(screen.getByTestId('tenant-plan').querySelector('[data-plan]')).toHaveAttribute(
      'data-plan',
      'free',
    );
  });

  it('shows 加载中 skeleton when tenant is loading', () => {
    render(<TenantInfoCard tenant={null} />);
    expect(screen.getByTestId('tenant-info-card-loading')).toBeInTheDocument();
    expect(screen.getByTestId('tenant-info-skeleton')).toBeInTheDocument();
    expect(screen.queryByTestId('tenant-info-card')).not.toBeInTheDocument();
  });

  it('prefers displayName override over tenant.name', () => {
    render(<TenantInfoCard tenant={sampleTenant} displayName="友好租户" />);
    expect(screen.getByTestId('tenant-name')).toHaveTextContent('友好租户');
  });

  it('renders a copy button next to the tenant id', async () => {
    const user = userEvent.setup();
    // jsdom may not implement clipboard — stub the minimum surface so the
    // click handler doesn't throw.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });

    render(<TenantInfoCard tenant={sampleTenant} />);
    await user.click(screen.getByTestId('tenant-id-copy'));
    expect(writeText).toHaveBeenCalledWith(sampleTenant.id);
  });
});