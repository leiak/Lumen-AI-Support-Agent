/**
 * Public SDK configuration as set by the embedding site on
 * `window.LumenAICustomerConfig` before the script tag runs.
 *
 * All fields except `apiBaseUrl`/`channelId`/`tenantId` are optional.
 */
export interface LumenConfig {
  /** Base URL of the Lumen API, e.g. "https://api.example.com". No trailing slash. */
  apiBaseUrl: string;
  /** The widget channel id (ChannelType.WEB). */
  channelId: string;
  /** Tenant id (must match the channel's tenant). */
  tenantId: string;
  /** Token endpoint path. Defaults to "/api/v1/widget/token". */
  widgetTokenEndpoint?: string;
  /** Hex color used as the accent for the floating button + iframe shell. */
  accentColor?: string;
  /** Where to dock the floating button. */
  position?: 'bottom-right' | 'bottom-left';
  /** Title shown in the chat header. */
  title?: string;
  /** Subtitle shown in the chat header. */
  subtitle?: string;
  /** BCP-47 locale tag. Currently informational only. */
  locale?: string;
  /** Stable id for this visitor; if omitted the SDK auto-generates one. */
  externalUserId?: string;
}

export interface ResolvedConfig extends LumenConfig {
  widgetTokenEndpoint: string;
  position: 'bottom-right' | 'bottom-left';
  title: string;
  subtitle: string;
  locale: string;
}

const DEFAULTS = {
  widgetTokenEndpoint: '/api/v1/widget/token',
  position: 'bottom-right' as const,
  title: '需要帮助?',
  subtitle: '我们的支持团队随时在线',
  locale: 'zh-CN',
};

/**
 * Read window.LumenAICustomerConfig, validate required fields, and apply
 * defaults. Throws a descriptive Error if the config is missing required
 * fields.
 */
export function readConfig(source: unknown): ResolvedConfig {
  if (source === null || typeof source !== 'object') {
    throw new Error(
      'Lumen widget: window.LumenAICustomerConfig is missing or invalid',
    );
  }
  const raw = source as Partial<LumenConfig>;

  if (typeof raw.apiBaseUrl !== 'string' || raw.apiBaseUrl.length === 0) {
    throw new Error('Lumen widget: apiBaseUrl is required');
  }
  if (typeof raw.channelId !== 'string' || raw.channelId.length === 0) {
    throw new Error('Lumen widget: channelId is required');
  }
  if (typeof raw.tenantId !== 'string' || raw.tenantId.length === 0) {
    throw new Error('Lumen widget: tenantId is required');
  }

  // Strip any trailing slash so URL building is deterministic.
  const apiBaseUrl = raw.apiBaseUrl.replace(/\/+$/, '');
  const widgetTokenEndpoint =
    raw.widgetTokenEndpoint ?? DEFAULTS.widgetTokenEndpoint;
  const position =
    raw.position === 'bottom-left' ? 'bottom-left' : DEFAULTS.position;

  return {
    apiBaseUrl,
    channelId: raw.channelId,
    tenantId: raw.tenantId,
    widgetTokenEndpoint,
    ...(raw.accentColor !== undefined ? { accentColor: raw.accentColor } : {}),
    position,
    title: raw.title ?? DEFAULTS.title,
    subtitle: raw.subtitle ?? DEFAULTS.subtitle,
    locale: raw.locale ?? DEFAULTS.locale,
    ...(raw.externalUserId !== undefined
      ? { externalUserId: raw.externalUserId }
      : {}),
  };
}
