import { useNavigate } from 'react-router-dom';
import { Trash2, RefreshCw } from 'lucide-react';

import { ArticleStatusBadge } from '@/components/kb/article-status-badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { Article } from '@/lib/knowledge';

const SOURCE_TYPE_LABEL: Record<Article['source_type'], string> = {
  upload: '文件',
  url: 'URL',
  manual: '文本',
};

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

export interface ArticleRowProps {
  article: Article;
  kbId: string;
  onDelete: (article: Article) => void;
  onReindex?: ((article: Article) => void) | undefined;
  /** Disable both action buttons while a parent mutation is pending. */
  busy?: boolean | undefined;
}

/**
 * One row in the KB detail article table. Renders title (link),
 * status badge, source type, created timestamp, and the action
 * column. The row itself is NOT clickable — only the title navigates
 * to the detail page — because the action buttons need to be
 * independently clickable without triggering navigation.
 */
export function ArticleRow({
  article,
  kbId,
  onDelete,
  onReindex,
  busy,
}: ArticleRowProps): JSX.Element {
  const navigate = useNavigate();

  const handleTitleClick = (): void => {
    navigate(`/kb/${kbId}/articles/${article.id}`);
  };

  const handleTitleKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>): void => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      handleTitleClick();
    }
  };

  const handleReindex = (): void => {
    onReindex?.(article);
  };

  const handleDelete = (): void => {
    onDelete(article);
  };

  return (
    <div
      role="row"
      data-testid="article-row"
      data-article-id={article.id}
      className="grid grid-cols-[minmax(0,1fr)_120px_80px_140px_140px] items-center gap-3 border-b px-4 py-3 text-sm"
    >
      <button
        type="button"
        onClick={handleTitleClick}
        onKeyDown={handleTitleKeyDown}
        className={cn(
          'truncate text-left font-medium text-foreground',
          'hover:underline focus-visible:underline focus-visible:outline-none',
        )}
        title={article.title}
        data-testid="article-row-title"
      >
        {article.title}
      </button>
      <span>
        <ArticleStatusBadge status={article.status} />
      </span>
      <span className="text-xs text-muted-foreground">
        {SOURCE_TYPE_LABEL[article.source_type]}
      </span>
      <span className="text-xs text-muted-foreground">
        {formatRelativeTime(article.created_at)}
      </span>
      <div className="flex items-center justify-end gap-1">
        {onReindex ? (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={handleReindex}
            disabled={busy === true}
            aria-label="重新索引"
            title="重新索引"
            data-testid="article-row-reindex"
          >
            <RefreshCw />
          </Button>
        ) : null}
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={handleDelete}
          disabled={busy === true}
          aria-label="删除文章"
          title="删除文章"
          data-testid="article-row-delete"
        >
          <Trash2 />
        </Button>
      </div>
    </div>
  );
}
