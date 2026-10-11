# scripts/ — root-level operator scripts

One-command bring-up + teardown utilities for the Lumen AI demo stack.
These wrap `deploy/docker-compose.yml` + the seed-chain (4 scripts)
into repeatable, idempotent commands safe to re-run any time.

## Files

| Script | Purpose | When to use |
|--------|---------|-------------|
| `bring-up.sh` | docker compose up + wait + SDK build + 4-script seed chain | First run, or after `teardown.sh --volumes` |
| `teardown.sh` | docker compose down (optionally with --volumes / --images) | Clean slate, or to free disk |
| `wait-ready.sh` | Poll an HTTP endpoint until 200 or timeout | Sub-routine used by `bring-up.sh` and CI |

## Bring-up cheat sheet

```bash
# Default: build + up + seed chain
scripts/bring-up.sh

# Use cached images (faster iteration)
scripts/bring-up.sh --no-build

# Skip the seed chain entirely (e.g. operator will run it manually)
scripts/bring-up.sh --no-seeds

# Skip docker — only run the 4 seed scripts
scripts/bring-up.sh --seed-only

# Show all flags + exit codes
scripts/bring-up.sh --help
```

After `bring-up.sh` finishes:

| Surface | URL | Login |
|---------|-----|-------|
| API | http://localhost:8000 | — |
| Admin SPA | http://localhost:8080 | `admin@example.com` / `Demo123!` |
| Agent SPA | http://localhost:8080 | `agent@example.com` / `Demo123!` |
| Widget host | `pnpm widget` (from `apps/example-integration`) | — |

## Teardown cheat sheet

```bash
scripts/teardown.sh              # stop containers, KEEP volumes
scripts/teardown.sh --volumes    # also wipe Postgres/Redis/Qdrant/MinIO data
scripts/teardown.sh --images     # also prune dangling Docker images
```

`--volumes` triggers DATA LOSS — you'll need to re-run `bring-up.sh`
to recreate the demo tenant + seed data.

## Seed chain order

The 4 seed scripts run in this fixed order — each depends on rows
inserted by the previous one:

| # | Script | Adds |
|---|--------|------|
| 1 | `tests/e2e/scripts/seed.py` | Demo tenant + agent/admin users + WEB channel + 1 PENDING conversation |
| 2 | `tests/e2e/scripts/seed_budget.py` | `tenant_budgets` + `tenant_budget_credits` + `llm_usage` rows |
| 3 | `tests/e2e/scripts/seed_demo_rich.py` | 4 SLA + 5 conv + 3 tickets + ticket events + budget usage |
| 4 | `apps/example-integration/scripts/extend-seed.py` (via `pnpm seed`) | FEISHU channel + KB + 2 articles + tenant_llm_config |

Each script supports `--help` for individual use:

```bash
cd apps/api && uv run python ../../tests/e2e/scripts/seed.py --help
cd apps/api && uv run python ../../tests/e2e/scripts/seed_budget.py --help
cd apps/api && uv run python ../../tests/e2e/scripts/seed_demo_rich.py --help
apps/api/.venv/Scripts/python.exe apps/example-integration/scripts/extend-seed.py --help
```

All 4 are **idempotent** — every row dedupes on a stable natural key,
so re-running them is a no-op.

## Exit codes

| Script | 0 | 1 | 2 | 64 |
|--------|---|---|---|-----|
| `bring-up.sh` | success | docker compose failure | API never became ready / seed failure | unknown CLI flag |
| `teardown.sh` | success | docker compose failure | — | unknown CLI flag |
| `wait-ready.sh` | endpoint returned 200 within timeout | timeout | curl missing | — |

For the seed scripts:

| Exit | Meaning |
|------|---------|
| 0 | Seed committed successfully |
| 1 | Uncaught exception (programming error) |
| 2 | Prereq violated (e.g. tenant missing — see `seed_budget.py`) |

## Cross-platform notes

- Scripts target **Git Bash / WSL / Linux / macOS** — POSIX `sh` syntax only.
- On Windows: `bash scripts/bring-up.sh` from Git Bash (the repo's
  `.gitattributes` already converts LF → CRLF, so the scripts work
  from any shell that interprets CRLF).
- `pnpm seed` (script #4 wrapper) requires Node.js ≥ 18 and the
  `apps/example-integration` `pnpm install` step to have run.

## Companion

- `deploy/docker-compose.yml` — the underlying stack
- `apps/example-integration/scripts/bring-up.sh` — narrower
  single-service variant (kept for backwards compat with `pnpm dev`)
- `apps/example-integration/scripts/wait-ready.sh` — original
  pre-root-scripts version, superseded by `scripts/wait-ready.sh`