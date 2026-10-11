#!/usr/bin/env bash
#
# Stage 11.5 + 15 + 19: Lumen AI demo screenshot recorder.
#
# Drives the same flows as the Playwright E2E suite
# (tests/e2e/agent-flow.spec.ts + widget-flow.spec.ts +
# demo-act4-02.spec.ts) but instead of asserting, takes a per-step PNG
# into ../images/ for inclusion in stakeholder docs and
# sales-engineer screen-shares.
#
# Acts captured (see docs/demo-script.md for the narrative):
#   Act 1: customer widget      → 3 PNGs
#   Act 2: agent workspace      → 5 PNGs
#   Act 3: admin KB + upload    → 3 PNGs
#   Act 4: admin ticket detail  → 2 PNGs (M2.A; #01 + #02 from real spec)
#   Act 5: email + multimodal + history mining → 3 PNGs (M2.B; stubs
#           live at images/demo-act5-{01,02,03}-*.png — Act 5 captures
#           rely on admin pages that need real SES / vision / cluster
#           wiring; record only the entry-point admin views here)
#
# Why a separate shell script (not a new spec)
# ---------------------------------------------
# The existing specs are written as **assertion tests** — they fail
# the build if anything regresses. The recorder is **capture**, not a
# test: it must produce every screenshot even when a button is in an
# unexpected state, so the presenter has the full story.
#
# Reusing the live webServer setup from playwright.config.ts would
# tightly couple capture to the assertion suite. Instead this script:
#
#   1. Assumes the three dev servers (api, web, widget-host) are
#      already running — same as the e2e suite's runtime requirements.
#   2. Drives a small headless Chromium through Playwright's Node API
#      directly (no test runner), one describe-block per Act.
#   3. Writes ``images/demo-act{N}-{step}.png`` next to the existing
#      hand-captured ``01_login.png`` … ``13_widget_chat_active.png``
#      set. The ``demo-`` prefix separates the two sources.
#
# Deliverable: ~16 PNGs. NOT a stitched mp4 — that needs a presenter +
# audio track; the script only automates the clicks.
#
# Optional ffmpeg stitching: if ``ffmpeg`` is on PATH, the script also
# emits ``images/demo-act{N}.gif`` (one animated GIF per Act). Install
# ffmpeg separately (``brew install ffmpeg`` / ``apt install ffmpeg``);
# not a hard requirement, the PNGs alone are the deliverable.
#
# Usage
# -----
#   # Prereqs (same as e2e suite): Postgres + Redis + Qdrant up,
#   # alembic upgrade head, scripts/bring-up.sh (so all 4 seeds run),
#   # web-sdk built, three dev servers (api/web/widget-host) reachable.
#   cd tests/e2e
#   bash scripts/record-demo.sh                # produce ~16 PNGs
#   bash scripts/record-demo.sh --gif          # also produce GIFs
#   bash scripts/record-demo.sh --skip-act5    # only capture M1 + M2.A
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TESTS_E2E_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${TESTS_E2E_DIR}/../.." && pwd)"
IMAGES_DIR="${REPO_ROOT}/images"

mkdir -p "${IMAGES_DIR}"

# Sanity: confirm Playwright is installed where Node can find it.
if ! command -v node >/dev/null 2>&1; then
  echo "[record-demo] Node.js is required (Playwright's Node API)."
  echo "              Install Node 20+ then re-run."
  exit 1
fi

if ! node -e "require.resolve('@playwright/test')" >/dev/null 2>&1; then
  echo "[record-demo] @playwright/test not installed at repo root."
  echo "              Run 'pnpm install --frozen-lockfile' then re-run."
  exit 1
fi

# Optional ffmpeg check (not a hard failure — the PNGs alone are the
# deliverable). When present we stitch per-Act GIFs at the end.
WITH_GIF=0
SKIP_ACT5=0
for arg in "$@"; do
  case "$arg" in
    --gif)        WITH_GIF=1 ;;
    --skip-act5)  SKIP_ACT5=1 ;;
    --help|-h)
      sed -n '2,55p' "$0"
      exit 0
      ;;
    *)
      echo "[record-demo] unknown flag: $arg" >&2
      exit 64
      ;;
  esac
done
if [[ "${WITH_GIF}" == "1" ]] && ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[record-demo] --gif requested but ffmpeg not found on PATH; skipping GIF step."
  WITH_GIF=0
fi

# Wait for the three dev servers the suite normally launches. They may
# already be running if you've been iterating with the e2e suite — in
# which case the probes return immediately.
wait_for_url() {
  local url="$1"
  local name="$2"
  for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null -m 2 "$url" 2>/dev/null; then
      echo "[record-demo] ${name}: up"
      return 0
    fi
    sleep 1
  done
  echo "[record-demo] ${name}: not reachable at ${url}" >&2
  return 1
}

wait_for_url "http://localhost:8000/health/live" "api (8000)"
wait_for_url "http://localhost:5173"               "web (5173)"
wait_for_url "http://localhost:8080/widget-host.html" "widget-host (8080)"

# Drive Chromium via the Playwright Node API. We embed the JS in a
# heredoc so the script is self-contained — no separate .ts file to
# compile.
node --input-type=module - <<'NODE'
import { chromium } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const IMAGES_DIR = path.resolve(__dirname, '../../../../images');

const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 } });
const page = await ctx.newPage();

const shoot = async (name) => {
  const file = path.join(IMAGES_DIR, `demo-${name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  console.log(`[record-demo] captured ${file}`);
};

// ---- Act 1: customer widget ------------------------------------------
{
  await page.goto('http://localhost:8080/widget-host.html');
  await page.waitForTimeout(1000);
  await shoot('act1-01-widget-closed');

  // Click the launcher bubble.
  await page.locator('lumen-widget').click();
  await page.waitForTimeout(800);
  await shoot('act1-02-widget-open');

  // Type a customer question and send it.
  const composer = page.frameLocator('iframe').locator('textarea, input[type="text"]').first();
  await composer.fill('How do I reset my password?');
  await composer.press('Enter');
  await page.waitForTimeout(3500);
  await shoot('act1-03-widget-chat-active');
}

// ---- Act 2: agent workspace -----------------------------------------
{
  await page.goto('http://localhost:5173/login');
  await page.waitForTimeout(500);
  await page.locator('input[type="email"]').fill('agent@demo.test');
  await page.locator('input[type="password"]').fill('Demo123!');
  await shoot('act2-01-login-filled');

  await page.locator('button[type="submit"]').click();
  await page.waitForURL(/\/inbox/, { timeout: 8000 });
  await page.waitForTimeout(500);
  await shoot('act2-02-inbox');

  await page.locator('a:has-text("Open"), tr').first().click();
  await page.waitForTimeout(1000);
  await shoot('act2-03-conversation-detail');

  // Click the "Suggest reply" affordance.
  await page.locator('button:has-text("Suggest")').first().click().catch(() => {});
  await page.waitForTimeout(3000);
  await shoot('act2-04-ai-suggestion');

  // Send a reply.
  await page.locator('textarea').last().fill('I have reset your password. Please check your inbox.');
  await page.locator('button:has-text("Send")').first().click();
  await page.waitForTimeout(1500);
  await shoot('act2-05-send-reply');
}

// ---- Act 3: admin KB --------------------------------------------------
{
  // Log out + log back in as admin.
  await page.goto('http://localhost:5173/login');
  await page.locator('input[type="email"]').fill('admin@demo.test');
  await page.locator('input[type="password"]').fill('Demo123!');
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(/\/(inbox|kb|settings)/, { timeout: 8000 });

  await page.goto('http://localhost:5173/settings');
  await page.waitForTimeout(1000);
  await shoot('act3-01-settings');

  await page.goto('http://localhost:5173/kb');
  await page.waitForTimeout(1000);
  await shoot('act3-02-kb-list');

  // Upload demo PDF.
  const sample = path.resolve(__dirname, '../../../../docs/sample-docs/new-feature.pdf');
  await page.locator('input[type="file"]').first().setInputFiles(sample);
  await page.waitForTimeout(2500);
  await shoot('act3-03-after-upload');
}

// ---- Act 4: admin ticket detail (M2.A) ------------------------------
// Stage 20 #20 shipped the admin ticket detail page
// (`apps/web/src/pages/admin-ticket-detail.tsx`). The deterministic
// P1 billing ticket id is fixed by seed_demo_rich.py — override via
// DEMO_TICKET_ID env if the rich seed evolves.
{
  const ticketId = process.env.DEMO_TICKET_ID || '01HZDEMOT0P1BILLING0TICKET';
  const ticketUrl = `http://localhost:5173/admin/tickets/${ticketId}`;

  // The page is admin-gated — re-login with the admin user.
  await page.goto('http://localhost:5173/login');
  await page.locator('input[type="email"]').fill('admin@demo.test');
  await page.locator('input[type="password"]').fill('Demo123!');
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(/\/(inbox|kb|settings)/, { timeout: 8000 });

  // Tickets list — capture the entry point.
  await page.goto('http://localhost:5173/admin/tickets');
  await page.waitForTimeout(1000);
  await shoot('act4-01-tickets-list');

  // Single-ticket detail page (the same flow as demo-act4-02.spec.ts,
  // which produced the canonical `images/demo-act4-02.png` artifact —
  // this recorder step just refreshes it without spec overhead).
  await page.goto(ticketUrl);
  await page.waitForTimeout(1500);
  await shoot('act4-02-ticket-detail');
}

if (process.env.SKIP_ACT5 !== '1') {
  // ---- Act 5: M2.B surfaces ------------------------------------------
  // 5-01 — Email channel admin (SES webhook + reply log)
  // 5-02 — Multimodal KB admin (vision collection + RRF retrieval)
  // 5-03 — History mining admin (clustered KB drafts + approve/reject)
  //
  // Each route is admin-gated; we re-login (already admin from Act 4)
  // and capture the entry-point view. Full flow screenshots live in
  // the stub PNGs `images/demo-act5-{01,02,03}-*.png` — those were
  // captured manually (Stage 19 close-out) and committed; the recorder
  // refreshes the live page so changes to the layout surface in CI.
  await page.goto('http://localhost:5173/admin/email');
  await page.waitForTimeout(1000);
  await shoot('act5-01-email-admin');

  await page.goto('http://localhost:5173/admin/knowledge/multimodal');
  await page.waitForTimeout(1000);
  await shoot('act5-02-multimodal-admin');

  await page.goto('http://localhost:5173/admin/knowledge/history-mining');
  await page.waitForTimeout(1000);
  await shoot('act5-03-history-mining-admin');
}

await browser.close();
console.log('[record-demo] done');
NODE

# ---- Optional GIF stitching -----------------------------------------
ACTS=(act1 act2 act3 act4)
if [[ "${SKIP_ACT5}" == "0" ]]; then
  ACTS+=(act5)
fi
if [[ "${WITH_GIF}" == "1" ]]; then
  for act in "${ACTS[@]}"; do
    out="${IMAGES_DIR}/demo-${act}.gif"
    ffmpeg -y -framerate 1 -pattern_type glob -i "${IMAGES_DIR}/demo-${act}-*.png" \
      -vf "fps=1,scale=1024:-1:flags=lanczos" "${out}" 2>/dev/null
    echo "[record-demo] stitched ${out}"
  done
fi

echo "[record-demo] wrote $(ls -1 ${IMAGES_DIR}/demo-*.png | wc -l) PNGs to ${IMAGES_DIR}/"
