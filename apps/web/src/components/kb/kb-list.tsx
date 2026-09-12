import { useNavigate } from 'react-router-dom';

import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { KnowledgeBase } from '@/lib/knowledge';

export interface KbListProps {
  knowledgeBases: KnowledgeBase[];
  isLoading?: boolean | undefined;
  /** KB id → article count, surfaced as 文章数. */
  articleCounts?: Record<string, number> | undefined;
  onDelete: (kb: KnowledgeBase) => void;
  busyKbId?: string | null | undefined;
}

function formatRelativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso);
  const diffMs = now.getTime() - then.getTime();
  if (Number.isNaN(diffMs)) return iso;
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  return then.toISOString().slice(0, 10);
}

interface KbRowProps {
  kb: KnowledgeBase;
  articleCount?: number | undefined;
  onOpen: (kb: KnowledgeBase) => void;
  onDelete: (kb: KnowledgeBase) => void;
  busy?: boolean | undefined;
}

function KbRow({
  kb,
  articleCount,
  onOpen,
  onDelete,
  busy,
}: KbRowProps): JSX.Element {
  const navigate = useNavigate();
  const handleOpen = (): void => {
    onOpen(kb);
    navigate(`/kb/${kb.id}`);
  };
  return (
    <div
      role="row"
      data-testid="kb-row"
      data-kb-id={kb.id}
      className={cn(
        'grid cursor-pointer grid-cols-[minmax(0,1fr)_180px_120px_140px_60px] items-center gap-3 border-b px-4 py-3 text-sm transition-colors',
        'hover:bg-accent/50 focus-within:bg-accent/50',
      )}
      onClick={handleOpen}
    >
      <div className="min-w-0">
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            handleOpen();
          }}
          className="truncate text-left font-medium text-foreground hover:underline focus-visible:underline focus-visible:outline-none"
          title={kb.name}
          data-testid="kb-row-name"
        >
          {kb.name}
        </button>
        {kb.description !== null && kb.description.length > 0 ? (
          <p
            className="mt-1 truncate text-xs text-muted-foreground"
            title={kb.description}
          >
            {kb.description}
          </p>
        ) : null}
      </div>
      <span className="rounded-md bg-secondary px-2 py-0.5 font-mono text-xs text-secondary-foreground">
        {kb.embedding_model}
      </span>
      <span className="text-xs text-muted-foreground">
        {typeof articleCount === 'number' ? `${articleCount} 篇` : '—'}
      </span>
      <span className="text-xs text-muted-foreground">
        {formatRelativeTime(kb.created_at)}
      </span>
      <div className="flex items-center justify-end">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={(e) => {
            e.stopPropagation();
            onDelete(kb);
          }}
          disabled={busy === true}
          aria-label={`删除知识库 ${kb.name}`}
          title="删除"
          data-testid="kb-row-delete"
        >
          删除
        </Button>
      </div>
    </div>
  );
}

function KbSkeletonRow(): JSX.Element {
  return (
    <div
      role="row"
      className="grid grid-cols-[minmax(0,1fr)_180px_120px_140px_60px] items-center gap-3 border-b px-4 py-3 text-sm"
    >
      <Skeleton className="h-4 w-2/3" />
      <Skeleton className="h-5 w-32 rounded-full" />
      <Skeleton className="h-4 w-12" />
      <Skeleton className="h-4 w-20" />
      <Skeleton className="ml-auto h-8 w-12" />
    </div>
  );
}

/**
 * Table-style list of knowledge bases. Pure presentational — the
 * parent owns the data fetch and the delete handler. Renders a
 * skeleton state, an empty state, or a header + rows.
 */
export function KbList({
  knowledgeBases,
  isLoading,
  articleCounts,
  onDelete,
  busyKbId,
}: KbListProps): JSX.Element {
  if (isLoading === true) {
    return (
      <div className="flex flex-col" data-testid="kb-list-loading">
        <div
          role="row"
          className="grid grid-cols-[minmax(0,1fr)_180px_120px_140px_60px] items-center gap-3 border-b bg-muted/30 px-4 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground"
        >
          <span>名称</span>
          <span>Embedding 模型</span>
          <span>文章数</span>
          <span>创建时间</span>
          <span className="text-right">操作</span>
        </div>
        {Array.from({ length: 4 }).map((_, idx) => (
          <KbSkeletonRow key={idx} />
        ))}
      </div>
    );
  }

  if (knowledgeBases.length === 0) {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 p-6 text-center text-sm text-muted-foreground"
        data-testid="kb-list-empty"
      >
        <p className="text-base text-foreground">暂无知识库</p>
        <p>点击右上角的"新建知识库"按钮来创建第一个知识库。</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col" data-testid="kb-list">
      <div
        role="row"
        className="grid grid-cols-[minmax(0,1fr)_180px_120px_140px_60px] items-center gap-3 border-b bg-muted/30 px-4 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground"
      >
        <span>名称</span>
        <span>Embedding 模型</span>
        <span>文章数</span>
        <span>创建时间</span>
        <span className="text-right">操作</span>
      </div>
      {knowledgeBases.map((kb) => (
        <KbRow
          key={kb.id}
          kb={kb}
          articleCount={articleCounts?.[kb.id]}
          onOpen={() => undefined}
          onDelete={onDelete}
          busy={busyKbId === kb.id}
        />
      ))}
    </div>
  );
}
