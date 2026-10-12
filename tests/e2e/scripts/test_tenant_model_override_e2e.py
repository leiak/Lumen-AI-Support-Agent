"""End-to-end verification of the M5.2 per-tenant LLM model override.

Drives the live API container end-to-end against the
``/api/v1/admin/tenants/{id}/llm-configs`` endpoints and confirms:

1. A fresh tenant can be minted and a ``TenantLLMConfig`` row created
   with a per-tenant ``model`` override via the admin POST.
2. The admin GET reflects the ``model`` field as written.
3. PATCH ``model`` updates the override (round-trip).
4. PATCH ``model=""`` clears the override to ``NULL`` (frontend-friendly).
5. DELETE removes the row from the DB (idempotent on second call).
6. The DB row is gone after DELETE.

Mirrors ``tests/e2e/scripts/test_tenant_fallback_chain_e2e.py``'s
argparse + ``[OK|WARN|FAIL|INFO]`` print conventions. Exit codes:

* ``0`` — all checks passed
* ``1`` — uncaught exception (traceback on stderr)
* ``2`` — prereq violation (missing dep / missing ``DATABASE_URL`` /
  ``JWT_SECRET`` / API unreachable). Operator action hint is printed
  before the exit.

Run from repo root with the docker stack up and alembic migrations
applied (the ``tenant_llm_configs`` table with the new ``model``
column must be reachable)::

    DATABASE_URL=... JWT_SECRET=... \\
    python tests/e2e/scripts/test_tenant_model_override_e2e.py \\
        --api-base http://localhost:8000

The script is self-contained — it does not require ``ANTHROPIC_API_KEY``
(no chat messages are sent). It only exercises the admin endpoints +
the underlying repo's upsert / update / delete behaviour.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import requests

# --- Repo-root + sys.path bootstrap -------------------------------------


def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "apps" / "api" / "src").is_dir() and (
            candidate / "tests" / "e2e"
        ).is_dir():
            return candidate
    raise RuntimeError("could not locate repo root")


_API_SRC = _find_repo_root(Path(__file__)) / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))


# Late imports — must come after sys.path tweak above.
from auth.jwt import create_access_token  # noqa: E402
from core.database import reset_engine, reset_sessionmaker  # noqa: E402
from llm_client.tenant_config_models import TenantLLMConfigRepository  # noqa: E402
from tenant.enums import TenantPlan, TenantStatus  # noqa: E402
from tenant.repository import TenantRepository  # noqa: E402

# A stable opaque key string — admin endpoint accepts anything
# non-empty and Fernet-encrypts it at rest; the e2e doesn't verify
# the LLM call itself, just the schema persistence + round-trip.
PLAINTEXT_KEY = "sk-m5-2-e2e-not-real-do-not-use-in-prod"


# --- Config -------------------------------------------------------------


def _print(level: str, msg: str) -> None:
    """level ∈ {OK, WARN, FAIL, INFO}; Windows-GBK safe."""
    line = f"  [{level}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((line + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()


# --- Test plan ----------------------------------------------------------


async def _seed_tenant() -> str:
    """Mint a fresh ACTIVE tenant for the run. No BYOK needed (admin only)."""
    repo = TenantRepository()
    tenant = await repo.create(
        name="verify-model-override",
        plan=TenantPlan.PRO,
        status=TenantStatus.ACTIVE,
    )
    return tenant.id


def _mint_admin_token(tenant_id: str) -> str:
    """Mint a fresh admin JWT for ``tenant_id`` via the project's JWT helper."""
    return create_access_token(
        tenant_id=tenant_id,
        user_id=f"verify-m5-2-{tenant_id}",
        role="admin",
    )


def _check_api_up(api_base: str) -> bool:
    """Verify the live API is reachable before driving it."""
    try:
        r = requests.get(f"{api_base}/health/live", timeout=5)
        return r.status_code == 200
    except requests.RequestException as exc:
        _print("FAIL", f"API /health/live unreachable: {type(exc).__name__}: {exc}")
        return False


def _step1_post_with_model(
    api_base: str, tenant_id: str, token: str,
) -> bool:
    """POST upserts a row with model=claude-haiku-4-5 — body must echo model."""
    payload = {
        "provider_name": "anthropic",
        "api_key": PLAINTEXT_KEY,
        "model": "claude-haiku-4-5",
    }
    try:
        r = requests.post(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"POST (with model): {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 201:
        _print("FAIL", f"POST (with model) status={r.status_code} body={r.text[:200]!r}")
        return False
    body = r.json()
    if body.get("model") != "claude-haiku-4-5":
        _print("FAIL", f"POST (with model): body.model != claude-haiku-4-5 — {body!r}")
        return False
    if body.get("provider_name") != "anthropic":
        _print("FAIL", f"POST (with model): body.provider_name != anthropic — {body!r}")
        return False
    _print("OK", "POST /admin/.../llm-configs 201 + model=claude-haiku-4-5 echoed")
    return True


def _step2_get_returns_model(
    api_base: str, tenant_id: str, token: str,
) -> bool:
    """GET must return the same row with model=claude-haiku-4-5."""
    try:
        r = requests.get(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"GET: {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 200:
        _print("FAIL", f"GET status={r.status_code} body={r.text[:200]!r}")
        return False
    rows = r.json()
    if len(rows) != 1:
        _print("FAIL", f"GET: expected 1 row, got {len(rows)} — {rows!r}")
        return False
    if rows[0].get("model") != "claude-haiku-4-5":
        _print(
            "FAIL",
            f"GET: row.model != claude-haiku-4-5 — {rows[0]!r}",
        )
        return False
    _print("OK", "GET returned 1 row with model=claude-haiku-4-5")
    return True


def _step3_patch_updates_model(
    api_base: str, tenant_id: str, token: str,
) -> bool:
    """PATCH with a new model value must update and echo it back."""
    payload = {"model": "claude-3-5-sonnet-20241022"}
    try:
        r = requests.patch(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs/anthropic",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"PATCH (update): {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 200:
        _print("FAIL", f"PATCH (update) status={r.status_code} body={r.text[:200]!r}")
        return False
    body = r.json()
    if body.get("model") != "claude-3-5-sonnet-20241022":
        _print("FAIL", f"PATCH (update): body.model != new value — {body!r}")
        return False
    _print("OK", "PATCH model=claude-3-5-sonnet-20241022 echoed in 200 response")
    return True


def _step4_patch_empty_string_clears(
    api_base: str, tenant_id: str, token: str,
) -> bool:
    """PATCH model='' must clear the column back to NULL."""
    payload = {"model": ""}
    try:
        r = requests.patch(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs/anthropic",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"PATCH (clear): {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 200:
        _print("FAIL", f"PATCH (clear) status={r.status_code} body={r.text[:200]!r}")
        return False
    body = r.json()
    if body.get("model") is not None:
        _print(
            "FAIL",
            f"PATCH (clear): body.model should be None — got {body.get('model')!r}",
        )
        return False
    _print("OK", "PATCH model='' cleared to NULL (frontend-friendly path)")
    return True


def _step5_delete_removes_row(
    api_base: str, tenant_id: str, token: str,
) -> bool:
    """DELETE → 204; second DELETE also 204 (idempotent)."""
    try:
        r = requests.delete(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs/anthropic",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"DELETE: {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 204:
        _print("FAIL", f"DELETE status={r.status_code} body={r.text[:200]!r}")
        return False
    try:
        again = requests.delete(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs/anthropic",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"DELETE (2nd): {type(exc).__name__}: {exc}")
        return False
    if again.status_code != 204:
        _print(
            "FAIL",
            f"DELETE (2nd) status={again.status_code} body={again.text[:200]!r}",
        )
        return False
    _print("OK", "DELETE → 204 (idempotent on second call)")
    return True


async def _step6_db_row_gone(tenant_id: str) -> bool:
    """Repo must report no rows for (tenant, anthropic) after DELETE."""
    repo = TenantLLMConfigRepository()
    rows = await repo.list_by_tenant(tenant_id, enabled_only=False)
    if rows:
        _print("FAIL", f"DB rows still present after DELETE: {rows!r}")
        return False
    _print("OK", "DB rows gone after DELETE (repo.list_by_tenant is empty)")
    return True


# --- Main ---------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    api_base = args.api_base.rstrip("/")
    print("=" * 70)
    print("M5.2 tenant model override — E2E")
    print("=" * 70)
    print(f"  api_base = {api_base}")
    print(f"  db_url   = {(os.environ.get('DATABASE_URL') or '')[:80]!r}")

    # Pre-flight: API reachable?
    if not _check_api_up(api_base):
        _print(
            "FAIL",
            f"API not reachable at {api_base}/health/live. "
            "Bring the docker stack up (scripts/bring-up.sh) before running.",
        )
        return 2

    # Pre-flight: DB env present (the script opens its own session).
    if not os.environ.get("DATABASE_URL"):
        _print(
            "FAIL",
            "DATABASE_URL not set — required to drive the config repo "
            "+ admin endpoint auth.",
        )
        return 2

    # Pre-flight: JWT secret present (create_access_token reads it from settings).
    if not os.environ.get("JWT_SECRET"):
        _print(
            "FAIL",
            "JWT_SECRET not set — required to mint admin JWT.",
        )
        return 2

    overall = True
    try:
        tenant_id = await _seed_tenant()
        _print("OK", f"seeded tenant_id={tenant_id}")

        token = _mint_admin_token(tenant_id=tenant_id)
        _print("OK", "minted admin JWT for tenant")

        overall &= _step1_post_with_model(api_base, tenant_id, token)
        overall &= _step2_get_returns_model(api_base, tenant_id, token)
        overall &= _step3_patch_updates_model(api_base, tenant_id, token)
        overall &= _step4_patch_empty_string_clears(api_base, tenant_id, token)
        overall &= _step5_delete_removes_row(api_base, tenant_id, token)
        overall &= await _step6_db_row_gone(tenant_id)
    except requests.RequestException as exc:
        _print(
            "FAIL",
            f"HTTP layer failed: {type(exc).__name__}: {exc} — "
            "is the docker stack up?",
        )
        return 2
    except Exception as exc:
        _print("FAIL", f"unexpected crash: {type(exc).__name__}: {exc}")
        return 1
    finally:
        reset_engine()
        reset_sessionmaker()

    print("=" * 70)
    print(f"  RESULT: {'ALL E2E STEPS PASS' if overall else 'AT LEAST ONE STEP FAILED'}")
    print("=" * 70)
    return 0 if overall else 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="test_tenant_model_override_e2e.py",
        description=(
            "End-to-end smoke for the M5.2 per-tenant LLM model override "
            "field on the admin /api/v1/admin/tenants/{id}/llm-configs "
            "endpoints. Runs against a live api container and exercises "
            "POST (with model) / GET / PATCH (update + clear-with-empty-string) "
            "/ DELETE."
        ),
        epilog=(
            "Prereqs:\n"
            "  - docker compose stack up (or the api service running on\n"
            "    --api-base)\n"
            "  - DATABASE_URL env var set (Postgres reachable, alembic\n"
            "    migrations applied including tenant_llm_configs.model)\n"
            "  - JWT_SECRET env var set (admin JWT minting reads settings)\n"
            "\n"
            "Examples:\n"
            "  python tests/e2e/scripts/test_tenant_model_override_e2e.py --help\n"
            "  python tests/e2e/scripts/test_tenant_model_override_e2e.py\n"
            "  python tests/e2e/scripts/test_tenant_model_override_e2e.py \\\n"
            "      --api-base http://staging.lumen.internal:8000\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--api-base",
        default="http://localhost:8000",
        help="Base URL of the running api service (default: http://localhost:8000).",
    )
    return p


if __name__ == "__main__":
    args = _build_parser().parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        _print("INFO", "interrupted by user")
        sys.exit(1)
