import { Skeleton } from '@/components/ui/skeleton';
import { ArticleRow } from '@/components/kb/article-row';
import type { Article } from '@/lib/knowledge';

export interface ArticleListProps {
  articles: Article[];
  kbId: string;
  onDelete: (article: Article) => void;
  onReindex?: ((article: Article) => void) | undefined;
  busyArticleId?: string | null | undefined;
  isLoading?: boolean | undefined;
  emptyMessage?: string | undefined;
}

/**
 * Table-style list of articles in a KB. Pure presentational — the
 * parent owns the data fetch and the mutation handlers. Renders a
 * skeleton state, an empty state, or a header + rows.
 */
export function ArticleList({
  articles,
  kbId,
  onDelete,
  onReindex,
  busyArticleId,
  isLoading,
  emptyMessage = '该知识库暂无文章,可以通过上传文档或新建文章来添加。',
}: ArticleListProps): JSX.Element {
  if (isLoading === true) {
    return (
      <div className="flex flex-col" data-testid="article-list-loading">
        {Array.from({ length: 4 }).map((_, idx) => (
          <div
            key={idx}
            className="grid grid-cols-[minmax(0,1fr)_120px_80px_140px_140px] items-center gap-3 border-b px-4 py-3"
          >
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-5 w-16 rounded-full" />
            <Skeleton className="h-4 w-12" />
            <Skeleton className="h-4 w-20" />
            <Skeleton className="ml-auto h-8 w-16" />
          </div>
        ))}
      </div>
    );
  }

  if (articles.length === 0) {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 p-6 text-center text-sm text-muted-foreground"
        data-testid="article-list-empty"
      >
        <p className="text-base text-foreground">暂无文章</p>
        <p>{emptyMessage}</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col" data-testid="article-list">
      <div
        role="row"
        className="grid grid-cols-[minmax(0,1fr)_120px_80px_140px_140px] items-center gap-3 border-b bg-muted/30 px-4 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground"
      >
        <span>标题</span>
        <span>状态</span>
        <span>来源</span>
        <span>创建时间</span>
        <span className="text-right">操作</span>
      </div>
      {articles.map((article) => (
        <ArticleRow
          key={article.id}
          article={article}
          kbId={kbId}
          onDelete={onDelete}
          onReindex={onReindex}
          busy={busyArticleId === article.id}
        />
      ))}
    </div>
  );
}
