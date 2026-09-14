import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import type { ConversationFilters, ConversationStatus } from '@/lib/conversations';

export interface ConversationFiltersProps {
  filters: ConversationFilters;
  onChange: (next: ConversationFilters) => void;
}

interface StatusOption {
  value: ConversationStatus | null;
  label: string;
}

const STATUS_OPTIONS: StatusOption[] = [
  { value: null, label: '全部' },
  { value: 'pending', label: '待处理' },
  { value: 'open', label: '进行中' },
  { value: 'closed', label: '已关闭' },
];

/**
 * Left-pane filter sidebar for the inbox page.
 *
 * Filters are server-side: `status` and `q` (case-insensitive match
 * against id / customer_external_id) are forwarded to the inbox endpoint.
 * The channel dropdown is intentionally deferred — the inbox endpoint does
 * not currently expose channel metadata, and a placeholder list would
 * mislead users.
 */
export function ConversationFilters({
  filters,
  onChange,
}: ConversationFiltersProps): JSX.Element {
  const handleStatusChange = (next: ConversationStatus | null): void => {
    onChange({ ...filters, status: next });
  };

  const handleSearchChange = (event: React.ChangeEvent<HTMLInputElement>): void => {
    onChange({ ...filters, search: event.target.value });
  };

  return (
    <aside
      className="flex w-56 shrink-0 flex-col gap-6 border-r bg-muted/20 p-4"
      aria-label="收件箱筛选"
    >
      <section className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          状态
        </h3>
        <div role="radiogroup" aria-label="状态筛选" className="flex flex-col gap-1">
          {STATUS_OPTIONS.map((option) => {
            const isActive = filters.status === option.value;
            return (
              <button
                key={option.label}
                type="button"
                role="radio"
                aria-checked={isActive}
                data-testid={`status-option-${option.value ?? 'all'}`}
                onClick={() => handleStatusChange(option.value)}
                className={cn(
                  'rounded-md px-3 py-1.5 text-left text-sm transition-colors',
                  isActive
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
                )}
              >
                {option.label}
              </button>
            );
          })}
        </div>
      </section>

      <section className="space-y-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          搜索
        </h3>
        <Input
          type="search"
          value={filters.search}
          onChange={handleSearchChange}
          placeholder="按会话 ID / 客户 ID 搜索"
          aria-label="按会话 ID 或客户 ID 搜索"
          data-testid="conversation-search"
        />
        <p className="text-xs text-muted-foreground">服务端搜索,匹配会话 ID / 客户 ID。</p>
      </section>
    </aside>
  );
}