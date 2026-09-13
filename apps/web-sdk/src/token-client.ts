/**
 * Mints a short-lived widget JWT by POSTing to the widget token endpoint.
 *
 * Mirrors `apps/api/src/widget/api.py::issue_widget_token`:
 *
 *   POST {widgetTokenEndpoint}
 *   body: { channel_id, external_user_id }
 *   resp: { token, expires_at, expires_in }
 */
import type { ResolvedConfig } from './config.js';

export interface TokenResponse {
  token: string;
  expiresAt: Date;
  expiresInSeconds: number;
}

export class TokenError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = 'TokenError';
  }
}

export interface TokenClientOptions {
  fetchImpl?: typeof fetch;
}

/**
 * Issue a widget token. Throws TokenError on non-2xx responses.
 */
export async function mintWidgetToken(
  config: ResolvedConfig,
  externalUserId: string,
  options: TokenClientOptions = {},
): Promise<TokenResponse> {
  const f = options.fetchImpl ?? fetch;
  const url = `${config.apiBaseUrl}${config.widgetTokenEndpoint}`;
  let res: Response;
  try {
    res = await f(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        channel_id: config.channelId,
        external_user_id: externalUserId,
      }),
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : 'network error';
    throw new TokenError(`token request failed: ${msg}`, 0);
  }

  if (!res.ok) {
    let detail = '';
    try {
      const body = (await res.json()) as { detail?: string };
      detail = body.detail ?? '';
    } catch {
      // body wasn't JSON — leave detail blank.
    }
    throw new TokenError(
      `token endpoint returned ${res.status}${detail ? `: ${detail}` : ''}`,
      res.status,
    );
  }

  const body = (await res.json()) as {
    token: string;
    expires_at: string;
    expires_in: number;
  };
  if (
    typeof body.token !== 'string' ||
    typeof body.expires_at !== 'string' ||
    typeof body.expires_in !== 'number'
  ) {
    throw new TokenError('token endpoint returned an unexpected payload', 200);
  }
  return {
    token: body.token,
    expiresAt: new Date(body.expires_at),
    expiresInSeconds: body.expires_in,
  };
}
