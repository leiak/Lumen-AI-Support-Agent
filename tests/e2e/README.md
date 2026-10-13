# Lumen AI — End-to-End Tests

End-to-end test suite for the Lumen AI Support Agent surface.

Three flows are covered:

| Flow | Surface | Test | Count |
| ---- | ------- | ---- | ----- |
| A    | Agent workspace SPA (`apps/web`)         | `agent-flow.spec.ts` (Playwright) | 5 tests |
| B    | Customer web widget SDK (`apps/web-sdk`) | `widget-flow.spec.ts` (Playwright) | 4 tests |
| C    | OpenTelemetry distributed tracing (M6)   | `scripts/test_otel_e2e.py` (Python) | 6 steps |

Plus a `globalSetup` that runs once before any Playwright test:
- `alembic upgrade head`
- `tests/e2e/scripts/seed.py` (idempotent demo data)
- Backend `/health` probe
- Widget SDK bundle build + copy into `tests/e2e/static/`

## Quick start

### Prerequisites

Running the Playwright suite end-to-end requires **everything below to already be
running locally**. The Playwright config does NOT spin up Docker /
Postgres / Redis / Qdrant — those are environment-managed services.

1. **Postgres + Redis + Qdrant** on their default ports (`5432`, `6379`,
   `6333`). If any is down, `globalSetup` aborts with a clear message.
2. **The FastAPI backend** (`apps/api/.venv` bootstrapped via `uv`).
3. The **agent SPA dev server** (`apps/web`).
4. The **widget SDK** bundle built at `apps/web-sdk/dist/lumen-widget.js`.

The Playwright config launches all three web servers (api, web, host)
itself, so you don't need to start them by hand — but the databases
must be reachable before the suite starts.

### CORS preconditions

Flow B embeds the SDK from `http://localhost:8080` (a separate origin
from the agent SPA on `:5173`). The backend's CORS allowlist defaults
to `http://localhost:5173,http://localhost:3000` — you MUST add
`http://localhost:8080` for Flow B to mint a widget token:

```dotenv
# apps/api/.env
WIDGET_ALLOWED_ORIGINS_GLOBAL=http://localhost:5173,http://localhost:3000,http://localhost:8080
```

Restart the API after editing `.env`.

### Run the Playwright suite

```bash
# One-time at the repo root:
pnpm install                          # installs @playwright/test
pnpm exec playwright install chromium # downloads the browser

# Every subsequent run (from the repo root):
pnpm e2e
```

The `pnpm e2e` script is `playwright test --config=tests/e2e/playwright.config.ts`.
Output: list + HTML report under `tests/e2e/report/`. Failed runs
keep trace/screenshot/video under `tests/e2e/test-results/`.

To run a single flow:

```bash
pnpm exec playwright test --config=tests/e2e/playwright.config.ts agent-flow
pnpm exec playwright test --config=tests/e2e/playwright.config.ts widget-flow
```

To watch locally:

```bash
pnpm e2e:headed
```

### Run the OpenTelemetry e2e smoke (M6 / Flow C)

The M6 OTel e2e script (`tests/e2e/scripts/test_otel_e2e.py`) drives a
real widget chat through the api container, captures the `X-Trace-Id`
response header, queries Tempo for the trace, and asserts the span
hierarchy + PII-safe attribute keys.

```bash
# Prereqs: bring-up must be running (api + otel-collector + tempo)
scripts/bring-up.sh

# Run the e2e smoke
cd "D:/work-ai/0401-ai-customer" && python tests/e2e/scripts/test_otel_e2e.py

# Or with custom api base
python tests/e2e/scripts/test_otel_e2e.py --api-base http://localhost:8000

# Show all flags
python tests/e2e/scripts/test_otel_e2e.py --help
```

The script runs 6 steps and prints `[OK|WARN|FAIL|INFO]` per step:
1. **infra check** — otel-collector:4317 (TCP) + tempo:33000/ready (HTTP)
2. **send chat** — POST /api/v1/widget/token → captures X-Trace-Id
3. **query Tempo** — GET http://localhost:33000/api/traces/{trace_id}, polls up to 10s
4. **span hierarchy** — assert `http.request` + `llm.chat` + outbound httpx child
5. **PII scan** — whole-key-boundary regex on every attribute key (NOT substring — same as M6.2 static scan)
6. **no orphan spans** — every non-root span has a parent in the trace

Exit codes: 0 = all pass, 1 = at least one step failed, 130 = Ctrl-C.

The WS path (`/api/v1/widget/ws`) is a separate trace tree from the
HTTP `/token` POST — the script searches for it via Tempo's
`/api/search?tags=name=llm.chat` and runs the PII scan against both
traces.

## Demo credentials

Created by `tests/e2e/scripts/seed.py` (idempotent — safe to re-run):

| Role  | Email              | Password   |
| ----- | ------------------ | ---------- |
| agent | `agent@example.com`  | `Demo123!` |
| admin | `admin@example.com`  | `Demo123!` |

These exist ONLY in the local dev database. Production MUST NOT ship
this seed.

## File layout

```
tests/e2e/
├── README.md                    # this file
├── playwright.config.ts         # 3 web servers, 1 project, globalSetup
├── playwright.docker.config.ts  # alternate config (docker network)
├── global-setup.ts              # alembic + seed + health + SDK build
├── global.d.ts                  # type defs
├── agent-flow.spec.ts           # Flow A — 5 tests
├── widget-flow.spec.ts          # Flow B — 4 tests
├── demo-act4.spec.ts            # Stage 11.5 demo recording
├── demo-act4-02.spec.ts         # Stage 13 ticket UI demo
├── agent-flow.spec.ts
├── fixtures/
│   ├── auth.ts                  # loginViaUi / loginViaApi helpers
│   ├── auth-shared.ts           # JWT_STORAGE_KEY constant
│   ├── seed.ts                  # SEED ids + base URL constants
│   └── screenshots.ts           # captureFlow(...)
├── scripts/
│   ├── seed.py                  # Python seed (uses apps.api domain code)
│   ├── seed_budget.py           # tenant_budgets + llm_usage seed
│   ├── seed_demo_rich.py        # 4 SLA + 5 conv + 3 tickets + budget usage
│   ├── explore.py               # 12-round exploratory test harness
│   ├── test_tenant_*.py         # 7 admin-tenant e2e tests
│   ├── test_widget_*.py         # 4 widget e2e tests
│   ├── test_qa_*.py             # 3 QA worker e2e tests
│   ├── test_otel_e2e.py         # Flow C — M6 OpenTelemetry e2e smoke (537 lines)
│   ├── record-demo.sh           # 11 PNG capture
│   └── record-demo.ps1          # Windows variant
├── artifacts/                   # test artifacts (gitignored)
├── report/                      # HTML Playwright report
├── static/                      # widget-host.html + lumen-widget.js
└── tsconfig.json
```

## Known limitations / trade-offs

These are documented as CONCERNS in the task handoff:

1. **No queue UI page.** The agent workspace (Stage 9) doesn't expose
   the unassigned `PENDING` queue as a page — only `/inbox` (assigned
   to me) and the conversation detail. Flow A therefore exercises
   `POST /agents/conversations/{id}/claim` via REST rather than a UI
   "click Claim" button. This is acceptable for M1 because the queue
   endpoint was added in Stage 8.2 but the queue UI is deferred to
   Stage 10+. The test still proves the underlying claim works and
   that the agent can navigate to the claimed conversation.

2. **Mock LLM / AI suggestion timing.** The agent suggest endpoint
   (`POST /agents/conversations/{id}/suggest-reply`) depends on a
   working LLM client + Qdrant + RAG. In environments where any of
   these is slow or absent, the endpoint falls back to
   `FALLBACK_MESSAGE` with `turn_kind="llm_unavailable"` and
   `warning="llm_unavailable"` — see
   `apps/api/src/agent/suggest.py:LLM_UNAVAILABLE_WARNING`. The Flow A
   test accepts EITHER a real suggestion OR this fallback so the
   suite doesn't flake on missing credentials.

3. **CORS allowlist must include port 8080.** See the CORS
   preconditions above. The default allowlist only covers the agent
   SPA + a couple of common dev ports; adding 8080 is a one-line
   `.env` change.

4. **Tests run serially (`workers: 1`).** Both flows mutate shared
   backend state (the seeded conversation + channel); parallel runs
   would race on the claim + WS messages.

5. **Seed lives under `tests/e2e/`, not `apps/api/scripts/`.** The
   project rule was "do not modify apps/api". The seed still imports
   from `apps/api/src/*` (anchored via `sys.path`), so it uses the
   real domain modules — no direct DB access. The downside: the
   `tenant_id` / `user_id` / `channel_id` are fixed strings
   (`01HZDEMO00000000000000000` etc.) rather than fresh ULIDs per
   run. The fixtures expose these as the `SEED` constant so tests
   never hard-code them.

6. **OTel e2e (Flow C) requires bring-up** — does NOT auto-spawn
   `otel-collector` / `tempo` containers. Operator must run
   `scripts/bring-up.sh` (which now includes the OTel stack since
   M6) before running the e2e. If the OTel containers are down, the
   script reports `[FAIL] otel-collector:4317 unreachable` at step 1
   and exits 1.

7. **OTel e2e gates on `ANTHROPIC_API_KEY` not set** — without a
   real LLM key, the chat request will fail at the LLM call but
   still produce an `http.request` span (with `status=ERROR`). The
   script accepts this — it proves the tracing pipeline is wired
   even if the LLM call fails.

## Re-seeding

If you change `tests/e2e/scripts/seed.py` or the demo tenant/user
data needs a reset:

```bash
# Wipe the seed rows (manual SQL — the seed is intentionally NOT
# destructive so an interrupted run can't nuke your local DB):
psql "$DATABASE_URL" -c "
  DELETE FROM messages    WHERE conversation_id = '01HZDEMO00000000000000004';
  DELETE FROM conversations WHERE id = '01HZDEMO00000000000000004';
  DELETE FROM channels    WHERE id = '01HZDEMO00000000000000003';
  DELETE FROM users       WHERE id IN ('01HZDEMO00000000000000001','01HZDEMO00000000000000002');
  DELETE FROM tenants     WHERE id = '01HZDEMO00000000000000000';
"

# Re-run the seed:
cd apps/api && uv run python ../../tests/e2e/scripts/seed.py
```

## CI

In CI, set `CI=1` so:
- `retries: 1` (one retry on flake)
- `forbidOnly: true` (no `test.only()` allowed)
- The `tsc --noEmit` pre-flight in `global-setup.ts` is skipped
  (deployment pipeline runs it independently)

Pass `WIDGET_ALLOWED_ORIGINS_GLOBAL` (or the equivalent override) so
Flow B's cross-origin request to `:8080` is allowed.

The OTel e2e (Flow C) is NOT part of the Playwright CI gate — it
requires the full docker-compose stack including otel-collector +
tempo. Run it manually with `python tests/e2e/scripts/test_otel_e2e.py`
against a deployed stack for spot-checks.

