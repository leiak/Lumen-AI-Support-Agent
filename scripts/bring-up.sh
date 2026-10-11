#!/usr/bin/env bash
# bring-up.sh — one-command full-stack bring-up for Lumen AI.
#
# Spins up the docker-compose stack (postgres + redis + qdrant + minio
# + api + web), waits for the API to be healthy, then runs the seed
# chain in order so the demo tenant has realistic data:
#
#   1. tests/e2e/scripts/seed.py            — base tenant/users/web channel
#   2. tests/e2e/scripts/seed_budget.py     — budget/credits/usage rows
#   3. tests/e2e/scripts/seed_demo_rich.py  — 4 SLA + 5 conv + 3 tickets + events
#   4. apps/example-integration/scripts/extend-seed.py — FEISHU + KB + 2 articles
#
# Idempotent — every step is safe to re-run. Use ``scripts/teardown.sh``
# to remove the stack + named volumes when you want a clean slate.
#
# Usage:
#   scripts/bring-up.sh                # full bring-up (default)
#   scripts/bring-up.sh --no-build     # skip ``docker compose build`` (use cached images)
#   scripts/bring-up.sh --no-seeds     # only bring up services, no seed chain
#   scripts/bring-up.sh --seed-only    # skip docker, only run the seed chain
#
# Exit codes:
#   0  success
#   1  docker compose failure
#   2  API never became ready
#   3  seed chain failure
#
# Companion: scripts/teardown.sh, scripts/wait-ready.sh.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"
API_DIR="$REPO_ROOT/apps/api"
SDK_DIR="$REPO_ROOT/apps/web-sdk"
EXINTEG_DIR="$REPO_ROOT/apps/example-integration"

WITH_BUILD=1
WITH_SEEDS=1
SEED_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --no-build)  WITH_BUILD=0 ;;
    --no-seeds)  WITH_SEEDS=0 ;;
    --seed-only) SEED_ONLY=1 ;;
    -h|--help)
      sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown flag: $arg" >&2
      exit 64
      ;;
  esac
done

step() { printf '\n→ %s\n' "$*"; }
ok()   { printf '✓ %s\n' "$*"; }
fail() { printf '✗ %s\n' "$*" >&2; exit "${2:-1}"; }

# -- Step 1: docker compose up ---------------------------------------
if [[ "$SEED_ONLY" == "0" ]]; then
  step "docker compose up -d (deploy/docker-compose.yml)"
  cd "$DEPLOY_DIR"
  if [[ "$WITH_BUILD" == "1" ]]; then
    docker compose up -d --build
  else
    docker compose up -d
  fi
  ok "compose stack started"

  step "wait for API to be healthy"
  bash "$HERE/wait-ready.sh" http://localhost:8000 120 \
    || fail "API did not become healthy in time" 2
  ok "API healthy at http://localhost:8000"
fi

# -- Step 2: SDK bundle (so e2e widget-host can load lumen-widget.js) --
if [[ "$SEED_ONLY" == "0" ]]; then
  step "build apps/web-sdk bundle"
  cd "$SDK_DIR"
  if [[ ! -d node_modules ]]; then
    pnpm install --prefer-offline
  fi
  pnpm build
  ok "bundle built → apps/web-sdk/dist/lumen-widget.js"
fi

# -- Step 3: seed chain ---------------------------------------------
if [[ "$WITH_SEEDS" == "1" ]]; then
  step "seed chain (4 scripts, idempotent)"
  cd "$API_DIR"

  # Each script anchors apps/api/src onto sys.path itself, so we can
  # invoke them directly without PYTHONPATH plumbing.
  step "  • seed.py — base demo tenant + users + web channel"
  uv run python ../../tests/e2e/scripts/seed.py \
    || fail "seed.py failed" 3

  step "  • seed_budget.py — Pack B budget/credits/usage rows"
  uv run python ../../tests/e2e/scripts/seed_budget.py \
    || fail "seed_budget.py failed" 3

  step "  • seed_demo_rich.py — SLA policies + 5 conv + 3 tickets + events"
  uv run python ../../tests/e2e/scripts/seed_demo_rich.py \
    || fail "seed_demo_rich.py failed" 3

  step "  • extend-seed.py — FEISHU channel + KB + 2 articles"
  cd "$EXINTEG_DIR"
  if [[ ! -d node_modules ]]; then
    pnpm install --prefer-offline
  fi
  pnpm seed || fail "extend-seed.py failed" 3
  ok "all 4 seeds completed"
fi

# -- Final summary -------------------------------------------------
cat <<EOF

✓ bring-up complete
  API:     http://localhost:8000
  Web SPA: http://localhost:8080
  Widget:  apps/example-integration  (pnpm widget)
  Login:   agent@example.com / Demo123!  (admin@example.com also valid)

  Re-run safely — every step is idempotent.
  For a clean slate: scripts/teardown.sh && scripts/bring-up.sh
EOF