import { cva, type VariantProps } from 'class-variance-authority';

export const badgeVariants = cva(
  'inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2',
  {
    variants: {
      variant: {
        default: 'border-transparent bg-primary text-primary-foreground',
        secondary: 'border-transparent bg-secondary text-secondary-foreground',
        destructive: 'border-transparent bg-destructive text-destructive-foreground',
        outline: 'text-foreground',
        // Status-specific hues for ConversationStatus badges. Kept neutral so
        // they read clearly on light AND dark backgrounds; no fully-saturated
        // fills that would clash with shadcn's muted palette.
        pending: 'border-transparent bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200',
        open: 'border-transparent bg-blue-100 text-blue-900 dark:bg-blue-900/40 dark:text-blue-200',
        closed: 'border-transparent bg-zinc-200 text-zinc-700 dark:bg-zinc-700/60 dark:text-zinc-200',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  },
);

export type BadgeVariantProps = VariantProps<typeof badgeVariants>;