"""End-to-end verification of the M5.3 tenant LLM config audit log.

Drives the live API container end-to-end and confirms:

1. A fresh tenant can be minted + a tenant_llm_configs row created
   via the admin POST (which writes 4 audit rows for an insert).
2. PATCH on the same provider writes additional audit rows.
3. The GET audit endpoint returns rows in DESC order, supports
   provider_name + limit filters, and the total reflects the filter.
4. DELETE writes 1 ``__deleted__`` audit marker.

Mirrors ``tests/e2e/scripts/test_tenant_fallback_chain_e2e.py``:
argparse + ``[OK|WARN|FAIL|INFO]`` + exit codes 0/1/2.

Run from repo root with the docker stack up and migrations applied::

    DATABASE_URL=... JWT_SECRET=... \\
    python tests/e2e/scripts/test_tenant_llm_config_audit_e2e.py \\
        --api-base http://localhost:8000
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

import requests


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


from auth.jwt import create_access_token  # noqa: E402
from core.database import reset_engine, reset_sessionmaker  # noqa: E402
from tenant.enums import TenantPlan, TenantStatus  # noqa: E402
from tenant.repository import TenantRepository  # noqa: E402


def _print(level: str, msg: str) -> None:
    line = f"  [{level}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((line + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()


async def _seed_tenant() -> str:
    repo = TenantRepository()
    tenant = await repo.create(
        name="verify-m5-3-audit", plan=TenantPlan.PRO, status=TenantStatus.ACTIVE,
    )
    return tenant.id


def _mint_admin_token(tenant_id: str) -> str:
    return create_access_token(
        tenant_id=tenant_id, user_id=f"verify-m5-3-{tenant_id}", role="admin",
    )


def _check_api_up(api_base: str) -> bool:
    try:
        r = requests.get(f"{api_base}/health/live", timeout=5)
        return r.status_code == 200
    except requests.RequestException as exc:
        _print("FAIL", f"API /health/live unreachable: {type(exc).__name__}: {exc}")
        return False


def _step1_post_writes_audit(api_base: str, tenant_id: str, token: str) -> bool:
    """POST creates row + 4 audit rows (api_key, base_url, model, enabled)."""
    payload = {
        "provider_name": "anthropic",
        "api_key": "sk-m5-3-e2e",
        "base_url": "https://proxy.example",
        "model": "claude-3-5-sonnet-20241022",
    }
    try:
        r = requests.post(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs",
            json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"POST audit: {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 201:
        _print("FAIL", f"POST status={r.status_code} body={r.text[:200]!r}")
        return False
    _print("OK", "POST /admin/.../llm-configs 201 (writes 4 audit rows)")
    return True


def _step2_patch_writes_audit(api_base: str, tenant_id: str, token: str) -> bool:
    payload = {"model": "claude-haiku-4-5"}
    try:
        r = requests.patch(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs/anthropic",
            json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"PATCH model: {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 200:
        _print("FAIL", f"PATCH model status={r.status_code} body={r.text[:200]!r}")
        return False
    payload2 = {"enabled": False}
    try:
        r = requests.patch(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs/anthropic",
            json=payload2, headers={"Authorization": f"Bearer {token}"}, timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"PATCH enabled: {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 200:
        _print("FAIL", f"PATCH enabled status={r.status_code} body={r.text[:200]!r}")
        return False
    _print("OK", "PATCH model + PATCH enabled → 200 (2 audit rows)")
    return True


def _step3_get_audit_returns_rows_newest_first(
    api_base: str, tenant_id: str, token: str,
) -> bool:
    """GET audit must return DESC order + total >= 6 + api_key hash fingerprint."""
    try:
        r = requests.get(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs/audit",
            headers={"Authorization": f"Bearer {token}"}, timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"GET audit: {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 200:
        _print("FAIL", f"GET audit status={r.status_code} body={r.text[:200]!r}")
        return False
    body = r.json()
    # POST insert writes 4 rows; 2 PATCHes each write 1 row (model + enabled)
    if body["total"] < 6:
        _print("FAIL", f"expected total >= 6, got {body['total']}")
        return False
    # Verify DESC order
    timestamps = [e["changed_at"] for e in body["entries"]]
    if timestamps != sorted(timestamps, reverse=True):
        _print("FAIL", "entries not in DESC order")
        return False
    # Verify api_key row has the SHA-256 fingerprint
    api_key_rows = [e for e in body["entries"] if e["field"] == "api_key"]
    if not api_key_rows:
        _print("FAIL", "no api_key audit row found")
        return False
    expected_hash = hashlib.sha256(b"sk-m5-3-e2e").hexdigest()
    if api_key_rows[0]["new_value"] != f"sha256:{expected_hash}":
        _print(
            "FAIL",
            f"api_key.new_value mismatch: {api_key_rows[0]['new_value']!r}",
        )
        return False
    _print("OK", f"GET audit returned {body['total']} rows, DESC, with api_key hash")
    return True


def _step4_get_audit_filter_by_provider(
    api_base: str, tenant_id: str, token: str,
) -> bool:
    """GET with provider_name filter + limit=2 must respect both."""
    try:
        r = requests.get(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs/audit",
            params={"provider_name": "anthropic", "limit": 2},
            headers={"Authorization": f"Bearer {token}"}, timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"GET audit (filter): {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 200:
        _print("FAIL", f"GET audit (filter) status={r.status_code}")
        return False
    body = r.json()
    if not all(e["provider_name"] == "anthropic" for e in body["entries"]):
        _print("FAIL", "filter returned non-anthropic entries")
        return False
    if len(body["entries"]) > 2:
        _print("FAIL", f" limit=2 returned {len(body['entries'])} entry")
        return False
    _print("OK", f"GET audit (filter+limit) returned {len(body['entries'])} rows")
    return True


def _step5_delete_writes_deleted_marker(
    api_base: str, tenant_id: str, token: str,
) -> bool:
    try:
        r = requests.delete(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs/anthropic",
            headers={"Authorization": f"Bearer {token}"}, timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"DELETE: {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 204:
        _print("FAIL", f"DELETE status={r.status_code} body={r.text[:200]!r}")
        return False
    _print("OK", "DELETE → 204 (writes 1 __deleted__ audit marker)")
    return True


def _step6_audit_has_deleted_key(
    api_base: str, tenant_id: str, token: str,
) -> bool:
    """After DELETE, GET audit must show 1 __deleted__ marker (old=anthropic, new=None)."""
    try:
        r = requests.get(
            f"{api_base}/api/v1/admin/tenants/{tenant_id}/llm-configs/audit",
            params={"limit": 200},
            headers={"Authorization": f"Bearer {token}"}, timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"GET audit (post-delete): {type(exc).__name__}: {exc}")
        return False
    if r.status_code != 200:
        _print("FAIL", f"GET audit (post-delete) status={r.status_code}")
        return False
    body = r.json()
    markers = [e for e in body["entries"] if e["field"] == "__deleted__"]
    if len(markers) != 1:
        _print("FAIL", f"expected 1 __deleted__ row, got {len(markers)}")
        return False
    if markers[0]["old_value"] != "anthropic" or markers[0]["new_value"] is not None:
        _print("FAIL", f"__deleted__ row malformed: {markers[0]!r}")
        return False
    _print("OK", "GET audit has 1 __deleted__ marker with old=anthropic, new=null")
    return True


async def run(args: argparse.Namespace) -> int:
    api_base = args.api_base.rstrip("/")
    print("=" * 70)
    print("M5.3 tenant LLM config audit log — E2E")
    print("=" * 70)
    print(f"  api_base = {api_base}")

    if not _check_api_up(api_base):
        _print(
            "FAIL",
            f"API not reachable at {api_base}/health/live. "
            "Bring the docker stack up (scripts/bring-up.sh) before running.",
        )
        return 2
    if not os.environ.get("DATABASE_URL"):
        _print("FAIL", "DATABASE_URL not set — required to mint admin JWT.")
        return 2
    if not os.environ.get("JWT_SECRET"):
        _print("FAIL", "JWT_SECRET not set — required to mint admin JWT.")
        return 2

    overall = True
    try:
        tenant_id = await _seed_tenant()
        _print("OK", f"seeded tenant_id={tenant_id}")
        token = _mint_admin_token(tenant_id=tenant_id)
        _print("OK", "minted admin JWT for tenant")

        overall &= _step1_post_writes_audit(api_base, tenant_id, token)
        overall &= _step2_patch_writes_audit(api_base, tenant_id, token)
        overall &= _step3_get_audit_returns_rows_newest_first(api_base, tenant_id, token)
        overall &= _step4_get_audit_filter_by_provider(api_base, tenant_id, token)
        overall &= _step5_delete_writes_deleted_marker(api_base, tenant_id, token)
        overall &= _step6_audit_has_deleted_key(api_base, tenant_id, token)
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
        prog="test_tenant_llm_config_audit_e2e.py",
        description=(
            "End-to-end smoke for the M5.3 tenant_llm_configs audit log. "
            "Drives the live api container through POST/PATCH/DELETE on "
            "/admin/tenants/{id}/llm-configs and verifies the GET audit "
            "endpoint reflects each mutation."
        ),
        epilog=(
            "Prereqs:\n"
            "  - docker compose stack up (or api running on --api-base)\n"
            "  - DATABASE_URL + JWT_SECRET set\n"
            "  - alembic head applied (table tenant_llm_config_audit_log)\n"
            "  - TENANT_LLM_FERNET_KEY matches the api container's key\n"
            "\n"
            "Examples:\n"
            "  python tests/e2e/scripts/test_tenant_llm_config_audit_e2e.py --help\n"
            "  python tests/e2e/scripts/test_tenant_llm_config_audit_e2e.py\n"
            "  python tests/e2e/scripts/test_tenant_llm_config_audit_e2e.py \\\n"
            "      --api-base http://staging.lumen.internal:8000\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--api-base", default="http://localhost:8000",
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
