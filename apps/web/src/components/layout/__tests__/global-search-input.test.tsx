import { describe, expect, it, vi, beforeEach } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import { GlobalSearchInput } from '@/components/layout/global-search-input';

// Tier 3 Task 3.3 — global search header input.
//
// Two assertions:
//   1. Non-admins don't see the input (avoids advertising a 403
//      UX).
//   2. Admin users can type + Enter-submit to navigate to the search
//      page with the query string.

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  // eslint-disable-next-line @typescript-eslint/consistent-type-imports -- vi.importActual needs the generic
  const actual = await vi.importActual<typeof import('react-router-dom')>(
    'react-router-dom',
  );
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

const useIsAdminMock = vi.fn();
vi.mock('@/lib/use-is-admin', () => ({
  useIsAdmin: () => useIsAdminMock(),
}));

beforeEach(() => {
  cleanup();
  mockNavigate.mockReset();
  useIsAdminMock.mockReset();
});

describe('GlobalSearchInput (Tier 3 Task 3.3)', () => {
  it('renders nothing for non-admins', () => {
    useIsAdminMock.mockReturnValue({ isAdmin: false, isLoading: false });
    const { container } = render(
      <MemoryRouter>
        <GlobalSearchInput />
      </MemoryRouter>,
    );
    expect(container.firstChild).toBeNull();
  });

  it('navigates to /admin/search?q=... when submitted', async () => {
    useIsAdminMock.mockReturnValue({ isAdmin: true, isLoading: false });
    render(
      <MemoryRouter>
        <GlobalSearchInput />
      </MemoryRouter>,
    );
    const input = screen.getByTestId('global-search-input');
    await userEvent.type(input, 'password');
    await userEvent.keyboard('{Enter}');
    expect(mockNavigate).toHaveBeenCalledWith('/admin/search?q=password');
  });

  it('does not navigate on empty submit', async () => {
    useIsAdminMock.mockReturnValue({ isAdmin: true, isLoading: false });
    render(
      <MemoryRouter>
        <GlobalSearchInput />
      </MemoryRouter>,
    );
    const input = screen.getByTestId('global-search-input');
    await userEvent.type(input, '   ');
    await userEvent.keyboard('{Enter}');
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});