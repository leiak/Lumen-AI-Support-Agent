import { useEffect, useState } from 'react';
import { MessageSquare, RotateCcw } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  DEFAULT_PRIMARY_COLOR,
  DEFAULT_WELCOME_MESSAGE,
  DEFAULT_WIDGET_TITLE,
  fetchWidgetConfig,
  patchWidgetConfig,
  resetWidgetConfig,
  WIDGET_POSITIONS,
  WIDGET_THEMES,
  type WidgetConfig,
  type WidgetPosition,
  type WidgetTheme,
} from '@/lib/widget-config';
import { useCurrentUser } from '@/lib/use-current-user';
import { cn } from '@/lib/utils';

// Widget appearance config admin page — view + edit + reset the
// tenant's web widget (bubble) appearance.
//
// Backend:
//   GET    /api/v1/admin/tenants/{tid}/widget-config
//   PATCH  /api/v1/admin/tenants/{tid}/widget-config  — upsert
//   DELETE /api/v1/admin/tenants/{tid}/widget-config  — reset (idempotent)
//
// The widget SPA reads these on boot to render the accent color,
// position, and welcome banner. Defaults match the M2.A baseline
// so a freshly-seeded tenant looks identical before any admin edits.

const WIDGET_CONFIG_QUERY_KEY = ['admin', 'widget-config'] as const;

const HEX_COLOR_RE = /^#[0-9A-Fa-f]{6}$/;

function PreviewBubble({
  config,
}: {
  config: WidgetConfig;
}): JSX.Element {
  const positionClass =
    config.position === 'bottom-left'
      ? 'left-4 sm:left-8'
      : 'right-4 sm:right-8';
  return (
    <div
      className={cn(
        'fixed bottom-4 z-10 sm:bottom-8',
        positionClass,
      )}
      data-testid="widget-preview-bubble"
    >
      <button
        type="button"
        className="rounded-full px-4 py-3 text-sm font-medium text-white shadow-lg"
        style={{ backgroundColor: config.primary_color }}
        disabled
      >
        {config.widget_title}
      </button>
    </div>
  );
}

function WidgetConfigEditor({
  initial,
  tenantId,
}: {
  initial: WidgetConfig;
  tenantId: string;
}): JSX.Element {
  const qc = useQueryClient();
  const [theme, setTheme] = useState<WidgetTheme>(initial.theme);
  const [primaryColor, setPrimaryColor] = useState(initial.primary_color);
  const [position, setPosition] = useState<WidgetPosition>(initial.position);
  const [widgetTitle, setWidgetTitle] = useState(initial.widget_title);
  const [welcomeMessage, setWelcomeMessage] = useState(
    initial.welcome_message,
  );
  const [logoUrl, setLogoUrl] = useState(initial.logo_url ?? '');
  const [enabled, setEnabled] = useState(initial.enabled);

  // Reset local state when the server-side row changes.
  useEffect(() => {
    setTheme(initial.theme);
    setPrimaryColor(initial.primary_color);
    setPosition(initial.position);
    setWidgetTitle(initial.widget_title);
    setWelcomeMessage(initial.welcome_message);
    setLogoUrl(initial.logo_url ?? '');
    setEnabled(initial.enabled);
  }, [
    initial.theme,
    initial.primary_color,
    initial.position,
    initial.widget_title,
    initial.welcome_message,
    initial.logo_url,
    initial.enabled,
  ]);

  const dirty =
    theme !== initial.theme ||
    primaryColor !== initial.primary_color ||
    position !== initial.position ||
    widgetTitle !== initial.widget_title ||
    welcomeMessage !== initial.welcome_message ||
    (logoUrl.trim() || null) !== (initial.logo_url ?? null) ||
    enabled !== initial.enabled;

  const colorInvalid = !HEX_COLOR_RE.test(primaryColor);
  const titleInvalid = widgetTitle.trim().length === 0;
  const welcomeInvalid =
    welcomeMessage.trim().length === 0 || welcomeMessage.length > 500;
  const logoInvalid = logoUrl.length > 500;

  const patchMut = useMutation({
    mutationFn: () =>
      patchWidgetConfig(tenantId, {
        theme,
        primary_color: primaryColor,
        position,
        widget_title: widgetTitle,
        welcome_message: welcomeMessage,
        logo_url: logoUrl.trim().length > 0 ? logoUrl.trim() : null,
        enabled,
      }),
    onSuccess: (next) => {
      void qc.setQueryData(
        [...WIDGET_CONFIG_QUERY_KEY, tenantId],
        next,
      );
    },
  });

  const resetMut = useMutation({
    mutationFn: () => resetWidgetConfig(tenantId),
    onSuccess: () => {
      // After DELETE, GET returns defaults — invalidate to refetch.
      void qc.invalidateQueries({
        queryKey: [...WIDGET_CONFIG_QUERY_KEY, tenantId],
      });
    },
  });

  function handleReset(): void {
    if (!window.confirm('重置 widget 配置为默认值?')) return;
    resetMut.mutate();
  }

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
      <Card data-testid="widget-config-card">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <MessageSquare className="h-4 w-4" />
            Widget 外观设置
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">主题</span>
              <select
                className="rounded border px-2 py-1.5"
                data-testid="widget-config-theme"
                value={theme}
                onChange={(e) => setTheme(e.target.value as WidgetTheme)}
              >
                {WIDGET_THEMES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">位置</span>
              <select
                className="rounded border px-2 py-1.5"
                data-testid="widget-config-position"
                value={position}
                onChange={(e) => setPosition(e.target.value as WidgetPosition)}
              >
                {WIDGET_POSITIONS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">主色 (#RRGGBB)</span>
              <input
                className={cn(
                  'rounded border px-2 py-1.5 font-mono',
                  colorInvalid && 'border-destructive',
                )}
                data-testid="widget-config-primary-color"
                value={primaryColor}
                onChange={(e) => setPrimaryColor(e.target.value)}
                maxLength={7}
              />
              {colorInvalid && (
                <span
                  className="text-xs text-destructive"
                  data-testid="widget-config-color-error"
                >
                  必须为 #RRGGBB 格式
                </span>
              )}
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">启用</span>
              <label className="mt-2 inline-flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  data-testid="widget-config-enabled"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                />
                {enabled ? '已启用' : '已禁用'}
              </label>
            </label>
            <label className="flex flex-col gap-1 text-sm sm:col-span-2">
              <span className="text-muted-foreground">气泡标题</span>
              <input
                className={cn(
                  'rounded border px-2 py-1.5',
                  titleInvalid && 'border-destructive',
                )}
                data-testid="widget-config-title"
                value={widgetTitle}
                onChange={(e) => setWidgetTitle(e.target.value)}
                maxLength={64}
              />
              {titleInvalid && (
                <span
                  className="text-xs text-destructive"
                  data-testid="widget-config-title-error"
                >
                  标题不能为空
                </span>
              )}
            </label>
            <label className="flex flex-col gap-1 text-sm sm:col-span-2">
              <span className="text-muted-foreground">欢迎语</span>
              <textarea
                rows={3}
                className={cn(
                  'rounded border px-2 py-1.5',
                  welcomeInvalid && 'border-destructive',
                )}
                data-testid="widget-config-welcome"
                value={welcomeMessage}
                onChange={(e) => setWelcomeMessage(e.target.value)}
                maxLength={500}
              />
              {welcomeInvalid && (
                <span
                  className="text-xs text-destructive"
                  data-testid="widget-config-welcome-error"
                >
                  欢迎语不能为空且 ≤ 500 字符
                </span>
              )}
            </label>
            <label className="flex flex-col gap-1 text-sm sm:col-span-2">
              <span className="text-muted-foreground">Logo URL (可选)</span>
              <input
                className={cn(
                  'rounded border px-2 py-1.5',
                  logoInvalid && 'border-destructive',
                )}
                data-testid="widget-config-logo"
                value={logoUrl}
                onChange={(e) => setLogoUrl(e.target.value)}
                maxLength={500}
                placeholder="https://..."
              />
            </label>
          </div>
          <div className="mt-4 flex justify-between">
            <Button
              type="button"
              size="sm"
              variant="outline"
              data-testid="widget-config-reset"
              onClick={handleReset}
              disabled={resetMut.isPending}
            >
              <RotateCcw className="mr-1 h-3 w-3" />
              恢复默认
            </Button>
            <Button
              type="button"
              size="sm"
              data-testid="widget-config-save"
              onClick={() => patchMut.mutate()}
              disabled={
                !dirty ||
                colorInvalid ||
                titleInvalid ||
                welcomeInvalid ||
                logoInvalid ||
                patchMut.isPending
              }
            >
              {patchMut.isPending ? '保存中…' : '保存'}
            </Button>
          </div>
          {patchMut.isError && (
            <p
              className="mt-2 text-xs text-destructive"
              role="alert"
              data-testid="widget-config-error"
            >
              保存失败:{' '}
              {patchMut.error instanceof Error
                ? patchMut.error.message
                : '未知错误'}
            </p>
          )}
        </CardContent>
      </Card>

      <Card data-testid="widget-config-preview-card">
        <CardHeader>
          <CardTitle className="text-base">预览</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">
            右上角的气泡模拟当前设置。保存后实际 widget 将按此渲染。
          </p>
          <div className="mt-3 flex flex-col gap-2 text-sm">
            <span>
              <Badge variant="outline" className="mr-2">
                主题
              </Badge>
              {theme}
            </span>
            <span>
              <Badge variant="outline" className="mr-2">
                位置
              </Badge>
              {position}
            </span>
            <span>
              <Badge variant="outline" className="mr-2">
                启用
              </Badge>
              {enabled ? '是' : '否'}
            </span>
            <span>
              <Badge variant="outline" className="mr-2">
                标题
              </Badge>
              {widgetTitle}
            </span>
            <span>
              <Badge variant="outline" className="mr-2">
                欢迎语
              </Badge>
              {welcomeMessage}
            </span>
          </div>
          <PreviewBubble
            config={{
              ...initial,
              theme,
              primary_color: primaryColor,
              position,
              widget_title: widgetTitle,
              welcome_message: welcomeMessage,
              enabled,
            }}
          />
        </CardContent>
      </Card>
    </div>
  );
}

export function AdminWidgetConfigPage(): JSX.Element {
  const { user } = useCurrentUser();
  const tenantId = user?.tenant_id ?? '';

  const widgetQuery = useQuery({
    queryKey: [...WIDGET_CONFIG_QUERY_KEY, tenantId],
    queryFn: () => fetchWidgetConfig(tenantId),
    enabled: Boolean(tenantId),
  });

  return (
    <div
      className="flex h-full flex-col gap-6 p-6"
      data-testid="admin-widget-config-page"
    >
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          Widget 配置
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          管理 Web Widget 气泡的主题色、位置、标题与欢迎语。
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          默认值:
          主色 <code>{DEFAULT_PRIMARY_COLOR}</code> ·
          标题 <code>“{DEFAULT_WIDGET_TITLE}”</code> ·
          欢迎语 <code>“{DEFAULT_WELCOME_MESSAGE}”</code>
        </p>
      </div>

      {widgetQuery.isLoading ? (
        <p
          className="text-sm text-muted-foreground"
          data-testid="widget-config-loading"
        >
          加载中…
        </p>
      ) : widgetQuery.isError ? (
        <p
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          role="alert"
          data-testid="widget-config-error-loading"
        >
          加载失败:{' '}
          {widgetQuery.error instanceof Error
            ? widgetQuery.error.message
            : '未知错误'}
        </p>
      ) : widgetQuery.data ? (
        <WidgetConfigEditor
          initial={widgetQuery.data}
          tenantId={tenantId}
        />
      ) : null}
    </div>
  );
}