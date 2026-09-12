import { useMemo } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { AxiosError } from 'axios';
import { RefreshCw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { ConversationFilters } from '@/components/inbox/conversation-filters';
import { ConversationRow } from '@/components/inbox/conversation-row';
import { Pagination } from '@/components/inbox/pagination';
import {
  fetchConversations,
  type Conversation,
  type ConversationFilters as ConversationFiltersT,
  type ConversationStatus,
} from '@/lib/conversations';

// Page size for client-side pagination. The inbox endpoint returns the
// full agent queue in one shot; we slice on the client for now. Matches
// the design note in Task 9.4 ("limit=20").
const PAGE_SIZE = 20;

const VALID_STATUSES: ReadonlySet<ConversationStatus> = new Set([
  'open',
  'pending',
  'closed',
]);

function parseStatusParam(raw: string | null): ConversationStatus | null {
  if (!raw) return null;
  const lower = raw.toLowerCase();
  return VALID_STATUSES.has(lower as ConversationStatus)
    ? (lower as ConversationStatus)
    : null;
}

function parsePageParam(raw: string | null): number {
  if (!raw) return 1;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < 1) return 1;
  return parsed;
}

/**
 * Pull every supported filter out of the URL. Defaults live in the
 * `DEFAULT_FILTERS` constant so they stay in sync with the backend
 * filter shape.
 */
function readFiltersFromParams(params: URLSearchParams): ConversationFiltersT {
  return {
    status: parseStatusParam(params.get('status')),
    search: params.get('q') ?? '',
  };
}

const QUERY_KEY = ['conversations'] as const;

interface FetchErrorPayload {
  detail?: string | Array<{ msg?: string }>;
}

/**
 * Extract a user-presentable error message from an unknown thrown value.
 * Prefers the backend's `detail` field (FastAPI's standard) so failures
 * stay consistent with the rest of the workspace; never echoes the raw
 * stack trace.
 */
function extractErrorMessage(error: unknown): string {
  if (error instanceof AxiosError) {
    const payload = error.response?.data as FetchErrorPayload | undefined;
    const detail = payload?.detail;
    if (typeof detail === 'string' && detail.trim().length > 0) return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0];
      if (first && typeof first === 'object' && typeof first.msg === 'string') {
        return first.msg;
      }
    }
    return error.message || '请求失败,请稍后再试。';
  }
  if (error instanceof Error && error.message) return error.message;
  return '请求失败,请稍后再试。';
}

export function InboxPage(): JSX.Element {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();

  const filters = useMemo<ConversationFiltersT>(
    () => readFiltersFromParams(searchParams),
    [searchParams],
  );
  const page = parsePageParam(searchParams.get('page'));

  // Query key encodes every filter so cache partitions cleanly. The
  // `keepPreviousData` placeholder keeps the previous list visible while
  // the new filter set is fetching — feels faster for typing in the
  // search box.
  const query = useQuery({
    queryKey: [...QUERY_KEY, filters.status ?? 'all'],
    queryFn: () => fetchConversations(filters),
    placeholderData: (previous) => previous,
  });

  // Client-side search filter — the inbox endpoint has no `q` parameter.
  // Match against id and customer_external_id (both fields are opaque
  // tenant-meaningful ids, never raw PII).
  const filteredItems = useMemo<Conversation[]>(() => {
    const items = query.data?.items ?? [];
    const needle = filters.search.trim().toLowerCase();
    if (!needle) return items;
    return items.filter(
      (item) =>
        item.id.toLowerCase().includes(needle) ||
        item.customer_external_id.toLowerCase().includes(needle),
    );
  }, [query.data, filters.search]);

  const pageCount = Math.max(1, Math.ceil(filteredItems.length / PAGE_SIZE));
  // Clamp the URL page back into range if filters shrank the dataset.
  const safePage = Math.min(page, pageCount);
  const pageStart = (safePage - 1) * PAGE_SIZE;
  const pageItems = filteredItems.slice(pageStart, pageStart + PAGE_SIZE);

  const handleFiltersChange = (next: ConversationFiltersT): void => {
    const params = new URLSearchParams();
    if (next.status) params.set('status', next.status);
    if (next.search.trim()) params.set('q', next.search.trim());
    // Filter changes always reset the page to 1 — `page` is intentionally
    // omitted so the URL stays clean.
    setSearchParams(params, { replace: true });
  };

  const handlePageChange = (nextPage: number): void => {
    const params = new URLSearchParams(searchParams);
    if (nextPage === 1) {
      params.delete('page');
    } else {
      params.set('page', String(nextPage));
    }
    setSearchParams(params, { replace: true });
  };

  const handleRefresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
  };

  const handleRetry = (): void => {
    void query.refetch();
  };

  return (
    <div className="flex h-full w-full">
      <ConversationFilters filters={filters} onChange={handleFiltersChange} />

      <div className="flex flex-1 flex-col overflow-hidden">
        <Card className="m-4 flex flex-1 flex-col overflow-hidden">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-lg">收件箱</CardTitle>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleRefresh}
              disabled={query.isFetching}
              data-testid="inbox-refresh"
            >
              <RefreshCw />
              刷新
            </Button>
          </CardHeader>
          <CardContent className="flex flex-1 flex-col overflow-hidden p-0">
            <div
              className="grid grid-cols-[140px_120px_100px_1fr_140px_140px] items-center gap-3 border-b bg-muted/30 px-4 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground"
              role="row"
            >
              <span>会话 ID</span>
              <span>渠道</span>
              <span>状态</span>
              <span>分配人</span>
              <span>最后活动</span>
              <span>客户</span>
            </div>

            {query.isError ? (
              <div
                className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center"
                role="alert"
                data-testid="inbox-error"
              >
                <p className="text-sm text-destructive">
                  {extractErrorMessage(query.error)}
                </p>
                <Button type="button" size="sm" onClick={handleRetry}>
                  重试
                </Button>
              </div>
            ) : query.isPending ? (
              <div className="flex-1 overflow-auto" data-testid="inbox-loading">
                {Array.from({ length: PAGE_SIZE }).map((_, idx) => (
                  <div
                    key={idx}
                    className="grid grid-cols-[140px_120px_100px_1fr_140px_140px] items-center gap-3 border-b px-4 py-3"
                  >
                    <span className="h-4 w-24 animate-pulse rounded bg-muted" />
                    <span className="h-4 w-20 animate-pulse rounded bg-muted" />
                    <span className="h-5 w-14 animate-pulse rounded-full bg-muted" />
                    <span className="h-4 w-32 animate-pulse rounded bg-muted" />
                    <span className="h-4 w-20 animate-pulse rounded bg-muted" />
                    <span className="h-4 w-28 animate-pulse rounded bg-muted" />
                  </div>
                ))}
              </div>
            ) : filteredItems.length === 0 ? (
              <div
                className="flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center text-sm text-muted-foreground"
                data-testid="inbox-empty"
              >
                <p className="text-base text-foreground">暂无会话</p>
                <p>当前筛选条件下没有匹配的会话。</p>
              </div>
            ) : (
              <div className="flex-1 overflow-auto" data-testid="inbox-list">
                {pageItems.map((conv) => (
                  <ConversationRow key={conv.id} conversation={conv} />
                ))}
              </div>
            )}

            {!query.isError && !query.isPending ? (
              <Pagination
                page={safePage}
                pageCount={pageCount}
                onPageChange={handlePageChange}
              />
            ) : null}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}