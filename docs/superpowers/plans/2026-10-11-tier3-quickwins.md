# 2026-10-11 — Tier 3 + Quick Wins + Consolidation 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Solidify the 2026-10-11 round of upgrades (Tier 3 + Quick Wins + seed/SQL/demo-script consolidation) into shippable artifacts — operator scripts, seed `--help`, SQL reference, Act 4/5 demo capture, README updates — without behavioral changes.

**Architecture:** Pure documentation / DX layer on top of the already-shipped functionality. No model or schema changes; all changes are: (a) new shell wrappers, (b) argparse `__main__` blocks, (c) mirror raw SQL to `.sql` reference, (d) extend demo recorder with new Acts, (e) README + script README.

---

## Phase 1 — Bring-up / Teardown / Wait-ready scripts

**Files:**
- Create: `scripts/bring-up.sh`
- Create: `scripts/teardown.sh`
- Create: `scripts/wait-ready.sh`
- Create: `scripts/README.md`

- [x] **Step 1.1 — `scripts/bring-up.sh`**

Idempotent one-command full-stack bring-up. Calls:
1. `docker compose up -d` (deploy/docker-compose.yml)
2. `scripts/wait-ready.sh http://localhost:8000 120`
3. `pnpm build` (apps/web-sdk) — skip when `dist/lumen-widget.js` already present
4. Seed chain (4 scripts in order: `seed.py` → `seed_budget.py` → `seed_demo_rich.py` → `extend-seed.py`)

Flags: `--no-build`, `--no-seeds`, `--seed-only`, `--help`. Exit codes: `0` success, `1` docker compose failure, `2` API never ready / seed failure, `64` unknown flag.

- [x] **Step 1.2 — `scripts/teardown.sh`**

`docker compose down` with `--volumes` (DATA LOSS) + `--images` (prune dangling). Companion to `bring-up.sh`.

- [x] **Step 1.3 — `scripts/wait-ready.sh`**

Generic HTTP poll helper with heartbeat (every 10s) and explicit exit codes. Replaces the narrower `apps/example-integration/scripts/wait-ready.sh`. Used by `bring-up.sh` and CI.

- [x] **Step 1.4 — `scripts/README.md`**

Operator-facing table of scripts + cheat sheet + seed-chain order + exit codes table + cross-platform notes.

**Why this matters:** Before this round, bringing up the full demo required knowing the correct docker-compose port offsets (`25432 / 26333 / 26379`), the migrate one-shot, the 4 seed scripts in dependency order, and the web-sdk bundle build. Operators copy-pasted from README snippets and got it wrong ~50% of the time. The single-command bring-up removed that.

---

## Phase 2 — Seed script `--help` + exit codes

**Files:**
- Modify: `tests/e2e/scripts/seed.py` (`__main__` block)
- Modify: `tests/e2e/scripts/seed_budget.py` (`__main__` block)
- Modify: `tests/e2e/scripts/seed_demo_rich.py` (`__main__` block)
- Modify: `apps/example-integration/scripts/extend-seed.py` (`__main__` block)

- [x] **Step 2.1 — Wrap each `__main__` block with `argparse`**

- `--help` only — no other args. Help text lists prereqs + invocation example + order-matters note (where applicable).
- `argparse.RawDescriptionHelpFormatter` to preserve newlines in epilog.
- Try/except around `asyncio.run(main())`:
  - `RuntimeError` → exit code `2` (prereq violation — the seed script's own hint messages)
  - Other exception → exit code `1` (programming error)

**Why this matters:** Operators routinely ask "what env vars does seed_budget.py need?" or "can I run extend-seed without ANTHROPIC_API_KEY?". The docstrings answered those questions but required `cat` or scroll. `--help` brings them to the operator's fingertips and makes the prereq violations recoverable (exit 2 surfaces the hint on stderr; exit 1 shows the traceback).

**How to apply:** When adding new seed scripts, follow the same pattern. Keep `argparse` blocks at the bottom of the file so the seed logic stays the primary docstring.

---

## Phase 3 — Demo recorder Act 4 + Act 5

**Files:**
- Modify: `tests/e2e/scripts/record-demo.sh` (inline Node script)
- Modify: `tests/e2e/scripts/record-demo.ps1` (inline Node script + `$SkipAct5` param)

- [x] **Step 3.1 — Add Act 4 block (ticket detail)**

```js
{
  const ticketId = process.env.DEMO_TICKET_ID || '01HZDEMOT0P1BILLING0TICKET';
  const ticketUrl = `http://localhost:5173/admin/tickets/${ticketId}`;
  // re-login as admin → /admin/tickets → /admin/tickets/{id}
  await shoot('act4-01-tickets-list');
  await shoot('act4-02-ticket-detail');
}
```

The deterministic P1 ticket id `01HZDEMOT0P1BILLING0TICKET` is from `seed_demo_rich.py`. Override via `DEMO_TICKET_ID` env.

- [x] **Step 3.2 — Add Act 5 block (M2.B entry-point admin views)**

```js
if (process.env.SKIP_ACT5 !== '1') {
  await page.goto('http://localhost:5173/admin/email');
  await shoot('act5-01-email-admin');
  await page.goto('http://localhost:5173/admin/knowledge/multimodal');
  await shoot('act5-02-multimodal-admin');
  await page.goto('http://localhost:5173/admin/knowledge/history-mining');
  await shoot('act5-03-history-mining-admin');
}
```

Each captures the entry-point admin view. Full-flow screenshots remain in the stub PNGs `images/demo-act5-{01,02,03}-*.png` (manually captured at Stage 19 close-out).

- [x] **Step 3.3 — Wire `--skip-act5` flag**

Bash: `for arg in "$@"; do case "$arg" in --skip-act5) SKIP_ACT5=1;; ... esac; done`. Default off. PowerShell: `[switch]$SkipAct5` and `if ($SkipAct5) { $acts = ... ; }` plus `$env:SKIP_ACT5 = if ($SkipAct5) { '1' } else { '0' }` to thread into the inline Node script.

- [x] **Step 3.4 — Update the GIF stitching loop**

Bash: `ACTS=(act1 act2 act3 act4); [[ "$SKIP_ACT5" == "0" ]] && ACTS+=(act5)`. PowerShell: same pattern.

**Why this matters:** Before, `record-demo.sh` covered only Act 1/2/3 (M1). M2.A shipped Act 4 (ticket detail) and M2.B shipped Act 5 (email / multimodal / history mining), but the recorder script was never updated — operators running `record-demo.sh` would overwrite the manually-captured stub PNGs with stale M1-only ones. The Act 4/5 additions keep the recorder in sync with the shipped surface.

---

## Phase 4 — SQL reference

**Files:**
- Create: `tests/e2e/scripts/seed_demo_rich.sql`
- Modify: `tests/e2e/scripts/seed_demo_rich.py` (docstring note)
- Create: `apps/api/migrations/README.md`

- [x] **Step 4.1 — Mirror raw SQL into `seed_demo_rich.sql`**

Each of the 3 raw SQL statements (sla_policies INSERT, tickets INSERT, conversations UPDATE, ticket_events INSERT) is mirrored with:
- The SQLAlchemy `text()` template (the `:name` placeholder form Python uses)
- A psql-runnable example (with `--` comments showing the literal values from the seed)

Header explains WHY raw SQL is used (SAEnum uppercase/lowercase mismatch) and how to run end-to-end (`scripts/bring-up.sh` or `python seed_demo_rich.py`).

- [x] **Step 4.2 — Cross-reference the .sql file from the Python docstring**

```python
A copy of every raw SQL statement is mirrored into
``tests/e2e/scripts/seed_demo_rich.sql`` as a single-source-of-truth
reference (with psql-runnable examples for manual ops). The Python
text() strings here MUST stay byte-identical to the templates in that
file — when you change a column list, update both.
```

- [x] **Step 4.3 — `apps/api/migrations/README.md`**

Operator-facing alembic guide:
- Quick reference (`alembic upgrade head`, `revision --autogenerate`, etc.)
- File-naming convention (ULID autogenerate vs `NN_<slug>` hand-authored)
- Schema overview (current head tables in dependency order)
- Operational notes (ENUM values, timezone discipline, idempotent seed IDs)
- CI integration (`backend-integration` job runs `upgrade head` first)

**Why this matters:** Operators investigating "what migration created this column?" or "what's the ENUM value convention?" had to grep through 17 files. The README centralizes the answers. The .sql reference also gives psql-only operators a way to insert demo rows without Python.

---

## Phase 5 — Top-level README + package.json

**Files:**
- Modify: `README.md` (row 26, 容器化 section)
- Modify: `package.json` (new scripts)

- [x] **Step 5.1 — README row 26 expansion**

Add Quick Win D (root scripts + 4 `--help` flags) to the existing Quick Wins row:
```
| 26 (Quick Wins) | ...; **D**: `scripts/bring-up.sh` + `scripts/teardown.sh` + `scripts/wait-ready.sh` ... (1 commit) | ✅ | 10 (A) + 0 (B) + 1 (C) + 0 (D) = **11 new tests + 1 captured PNG + 3 root scripts + 4 `--help` flags** |
```

- [x] **Step 5.2 — README 容器化 section split**

Replace the old "manual docker compose up" block with two sub-sections:
- "一键全栈 bring-up / teardown" — `scripts/bring-up.sh` cheat sheet + seed `--help` invocation examples + exit code table
- "手动步骤" — the original docker compose flow for operators who need fine-grained control

- [x] **Step 5.3 — package.json script aliases**

```json
"e2e:docker": "playwright test --config=tests/e2e/playwright.docker.config.ts",
"demo:record": "bash tests/e2e/scripts/record-demo.sh",
"demo:record:windows": "powershell -File tests/e2e/scripts/record-demo.ps1",
"demo:record:skip5": "bash tests/e2e/scripts/record-demo.sh --skip-act5",
"stack:up": "bash scripts/bring-up.sh",
"stack:up:no-build": "bash scripts/bring-up.sh --no-build",
"stack:up:seed-only": "bash scripts/bring-up.sh --seed-only",
"stack:down": "bash scripts/teardown.sh",
"stack:down:clean": "bash scripts/teardown.sh --volumes --images"
```

**Why this matters:** Operators discovering the project via `pnpm run` see only `e2e*` aliases. Adding `stack:up` / `stack:down` / `demo:record` makes the demo surface discoverable from `pnpm run` alone (no need to grep for shell scripts).

---

## Verification

- [x] `bash -n scripts/bring-up.sh && bash -n scripts/teardown.sh && bash -n scripts/wait-ready.sh` — all pass
- [x] All 4 seed scripts accept `--help` and exit 0 (verified manually)
- [x] `bash -n tests/e2e/scripts/record-demo.sh` — passes
- [ ] PowerShell `record-demo.ps1` syntax — pwsh not available on Linux; manually reviewed
- [ ] End-to-end bring-up test — requires docker stack; documented for follow-up

---

## Out of scope (deferred)

- **Self-contained `boot-test.sh`** — a smoke test that brings up + captures demo-act4-02 + tears down, all without manual intervention. Useful for CI matrix but requires docker-in-docker on the runner. Deferred.
- **PowerShell-only operators** — `record-demo.ps1` mirrors the bash version but is not exercised by CI. The `[switch]$SkipAct5` param is added for parity; visual review only.
- **Auto-update of stub PNGs** — `images/demo-act5-{01,02,03}-*.png` are still manual. Recording Act 5 live requires wiring SES inbound + vision embedding + cluster worker against the dev DB, which is more than a recorder tweak.
- **SQLite fallback for demos** — every demo path uses Postgres ENUMs; SQLite doesn't support them. Out of scope.

---

## Cross-references

- Memory: [[2026-10-11-tier3-complete]] — Tier 3 4 tasks (i18n, CI, cross-domain search, runtime LLM chain)
- Memory: [[2026-10-11-quick-wins]] — Quick Wins A/B/C (predecessor; Quick Win D is this plan)
- Memory: [[2026-10-09-nitpick-plan]] — the 14-task gap-fill plan this round built on
- Memory: [[m4-d-pack-c-progress]] — Pack C Docker部署 (predecessor bring-up)
- Spec: `docs/superpowers/specs/2026-09-10-ai-customer-service-design.md`