import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search } from 'lucide-react';

import { Input } from '@/components/ui/input';
import { useIsAdmin } from '@/lib/use-is-admin';

/**
 * Header-mounted global search box — Tier 3 Task 3.3.
 *
 * Only visible to admins (search hits KB / tickets / conversations,
 * all admin-scoped). Submits by hitting Enter — the operator is
 * taken to ``/admin/search?q=...`` where the results live.
 *
 * Non-admins see no search affordance (the backend would 403
 * anyway, and we don't want to advertise a broken input).
 */
export function GlobalSearchInput(): JSX.Element | null {
  const { isAdmin } = useIsAdmin();
  const navigate = useNavigate();
  const [value, setValue] = useState('');

  if (!isAdmin) return null;

  const submit = (e: React.FormEvent<HTMLFormElement>): void => {
    e.preventDefault();
    const q = value.trim();
    if (q.length === 0) return;
    navigate(`/admin/search?q=${encodeURIComponent(q)}`);
  };

  return (
    <form
      onSubmit={submit}
      className="relative w-64"
      data-testid="global-search-form"
    >
      <Search
        aria-hidden
        className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
      />
      <Input
        type="search"
        data-testid="global-search-input"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="全局搜索…"
        maxLength={100}
        className="h-8 pl-7 text-xs"
      />
    </form>
  );
}