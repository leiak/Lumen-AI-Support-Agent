import { Button } from '@/components/ui/button';

export interface PaginationProps {
  page: number;
  /** Total number of pages. A value of 1 means "no next page". */
  pageCount: number;
  onPageChange: (next: number) => void;
}

/**
 * Minimal two-button pagination control for the inbox list.
 *
 * The M1 inbox endpoint does not support server-side paging, so this
 * component operates on a computed `pageCount` derived from the total
 * count and the page size. Pages are 1-indexed in the UI (matching what
 * users expect); the conversion to a 0-indexed offset happens in the
 * page component.
 */
export function Pagination({
  page,
  pageCount,
  onPageChange,
}: PaginationProps): JSX.Element {
  const canPrev = page > 1;
  const canNext = page < pageCount;

  const handlePrev = (): void => {
    if (canPrev) onPageChange(page - 1);
  };
  const handleNext = (): void => {
    if (canNext) onPageChange(page + 1);
  };

  return (
    <div
      className="flex items-center justify-end gap-2 px-4 py-3"
      data-testid="inbox-pagination"
    >
      <span className="text-xs text-muted-foreground">
        第 {page} / {pageCount} 页
      </span>
      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={handlePrev}
        disabled={!canPrev}
        data-testid="pagination-prev"
      >
        上一页
      </Button>
      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={handleNext}
        disabled={!canNext}
        data-testid="pagination-next"
      >
        下一页
      </Button>
    </div>
  );
}