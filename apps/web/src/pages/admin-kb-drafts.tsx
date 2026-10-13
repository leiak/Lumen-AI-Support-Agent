import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';

import { Button } from '@/components/ui/button';
import { DraftList } from '@/components/admin/draft-list';
import { fetchDrafts, triggerHistoryMining, type DraftStatus } from '@/lib/kb-drafts';

const STATUS_TABS: { value: DraftStatus; label: string }[] = [
  { value: 'DRAFT', label: '待审核' },
  { value: 'APPROVED', label: '已批准' },
  { value: 'REJECTED', label: '已拒绝' },
];

const KB_DRAFTS_QUERY_KEY = ['admin', 'kb-drafts'] as const;

export function AdminKbDraftsPage(): JSX.Element {
  const [status, setStatus] = useState<DraftStatus>('DRAFT');
  // Banner state for the "立即执行" button — null when idle, an
  // object with the API outcome when a click has been processed. The
  // banner is a thin inline notice, NOT a toast, so the operator
  // sees the result even if they navigate away briefly.
  const [runMiningBanner, setRunMiningBanner] = useState<
    { kind: 'ok' | 'error'; message: string } | null
  >(null);

  const draftsQuery = useQuery({
    queryKey: [...KB_DRAFTS_QUERY_KEY, status],
    queryFn: () => fetchDrafts({ status, limit: 50 }),
  });

  const runMiningMutation = useMutation({
    mutationFn: () => triggerHistoryMining(),
    onSuccess: (result) => {
      const workerNote = result.worker_started
        ? '已启动新 worker'
        : '复用现有 worker';
      setRunMiningBanner({
        kind: 'ok',
        message: `已入队 (${result.job_id ?? 'no-job-id'}),${workerNote}。请稍后刷新查看新草稿。`,
      });
      // Refetch DRAFT list in the background so newly-clustered
      // drafts appear when the operator comes back to this page.
      // 2s delay gives the worker a chance to enqueue + start.
      setTimeout(() => draftsQuery.refetch(), 2000);
    },
    onError: (err) => {
      const message = err instanceof Error ? err.message : '未知错误';
      // 503 from the backend carries "redis unavailable" — surface
      // it as a clear "queue unreachable" so the operator doesn't
      // think the click did nothing. Same copy as the
      // [2026-10-12 AI fail escalate] regression.
      const isQueue = /503|unreachable|redis/i.test(message);
      setRunMiningBanner({
        kind: 'error',
        message: isQueue
          ? '队列暂不可达,请稍后重试 (Redis 离线)'
          : `触发失败: ${message}`,
      });
    },
  });

  return (
    <div className="flex h-full flex-col gap-6 p-6" data-testid="admin-kb-drafts-page">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">KB 草稿审核</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            由 history-mining worker 自动从历史会话聚类生成的草稿。批准后将创建正式 KB 文章。
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setRunMiningBanner(null);
            runMiningMutation.mutate();
          }}
          disabled={runMiningMutation.isPending}
          data-testid="run-mining-button"
        >
          {runMiningMutation.isPending ? '执行中…' : '立即执行'}
        </Button>
      </div>

      {runMiningBanner && (
        <p
          role="status"
          data-testid={`run-mining-banner-${runMiningBanner.kind}`}
          className={
            runMiningBanner.kind === 'ok'
              ? 'rounded-md border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm text-emerald-800'
              : 'rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive'
          }
        >
          {runMiningBanner.message}
        </p>
      )}

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
