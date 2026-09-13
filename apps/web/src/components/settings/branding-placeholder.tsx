import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

export interface BrandingPlaceholderProps {
  /** Optional logo URL hint. Reserved for M2 when branding is editable. */
  logoUrl?: string | null;
}

/**
 * M1 placeholder for branding controls (logo URL, theme color, welcome
 * message). Editing is M2+; this card renders a muted hint so the
 * settings page feels complete without promising functionality.
 */
export function BrandingPlaceholder({
  logoUrl,
}: BrandingPlaceholderProps): JSX.Element {
  return (
    <Card data-testid="branding-placeholder">
      <CardHeader>
        <CardTitle className="text-lg">品牌设置 (Branding)</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <p className="text-sm text-muted-foreground" data-testid="branding-hint">
          M2 提供 — logo URL、主题色、欢迎语。
        </p>
        {logoUrl !== undefined && logoUrl !== null && logoUrl.length > 0 ? (
          <p
            className="font-mono text-xs text-foreground"
            data-testid="branding-logo-url"
            title={logoUrl}
          >
            {logoUrl}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}