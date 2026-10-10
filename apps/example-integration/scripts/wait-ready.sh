#!/usr/bin/env bash
# Poll http://localhost:8000/health/ready until 200 or timeout.
# Usage: ./scripts/wait-ready.sh [api_base_url] [max_seconds]

set -euo pipefail

API="${1:-http://localhost:8000}"
MAX="${2:-90}"

echo "waiting for $API/health/ready (timeout ${MAX}s)…"
for i in $(seq 1 "$MAX"); do
  status=$(curl -s -o /dev/null -w '%{http_code}' "$API/health/ready" || true)
  if [ "$status" = "200" ]; then
    echo "ready after ${i}s"
    exit 0
  fi
  sleep 1
done
echo "TIMEOUT waiting for $API/health/ready" >&2
exit 1
