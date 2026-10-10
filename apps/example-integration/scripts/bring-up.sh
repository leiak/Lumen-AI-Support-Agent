#!/usr/bin/env bash
# One-command bring-up: docker compose up → wait ready → seed → extend-seed.
# Usage: ./scripts/bring-up.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXINTEG="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$EXINTEG/../.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"
API_DIR="$REPO_ROOT/apps/api"
SDK_DIR="$REPO_ROOT/apps/web-sdk"

cd "$DEPLOY_DIR"
echo "→ docker compose up -d"
docker compose up -d --build

echo "→ wait for API"
"$HERE/wait-ready.sh" http://localhost:8000 90

echo "→ apps/web-sdk build"
cd "$SDK_DIR"
pnpm install --prefer-offline
pnpm build

echo "→ seed.py (demo tenant/users/web channel)"
cd "$API_DIR"
uv run python ../../tests/e2e/scripts/seed.py

echo "→ example-integration seed extension"
cd "$EXINTEG"
pnpm install --prefer-offline
pnpm seed

echo
echo "✓ bring-up complete."
echo "  API:    http://localhost:8000"
echo "  widget: cd apps/example-integration && pnpm widget"
echo "  api:    cd apps/example-integration && pnpm api-client:login"
echo "  hook:   cd apps/example-integration && pnpm webhook:start"
