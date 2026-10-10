import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const CACHE_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../.api-token-cache.json',
);

interface TokenCache {
  email: string;
  accessToken: string;
  cachedAt: string;
}

export function readTokenCache(): TokenCache | null {
  if (!existsSync(CACHE_PATH)) return null;
  try {
    return JSON.parse(readFileSync(CACHE_PATH, 'utf8')) as TokenCache;
  } catch {
    return null;
  }
}

export function writeTokenCache(entry: TokenCache): void {
  writeFileSync(CACHE_PATH, JSON.stringify(entry, null, 2));
}