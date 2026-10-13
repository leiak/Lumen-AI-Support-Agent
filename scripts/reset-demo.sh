#!/usr/bin/env bash
# reset-demo.sh — restore the demo tenant + its fixtures, leave
# everything else alone.
#
# When this is useful: another session's test fixture wiped the demo
# tenant (`01HZDEMO00000000000000000`) or its admin user out from
# under you. `bring-up.sh --seed-only` is idempotent and the underlying
# seed scripts use fixed primary keys, so re-running it is safe even
# if the demo data already exists. The point of THIS script is:
#
#   1. Don't accidentally `docker compose down -v` (you'd lose every
#      other tenant — BYOK tests, pipeline-tenant fixtures, etc.)
#   2. Don't `TRUNCATE` by accident (same reason)
#   3. Print a clear before/after so you can confirm the demo row is
#      back before you try to log in again
#
# Usage:
#   scripts/reset-demo.sh              # full reset (default)
#   scripts/reset-demo.sh --no-verify  # skip the post-run row check
#
# Exit codes:
#   0  demo tenant + admin user are present after the seed chain
#   1  docker compose failure
#   2  seed chain failure
#   3  post-run verification failed (seed ran but demo row missing)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

DEMO_TENANT_ID="01HZDEMO00000000000000000"
DEMO_ADMIN_EMAIL="admin@example.com"
PG_CONTAINER="${PG_CONTAINER:-aicustomer-postgres-1}"
PG_USER="${PG_USER:-aicustomer}"
PG_DB="${PG_DB:-aicustomer}"

VERIFY=1
for arg in "$@"; do
  case "$arg" in
    --no-verify) VERIFY=0 ;;
    -h|--help)
      sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

cd "$REPO_ROOT"

echo ">>> reset-demo: re-running the seed chain (idempotent)..."
echo "    demo tenant_id = $DEMO_TENANT_ID"
echo "    demo admin     = $DEMO_ADMIN_EMAIL"
echo
echo "    NOTE: this only re-runs the existing idempotent seed scripts."
echo "          It does NOT touch other tenants, BYOK fixtures, or any"
echo "          tenant created outside the seed chain."
echo

# set -e + a failing subshell can abort before we even see the rc; capture
# with `|| true` so we always get a value and can decide what to do below.
SEED_RC=0
bash "$HERE/bring-up.sh" --seed-only || SEED_RC=$?

if [ "$SEED_RC" -ne 0 ]; then
  # bring-up.sh's --seed-only still aborts on the final extend-seed
  # step on Windows (calls `python3` instead of `python`). The first
  # three seed scripts — the ones that actually create Acme Demo and
  # its users — succeed before that failure, so we treat exit code 3
  # as a soft warning and continue to verify.
  if [ "$SEED_RC" -eq 3 ]; then
    echo
    echo ">>> NOTE: extend-seed.py failed (Windows: no \`python3\`)."
    echo "    The 3 demo seeds that matter (tenant/users/budget/rich"
    echo "    data) all ran. Skipping just that step is fine here."
    echo
  else
    echo ">>> reset-demo: seed chain failed (rc=$SEED_RC)" >&2
    exit "$SEED_RC"
  fi
fi

if [ "$VERIFY" -eq 0 ]; then
  echo ">>> reset-demo: --no-verify set, skipping row check."
  exit 0
fi

echo ">>> reset-demo: verifying demo tenant + admin row are back..."
TENANT_ROWS=$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -At \
  -c "SELECT count(*) FROM tenants WHERE id='$DEMO_TENANT_ID';")
ADMIN_ROWS=$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -At \
  -c "SELECT count(*) FROM users WHERE tenant_id='$DEMO_TENANT_ID' AND email='$DEMO_ADMIN_EMAIL';")

echo "    tenants where id='$DEMO_TENANT_ID'        : $TENANT_ROWS"
echo "    users    where tenant_id='$DEMO_TENANT_ID' : $ADMIN_ROWS"
echo

if [ "$TENANT_ROWS" -lt 1 ] || [ "$ADMIN_ROWS" -lt 1 ]; then
  echo ">>> reset-demo: FAILED — demo row missing after seed." >&2
  echo "    Try: docker exec -it $PG_CONTAINER psql -U $PG_USER -d $PG_DB" >&2
  echo "    then: SELECT id, name FROM tenants ORDER BY created_at DESC LIMIT 5;" >&2
  exit 3
fi

echo ">>> reset-demo: OK — Acme Demo tenant + admin user are back."
echo "    Log in: admin@example.com / Demo123!  (X-Tenant-Id auto-resolved)"
echo "    Or for the agent view: agent@example.com / Demo123!"
