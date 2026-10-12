#!/bin/bash
# Reusable wrapper for tests/e2e/scripts/explore.py.
# Reads the API key from the running api container so the operator never
# has to copy/paste it. Requires the api container to be up.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

KEY=$(docker exec aicustomer-api-1 printenv ANTHROPIC_API_KEY)
DB_URL="postgresql+asyncpg://aicustomer:aicustomer@localhost:25432/aicustomer"

DATABASE_URL="$DB_URL" \
REDIS_URL="redis://localhost:26379/0" \
JWT_SECRET="dev-only-change-me-please-32chars-min-aaaa" \
ANTHROPIC_API_KEY="$KEY" \
ANTHROPIC_BASE_URL="https://api.minimaxi.com/anthropic" \
python tests/e2e/scripts/explore.py "$@"