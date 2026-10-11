#!/usr/bin/env bash
# teardown.sh — clean shutdown of the Lumen AI demo stack.
#
# Removes the docker-compose stack and (with --volumes) the named
# volumes so Postgres / Redis / Qdrant / MinIO start empty next time.
#
# Usage:
#   scripts/teardown.sh                # stop containers, keep volumes
#   scripts/teardown.sh --volumes      # also wipe named volumes (DATA LOSS)
#   scripts/teardown.sh --images       # also prune dangling images
#
# Exit codes:
#   0  success
#   1  docker compose failure
#
# Companion: scripts/bring-up.sh, scripts/wait-ready.sh.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"

WITH_VOLUMES=0
WITH_IMAGES=0

for arg in "$@"; do
  case "$arg" in
    --volumes) WITH_VOLUMES=1 ;;
    --images)  WITH_IMAGES=1 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown flag: $arg" >&2
      exit 64
      ;;
  esac
done

step() { printf '\n→ %s\n' "$*"; }
fail() { printf '✗ %s\n' "$*" >&2; exit "${2:-1}"; }

cd "$DEPLOY_DIR"

step "docker compose down"
if [[ "$WITH_VOLUMES" == "1" ]]; then
  printf '⚠ --volumes set — Postgres / Redis / Qdrant / MinIO data will be wiped\n'
  docker compose down --volumes || fail "docker compose down --volumes failed" 1
else
  docker compose down || fail "docker compose down failed" 1
fi
printf '✓ compose stack stopped\n'

if [[ "$WITH_IMAGES" == "1" ]]; then
  step "prune dangling Lumen images"
  # ``docker image prune`` removes only dangling (untagged) images so the
  # build cache from the last ``compose build`` survives. We pass the
  # api/web images explicitly via the filter just to be safe — anything
  # tagged ``aicustomer-*`` or ``<none>`` matching the compose labels is
  # in scope.
  docker image prune -f || fail "docker image prune failed" 1
  printf '✓ dangling images pruned\n'
fi

if [[ "$WITH_VOLUMES" == "1" && "$WITH_IMAGES" == "1" ]]; then
  SUMMARY="  - compose stack stopped
  - named volumes wiped (DATA LOSS)
  - dangling images pruned"
elif [[ "$WITH_VOLUMES" == "1" ]]; then
  SUMMARY="  - compose stack stopped
  - named volumes wiped (DATA LOSS)"
elif [[ "$WITH_IMAGES" == "1" ]]; then
  SUMMARY="  - compose stack stopped
  - dangling images pruned"
else
  SUMMARY="  - compose stack stopped
  - volumes kept (re-run bring-up.sh for a clean slate)"
fi

cat <<EOF

✓ teardown complete
${SUMMARY}

  To bring the stack back: scripts/bring-up.sh
EOF