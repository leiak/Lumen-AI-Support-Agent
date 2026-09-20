import { describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { render, screen } from '@testing-library/react';

import { AdminGuard } from '@/components/auth/admin-guard';

vi.mock('@/lib/use-is-admin', () => ({
  useIsAdmin: vi.fn(),
}));

import { useIsAdmin } from '@/lib/use-is-admin';
const mockedUseIsAdmin = vi.mocked(useIsAdmin);

function renderAt(path: string): void {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/login" element={<div>login</div>} />
        <Route path="/inbox" element={<div>inbox</div>} />
        <Route path="/admin" element={<AdminGuard><div>protected</div></AdminGuard>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('AdminGuard', () => {
  it('renders children when user is admin', () => {
    mockedUseIsAdmin.mockReturnValue({ isAdmin: true, isLoading: false });
    renderAt('/admin');
    expect(screen.getByText('protected')).toBeInTheDocument();
  });

  it('renders children when user is owner', () => {
    mockedUseIsAdmin.mockReturnValue({ isAdmin: true, isLoading: false });
    renderAt('/admin');
    expect(screen.getByText('protected')).toBeInTheDocument();
  });

  it('redirects to /inbox when user is agent', () => {
    mockedUseIsAdmin.mockReturnValue({ isAdmin: false, isLoading: false });
    renderAt('/admin');
    expect(screen.getByText('inbox')).toBeInTheDocument();
    expect(screen.queryByText('protected')).not.toBeInTheDocument();
  });

  it('renders spinner while loading', () => {
    mockedUseIsAdmin.mockReturnValue({ isAdmin: false, isLoading: true });
    const { container } = render(
      <MemoryRouter>
        <AdminGuard><div>protected</div></AdminGuard>
      </MemoryRouter>,
    );
    expect(container.querySelector('.animate-spin')).toBeTruthy();
    expect(screen.queryByText('protected')).not.toBeInTheDocument();
  });
});