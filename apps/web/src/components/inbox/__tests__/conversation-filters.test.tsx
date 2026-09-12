import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ConversationFilters } from '@/components/inbox/conversation-filters';
import { DEFAULT_FILTERS, type ConversationFilters as Filters } from '@/lib/conversations';

function StatefulHarness({
  initial,
  onChange,
}: {
  initial: Filters;
  onChange: (next: Filters) => void;
}): JSX.Element {
  const [filters, setFilters] = useState<Filters>(initial);
  return (
    <ConversationFilters
      filters={filters}
      onChange={(next) => {
        setFilters(next);
        onChange(next);
      }}
    />
  );
}

function renderFilters(
  initial: Filters = DEFAULT_FILTERS,
  onChange = vi.fn(),
): { onChange: ReturnType<typeof vi.fn> } {
  render(<StatefulHarness initial={initial} onChange={onChange} />);
  return { onChange };
}

describe('ConversationFilters', () => {
  it('renders all status options', () => {
    renderFilters();
    expect(screen.getByTestId('status-option-all')).toHaveTextContent('全部');
    expect(screen.getByTestId('status-option-pending')).toHaveTextContent('待处理');
    expect(screen.getByTestId('status-option-open')).toHaveTextContent('进行中');
    expect(screen.getByTestId('status-option-closed')).toHaveTextContent('已关闭');
    // The active option must be marked via aria-checked so screen readers
    // announce its state.
    expect(screen.getByTestId('status-option-all')).toHaveAttribute('aria-checked', 'true');
  });

  it('propagates filter updates on status change', async () => {
    const user = userEvent.setup();
    const { onChange } = renderFilters();
    await user.click(screen.getByTestId('status-option-pending'));
    expect(onChange).toHaveBeenCalledWith({ ...DEFAULT_FILTERS, status: 'pending' });
  });

  it('propagates filter updates on search input', async () => {
    const user = userEvent.setup();
    const { onChange } = renderFilters();
    const search = screen.getByTestId('conversation-search');
    await user.type(search, 'abc');
    // Each keystroke fires onChange once — keep this loose so we don't
    // pin to the exact UI binding (input vs change events).
    expect(onChange).toHaveBeenCalled();
    const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1]?.[0] as Filters;
    expect(lastCall.search).toBe('abc');
  });
});