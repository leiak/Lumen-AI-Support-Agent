#!/usr/bin/env bash
# wait-ready.sh — poll an HTTP endpoint until it returns 200 or timeout.
#
# Usage:
#   scripts/wait-ready.sh                        # http://localhost:8000/health/ready, 120s
#   scripts/wait-ready.sh http://localhost:8000  # same path, custom host
#   scripts/wait-ready.sh http://localhost:8080/health/live 90
#
# Exit codes:
#   0  endpoint returned 200 within timeout
#   1  timed out
#   2  curl missing (very unlikely — curl is in the Git-for-Windows SDK)
#
# Compatible with Linux + macOS + Git Bash. Used by bring-up.sh and
# tests/e2e/scripts/verify_three_integrations.py callers.

set -euo pipefail

API="${1:-http://localhost:8000}"
MAX="${2:-120}"
# Default to /health/ready when caller passed only the origin.
if [[ "$API" != *"/health/"* ]]; then
  API="$API/health/ready"
fi

printf 'waiting for %s (timeout %ss)…\n' "$API" "$MAX"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl not found in PATH — install curl and retry" >&2
  exit 2
fi

start=$SECONDS
for i in $(seq 1 "$MAX"); do
  status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$API" || echo "000")
  if [ "$status" = "200" ]; then
    elapsed=$((SECONDS - start))
    printf 'ready after %ss (HTTP 200)\n' "$elapsed"
    exit 0
  fi
  # Print a heartbeat every 10s so a long wait doesn't look hung.
  if (( i % 10 == 0 )); then
    printf '  …still waiting (%ss, last status %s)\n' "$i" "$status"
  fi
  sleep 1
done

printf 'TIMEOUT after %ss waiting for %s (last status %s)\n' "$MAX" "$API" "$status" >&2
exit 1