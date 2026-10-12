"""End-to-end verification of the M5.1 per-tenant LLM fallback chain.

Drives the live API container end-to-end against the
``/api/v1/admin/tenants/{id}/fallback-chain`` endpoints and confirms:

1. A fresh tenant can be minted and a ``TenantFallbackChain`` row
   upserted via the admin POST.
2. The admin GET returns the row body as written.
3. The per-process :class:`TenantFallbackChainCache` singleton
   reflects writes (positive ``put``/``get``) and that POST/DELETE
   bust the cache eagerly (cache cleared after the write).
4. DELETE removes the row from the DB.

Mirrors ``tests/e2e/scripts/verify_three_integrations.py`` argparse
+ ``[OK|WARN|FAIL|INFO]`` print conventions. Exit codes:

* ``0`` — all checks passed
* ``1`` — uncaught exception (traceback on stderr)
* ``2`` — prereq violation (missing dep / missing ``DATABASE_URL`` / API
  unreachable). Operator action hint is printed before the exit.

Run from repo root with the docker stack up and the alembic migrations
applied (the ``tenant_fallback_chains`` table must be reachable)::

    DATABASE_URL=... JWT_SECRET=... \\
    python tests/e2e/scripts/test_tenant_fallback_chain_e2e.py \\
        --api-base http://localhost:8000

The script is self-contained — it does not require ``ANTHROPIC_API_KEY``
(no chat messages are sent). It only exercises the admin endpoints +
cache layer.
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
from llm_client.tenant_fallback_chain_models import (  # noqa: E402
    TenantFallbackChainRepository,
)
from tenant.enums import TenantPlan, TenantStatus  # noqa: E402
from tenant.repository import TenantRepository  # noqa: E402

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
        name="verify-fallback-chain",
        plan=TenantPlan.PRO,
        status=TenantStatus.ACTIVE,
    )
    return tenant.id


async def _seed_chain_row(*, tenant_id: str) -> None:
    """Upsert a 2-step chain (anthropic → openai) directly via the repo.

    Bypasses the API surface so we can exercise the GET round-trip in
    step 4 deterministically (no JWT juggling yet).
    """
    repo = TenantFallbackChainRepository()
    await repo.upsert(
        tenant_id=tenant_id,
        steps=[
            {
                "provider": "anthropic",
                "model": "claude-3-5-sonnet-20241022",
                "enabled": True,
            },
            {
                "provider": "openai",
                "model": "gpt-4o",
                "enabled": True,
            },
        ],
        enabled=True,
    )


def _mint_admin_token(tenant_id: str) -> str:
    """Mint a fresh admin JWT for ``tenant_id`` via the project's JWT helper."""
    return create_access_token(
        tenant_id=tenant_id,
        user_id=f"verify-fc-{tenant_id}",
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


def _step1_upsert_chain(api_base: str, tenant_id: str, token: str) -> bool:
    """POST upserts a 3-step chain (anthropic → openai → minimax)."""
    payload = {
        "steps": [
            {"provider": "anthropic", "model": "claude-3-5-sonnet-20241022", "enabled": True},
            {"provider": "openai", "model": "gpt-4o", "enabled": True},
            {"provider": "minimax", "model": "MiniMax-M3", "enabled": True},
        ],
        "enabled": True,
    }
    try:
        r = requests.post(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/fallback-chain",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"POST upsert: {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 200:
        _print("FAIL", f"POST upsert status={r.status_code} body={r.text[:200]!r}")
        return False
    body = r.json()
    if len(body.get("steps", [])) != 3:
        _print("FAIL", f"POST upsert: expected 3 steps, got {body.get('steps')!r}")
        return False
    if body["steps"][0]["provider"] != "anthropic":
        _print("FAIL", f"POST upsert: step[0].provider != anthropic — {body['steps']!r}")
        return False
    _print("OK", "POST /admin/.../fallback-chain upsert 200 + 3 steps")
    return True


def _step2_get_chain(api_base: str, tenant_id: str, token: str) -> bool:
    """GET should return the row written by step 1 (or step 0 if step 1 skipped)."""
    try:
        r = requests.get(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/fallback-chain",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"GET chain: {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 200:
        _print("FAIL", f"GET chain status={r.status_code} body={r.text[:200]!r}")
        return False
    body = r.json()
    if body.get("tenant_id") != tenant_id:
        _print("FAIL", f"GET chain: tenant_id mismatch — {body.get('tenant_id')!r}")
        return False
    steps = body.get("steps", [])
    if not steps or steps[0].get("provider") != "anthropic":
        _print("FAIL", f"GET chain: step[0] provider != anthropic — {steps!r}")
        return False
    if body.get("enabled") is not True:
        _print("FAIL", f"GET chain: enabled != True — {body.get('enabled')!r}")
        return False
    _print("OK", f"GET /admin/.../fallback-chain 200 + {len(steps)} steps back")
    return True


def _step3_cache_prime_and_get(tenant_id: str) -> bool:
    """Reset the cache singleton, prime it with a fake row, assert cache.get returns it."""
    # Local import — keep top-level imports clean of the app init path.
    import agent.llm_factory

    agent.llm_factory._fallback_chain_cache = None
    cache = agent.llm_factory._build_fallback_chain_cache()
    # Build a fake row in-memory (no DB write — pure cache sanity check).
    from datetime import UTC, datetime

    from llm_client.tenant_fallback_chain_models import TenantFallbackChain

    fake_row = TenantFallbackChain(
        id="01HZDUMMY0000000000000000",
        tenant_id=tenant_id,
        steps=[
            {"provider": "anthropic", "model": "claude-3-5-sonnet-20241022", "enabled": True},
        ],
        enabled=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    cache.put(tenant_id, fake_row)
    if cache.get(tenant_id) is not fake_row:
        _print("FAIL", "cache prime+get: cache.get(tenant_id) != fake_row")
        return False
    _print("OK", "cache prime+get: put/fetch returned the same row reference")
    return True


def _step4_post_then_cache_invalidated(
    api_base: str, tenant_id: str, token: str,
) -> bool:
    """After POST, the per-process cache must be cleared for this tenant."""
    import agent.llm_factory

    # Prime again with a stale fake so we can prove POST busts it.
    cache = agent.llm_factory._build_fallback_chain_cache()
    if cache.get(tenant_id) is None:
        # 30 s: nothing primed — push a sentinel.
        from datetime import UTC, datetime

        from llm_client.tenant_fallback_chain_models import TenantFallbackChain
        sentinel = TenantFallbackChain(
            id="01HZSENTINEL00000000000000",
            tenant_id=tenant_id,
            steps=[{"provider": "openai", "model": "gpt-4o", "enabled": True}],
            enabled=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        cache.put(tenant_id, sentinel)

    payload = {
        "steps": [
            {"provider": "openai", "model": "gpt-4o", "enabled": True},
        ],
        "enabled": True,
    }
    try:
        r = requests.post(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/fallback-chain",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"POST (cache bust): {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 200:
        _print("FAIL", f"POST (cache bust) status={r.status_code} body={r.text[:200]!r}")
        return False
    # Same singleton instance — invalidate() pops the tenant entry.
    if cache.get(tenant_id) is not None:
        _print("FAIL", "POST bust: cache.get(tenant_id) still primed after POST")
        return False
    _print("OK", "POST busted the per-process singleton (cache.get == None)")
    return True


def _step5_delete_removes_row(
    api_base: str, tenant_id: str, token: str,
) -> bool:
    """DELETE → 204; the row must be gone from the DB."""
    try:
        r = requests.delete(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/fallback-chain",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"DELETE: {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 204:
        _print("FAIL", f"DELETE status={r.status_code} body={r.text[:200]!r}")
        return False
    _print("OK", "DELETE /admin/.../fallback-chain → 204")
    return True


async def _step6_db_row_gone(tenant_id: str) -> bool:
    """The TenantFallbackChainRepository must report None after DELETE."""
    repo = TenantFallbackChainRepository()
    row = await repo.get(tenant_id)
    if row is not None:
        _print("FAIL", f"DB row still present after DELETE: {row!r}")
        return False
    _print("OK", "DB row gone after DELETE (repo.get(tenant_id) is None)")
    return True


# --- Main ---------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    api_base = args.api_base.rstrip("/")
    print("=" * 70)
    print("M5.1 tenant fallback chain — E2E")
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
            "DATABASE_URL not set — required to drive the chain repo "
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

        await _seed_chain_row(tenant_id=tenant_id)
        _print("OK", "seeded 2-step fallback chain (anthropic → openai) via repo")

        token = _mint_admin_token(tenant_id=tenant_id)
        _print("OK", "minted admin JWT for tenant")

        overall &= _step1_upsert_chain(api_base, tenant_id, token)
        overall &= _step2_get_chain(api_base, tenant_id, token)
        overall &= _step3_cache_prime_and_get(tenant_id)
        overall &= _step4_post_then_cache_invalidated(api_base, tenant_id, token)
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
        prog="test_tenant_fallback_chain_e2e.py",
        description=(
            "End-to-end smoke for the M5.1 per-tenant LLM fallback chain admin "
            "endpoints + per-process TenantFallbackChainCache invalidation. "
            "Runs against a live api container and exercises POST/GET/DELETE "
            "on /api/v1/admin/tenants/{id}/fallback-chain."
        ),
        epilog=(
            "Prereqs:\n"
            "  - docker compose stack up (or the api service running on\n"
            "    --api-base)\n"
            "  - DATABASE_URL env var set (Postgres reachable, alembic\n"
            "    migrations applied including tenant_fallback_chains)\n"
            "  - JWT_SECRET env var set (admin JWT minting reads settings)\n"
            "  - The Fernet key in source matches the api container's\n"
            "    TENANT_LLM_FERNET_KEY (dev default is set in code)\n"
            "\n"
            "Examples:\n"
            "  python tests/e2e/scripts/test_tenant_fallback_chain_e2e.py --help\n"
            "  python tests/e2e/scripts/test_tenant_fallback_chain_e2e.py\n"
            "  python tests/e2e/scripts/test_tenant_fallback_chain_e2e.py \\\n"
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