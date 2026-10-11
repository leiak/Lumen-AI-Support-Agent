import { useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import {
  BookOpen,
  Loader2,
  MessageSquare,
  Search as SearchIcon,
  Ticket,
} from 'lucide-react';
import { useQuery } from '@tanstack/react-query';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import {
  adminSearch,
  hrefForHit,
  type SearchKind,
  type SearchResult,
} from '@/lib/search';

// Cross-domain admin search page — Tier 3 Task 3.3.
//
// Backend:
//   GET /api/v1/admin/search?q=...&limit=10
//
// Renders grouped hits (KB / tickets / conversations) with deep
// links into the relevant admin or agent page. The query string is
// the source of truth so a hit is shareable / refreshable.
//
// PII: only opaque IDs + tenant-owned customer_external_id leak to
// the UI. We deliberately do NOT render raw message bodies.

const SEARCH_QUERY_KEY = ['admin', 'search'] as const;
const KIND_ICON: Record<SearchKind, JSX.Element> = {
  article: <BookOpen className="h-4 w-4" />,
  ticket: <Ticket className="h-4 w-4" />,
  conversation: <MessageSquare className="h-4 w-4" />,
};

const KIND_LABEL: Record<SearchKind, string> = {
  article: '文章',
  ticket: '工单',
  conversation: '对话',
};

interface GroupSectionProps {
  kind: SearchKind;
  hits: readonly SearchResult[];
}

function GroupSection({ kind, hits }: GroupSectionProps): JSX.Element | null {
  if (hits.length === 0) return null;
  return (
    <section
      data-testid={`search-group-${kind}`}
      className="space-y-2"
    >
      <header className="flex items-center gap-2 text-sm font-semibold text-muted-foreground">
        {KIND_ICON[kind]}
        <span>{KIND_LABEL[kind]}</span>
        <Badge variant="secondary">{hits.length}</Badge>
      </header>
      <ul className="space-y-2">
        {hits.map((hit) => {
          const href = hrefForHit(hit);
          const isLink = href !== null;
          const body = (
            <div className="space-y-1">
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-medium">{hit.title}</span>
                {hit.subtitle && (
                  <span className="text-xs text-muted-foreground">
                    {hit.subtitle}
                  </span>
                )}
              </div>
              {hit.snippet && (
                <p className="text-xs text-muted-foreground line-clamp-2">
                  {hit.snippet}
                </p>
              )}
            </div>
          );
          return (
            <li key={`${kind}-${hit.id}`} data-testid={`search-hit-${hit.id}`}>
              {isLink ? (
                <Link
                  to={href}
                  className="block rounded-md border bg-background p-3 transition-colors hover:bg-accent"
                >
                  {body}
                </Link>
              ) : (
                <div className="rounded-md border bg-background p-3">
                  {body}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function AdminSearchPage(): JSX.Element {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const initialQuery = searchParams.get('q') ?? '';
  const [draft, setDraft] = useState(initialQuery);
  const activeQuery = initialQuery.trim();

  const trimmedDraft = draft.trim();
  const query = useQuery({
    queryKey: [...SEARCH_QUERY_KEY, activeQuery] as const,
    queryFn: () => adminSearch(activeQuery, 10),
    enabled: activeQuery.length > 0,
    staleTime: 30_000,
  });

  const grouped = useMemo(() => {
    const out: Record<SearchKind, SearchResult[]> = {
      article: [],
      ticket: [],
      conversation: [],
    };
    for (const r of query.data?.results ?? []) {
      out[r.kind].push(r);
    }
    return out;
  }, [query.data]);

  const submit = (e: React.FormEvent<HTMLFormElement>): void => {
    e.preventDefault();
    const next = trimmedDraft;
    if (next.length === 0) return;
    navigate(`/admin/search?q=${encodeURIComponent(next)}`);
  };

  const hasResults = query.data && query.data.results.length > 0;
  const isEmpty = activeQuery.length > 0 && !query.isLoading && !hasResults;

  return (
    <div className="space-y-6 p-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <SearchIcon className="h-5 w-5" />
            全局搜索
          </CardTitle>
        </CardHeader>
        <CardContent>
          <form
            onSubmit={submit}
            className="flex items-center gap-2"
            data-testid="search-form"
          >
            <Input
              data-testid="search-input"
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="搜索文章、工单或对话…"
              maxLength={100}
            />
            <Button
              type="submit"
              data-testid="search-submit"
              disabled={trimmedDraft.length === 0}
            >
              搜索
            </Button>
          </form>
        </CardContent>
      </Card>

      {activeQuery.length === 0 && (
        <p className="text-sm text-muted-foreground">
          请输入关键词以开始搜索。
        </p>
      )}

      {query.isLoading && activeQuery.length > 0 && (
        <div
          className="flex items-center gap-2 text-sm text-muted-foreground"
          data-testid="search-loading"
        >
          <Loader2 className="h-4 w-4 animate-spin" />
          正在搜索…
        </div>
      )}

      {query.isError && (
        <p
          className="text-sm text-destructive"
          data-testid="search-error"
        >
          搜索失败,请稍后重试。
        </p>
      )}

      {isEmpty && (
        <p
          className="text-sm text-muted-foreground"
          data-testid="search-empty"
        >
          未找到与「{activeQuery}」相关的内容。
        </p>
      )}

      {hasResults && (
        <div
          className="grid gap-6 md:grid-cols-3"
          data-testid="search-results"
        >
          <GroupSection kind="article" hits={grouped.article} />
          <GroupSection kind="ticket" hits={grouped.ticket} />
          <GroupSection kind="conversation" hits={grouped.conversation} />
        </div>
      )}

      {!hasResults &&
        !query.isLoading &&
        !query.isError &&
        activeQuery.length > 0 && null}

      {hasResults && (
        <p className="text-xs text-muted-foreground">
          共 {query.data?.total ?? 0} 条结果
        </p>
      )}
    </div>
  );
}