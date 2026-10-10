/**
 * Seed extension: FEISHU channel + KB + 2 articles.
 *
 * Run AFTER `tests/e2e/scripts/seed.py` (which creates the demo tenant,
 * users, web channel, and one PENDING conversation). This extension
 * adds a FEISHU channel + a KnowledgeBase with two INDEXED articles so
 * the example-integration demo can hit FEISHU webhook events and find
 * KB rows by ID without going through the RAG worker.
 *
 * Talks to the same Postgres instance via `core.database.get_session()`,
 * which picks up DATABASE_URL from `apps/api/.env`.
 *
 * Re-running this script is safe: every row is keyed by a fixed ULID
 * and the helpers are skip-if-exists (mirrors tests/e2e/scripts/seed.py).
 */
import { existsSync, readFileSync } from 'node:fs';
import { execSync } from 'node:child_process';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

function findRepoRoot(start: string): string {
  let cur = resolve(start);
  for (let i = 0; i < 10; i++) {
    if (
      existsSync(resolve(cur, 'apps/api/src')) &&
      existsSync(resolve(cur, 'tests/e2e'))
    ) {
      return cur;
    }
    const parent = dirname(cur);
    if (parent === cur) break;
    cur = parent;
  }
  throw new Error('could not locate repo root');
}

const REPO_ROOT = findRepoRoot(dirname(fileURLToPath(import.meta.url)));
const PY_SCRIPT = resolve(dirname(fileURLToPath(import.meta.url)), 'extend-seed.py');

// Load apps/api/.env into process.env so DATABASE_URL is set before
// Python imports core.config (which constructs Settings on import).
const apiEnvPath = resolve(REPO_ROOT, 'apps/api/.env');
if (existsSync(apiEnvPath)) {
  for (const line of readFileSync(apiEnvPath, 'utf8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq < 0) continue;
    const k = trimmed.slice(0, eq).trim();
    const v = trimmed.slice(eq + 1).trim().replace(/^['"]|['"]$/g, '');
    if (!(k in process.env)) process.env[k] = v;
  }
}

const python = process.env.PYTHON ?? (existsSync(resolve(REPO_ROOT, 'apps/api/.venv/bin/python'))
  ? resolve(REPO_ROOT, 'apps/api/.venv/bin/python')
  : 'python3');

try {
  execSync(`"${python}" "${PY_SCRIPT}"`, {
    stdio: 'inherit',
    cwd: REPO_ROOT,
  });
} catch (err) {
  console.error('extend-seed failed:', err);
  process.exit(1);
}
