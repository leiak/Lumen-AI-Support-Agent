import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { Pagination } from '@/components/inbox/pagination';

describe('Pagination', () => {
  it('disables previous on first page', () => {
    render(<Pagination page={1} pageCount={3} onPageChange={vi.fn()} />);
    expect(screen.getByTestId('pagination-prev')).toBeDisabled();
    expect(screen.getByTestId('pagination-next')).not.toBeDisabled();
  });

  it('calls onPageChange on next click', async () => {
    const user = userEvent.setup();
    const onPageChange = vi.fn();
    render(<Pagination page={2} pageCount={3} onPageChange={onPageChange} />);
    await user.click(screen.getByTestId('pagination-next'));
    expect(onPageChange).toHaveBeenCalledWith(3);
  });

  it('disables next on the last page', () => {
    render(<Pagination page={3} pageCount={3} onPageChange={vi.fn()} />);
    expect(screen.getByTestId('pagination-next')).toBeDisabled();
    expect(screen.getByTestId('pagination-prev')).not.toBeDisabled();
  });
});