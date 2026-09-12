import { Badge } from '@/components/ui/badge';
import type { ArticleStatus } from '@/lib/knowledge';

const STATUS_LABEL: Record<ArticleStatus, string> = {
  draft: '草稿',
  indexing: '索引中',
  indexed: '已索引',
  failed: '失败',
};

const STATUS_VARIANT = {
  draft: 'secondary',
  indexing: 'default',
  indexed: 'open',
  failed: 'destructive',
} as const satisfies Record<
  ArticleStatus,
  'secondary' | 'default' | 'open' | 'destructive'
>;

export interface ArticleStatusBadgeProps {
  status: ArticleStatus;
}

/**
 * Small colour-coded badge for an Article's lifecycle status. The four
 * shadcn-friendly hues match the spec: DRAFT gray, INDEXING blue,
 * INDEXED green, FAILED red.
 */
export function ArticleStatusBadge({
  status,
}: ArticleStatusBadgeProps): JSX.Element {
  return (
    <Badge
      variant={STATUS_VARIANT[status]}
      data-testid="article-status-badge"
      data-status={status}
    >
      {STATUS_LABEL[status]}
    </Badge>
  );
}
