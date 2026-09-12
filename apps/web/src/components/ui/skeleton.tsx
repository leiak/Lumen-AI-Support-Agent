import { cn } from '@/lib/utils';

/**
 * Skeleton block — minimal shadcn-style primitive used by loading
 * states in the conversation pane. Intentionally renders a single
 * ``div`` so consumers can override width/height/rounded via the
 * standard Tailwind classes.
 */
function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>): JSX.Element {
  return (
    <div
      className={cn('animate-pulse rounded-md bg-muted', className)}
      {...props}
    />
  );
}

export { Skeleton };
