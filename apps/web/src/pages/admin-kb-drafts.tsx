import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { Button } from '@/components/ui/button';
import { DraftList } from '@/components/admin/draft-list';
import { fetchDrafts, type DraftStatus } from '@/lib/kb-drafts';

const STATUS_TABS: { value: DraftStatus; label: string }[] = [
  { value: 'DRAFT', label: '待审核' },
  { value: 'APPROVED', label: '已批准' },
  { value: 'REJECTED', label: '已拒绝' },
];

const KB_DRAFTS_QUERY_KEY = ['admin', 'kb-drafts'] as const;

export function AdminKbDraftsPage(): JSX.Element {
  const [status, setStatus] = useState<DraftStatus>('DRAFT');

  const draftsQuery = useQuery({
    queryKey: [...KB_DRAFTS_QUERY_KEY, status],
    queryFn: () => fetchDrafts({ status, limit: 50 }),
  });

  return (
    <div className="flex h-full flex-col gap-6 p-6" data-testid="admin-kb-drafts-page">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">KB 草稿审核</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          由 history-mining worker 自动从历史会话聚类生成的草稿。批准后将创建正式 KB 文章。
        </p>
      </div>

      <div className="flex gap-2" role="tablist">
        {STATUS_TABS.map((tab) => (
          <Button
            key={tab.value}
            variant={status === tab.value ? 'default' : 'outline'}
            size="sm"
            onClick={() => setStatus(tab.value)}
            data-testid={`status-tab-${tab.value}`}
          >
            {tab.label}
          </Button>
        ))}
      </div>

      {draftsQuery.isLoading ? (
        <p className="text-sm text-muted-foreground">加载中…</p>
      ) : draftsQuery.isError ? (
        <p
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          role="alert"
        >
          加载失败: {draftsQuery.error instanceof Error
            ? draftsQuery.error.message
            : '未知错误'}
        </p>
      ) : (
        <DraftList
          drafts={draftsQuery.data?.drafts ?? []}
          onChange={() => draftsQuery.refetch()}
        />
      )}
    </div>
  );
}
