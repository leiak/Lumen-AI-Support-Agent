// Loads configuration from process.env. The widget-demo reads from
// import.meta.env (Vite-injected), but CLI scripts use process.env.
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

export interface Config {
  apiBaseUrl: string;
  tenantId: string;
  webChannelId: string;
  feishuAppId: string;
}

function loadDotenv(): void {
  // Minimal .env loader — keep zero deps. Only reads key=value lines.
  const here = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    resolve(here, '../../.env'),
    resolve(here, '../../../.env'),
  ];
  for (const path of candidates) {
    if (!existsSync(path)) continue;
    const text = readFileSync(path, 'utf8');
    for (const line of text.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const eq = trimmed.indexOf('=');
      if (eq < 0) continue;
      const key = trimmed.slice(0, eq).trim();
      const val = trimmed.slice(eq + 1).trim().replace(/^['"]|['"]$/g, '');
      if (!(key in process.env)) process.env[key] = val;
    }
  }
}

loadDotenv();

function required(name: string): string {
  const v = process.env[name];
  if (!v) {
    throw new Error(`Missing required env var: ${name}. Copy .env.example to .env first.`);
  }
  return v;
}

export const config: Config = {
  apiBaseUrl: process.env.API_BASE_URL ?? 'http://localhost:8000',
  tenantId: process.env.TENANT_ID ?? '01HZDEMO00000000000000000',
  webChannelId: process.env.CHANNEL_ID ?? '01HZDEMO00000000000000003',
  feishuAppId: process.env.FEISHU_APP_ID ?? 'demo-feishu-app-001',
};

// Silence unused-export warning for `required` helper while keeping it
// exported for future tasks that may need strict env validation.
export { required };
