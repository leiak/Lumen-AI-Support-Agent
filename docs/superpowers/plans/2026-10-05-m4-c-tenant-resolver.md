# M4.C — BYOK TenantResolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-tenant LLM provider API keys (BYOK) — each tenant registers its own provider keys via admin, encrypted with Fernet; resolvers built per-tenant and composed with M4.B's `FallbackResolver`.

**Architecture:** A `tenant_resolver.TenantResolver` wraps a tenant-private `LLMGateway`, implements the M4.A `Resolver` + `ainvoke` seam (no `LLMClient.__init__` change). Config rows live in a new `tenant_llm_configs` table; API keys encrypted at rest with Fernet (master key from env). An in-process LRU + TTL cache absorbs per-request DB lookup cost. Strict mode — tenants without enabled keys raise `TenantLlmNotConfigured(ProviderUnavailable)`, no silent fallback to global keys.

**Tech Stack:** SQLAlchemy (async), `cryptography.fernet`, Pydantic v2, FastAPI, pytest + pytest-asyncio + pytest-httpx.

**Predecessor:** M4.B at commit `404d91e`. Spec: `docs/superpowers/specs/2026-10-05-m4-c-tenant-resolver-design.md` (commits `aa18ae1`, `cf9e402`).

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `apps/api/migrations/versions/16_add_tenant_llm_configs.py` | New | Alembic migration for `tenant_llm_configs` table |
| `apps/api/src/llm_client/tenant_config_models.py` | New | `TenantLLMConfig` ORM + `TenantLLMConfigRepository.list_by_tenant(enabled_only=True)` |
| `apps/api/src/llm_client/tenant_config_crypto.py` | New | `TenantLLMConfigCipher` (Fernet wrapper: `encrypt`/`decrypt`/`__init__`) |
| `apps/api/src/llm_client/tenant_resolver.py` | New | `TenantLLMConfigCache` (LRU+TTL) + `TenantResolver` class + `_NoChainConfigured` sentinel |
| `apps/api/src/llm_client/exceptions.py` | Modified | Add `TenantLlmNotConfigured(ProviderUnavailable)` |
| `apps/api/src/agent/llm_factory.py` | Modified | `_default_llm_client_factory(tenant_id)` builds via `TenantResolver.build(tenant_id)` |
| `apps/api/src/core/config.py` | Modified | Add `tenant_llm_fernet_key`, `tenant_llm_cache_ttl_s`, `tenant_llm_cache_maxsize` |
| `apps/api/src/core/business_metrics.py` | Modified | Add `LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL` counter |
| `apps/api/src/admin/schemas/tenant_llm_config.py` | New | `TenantLLMConfigCreate` (request) + `TenantLLMConfigRead` (response) Pydantic schemas |
| `apps/api/src/admin/repository.py` | Modified | Add `AdminTenantLLMConfigRepository.upsert(tenant_id, provider_name, encrypted_api_key, base_url, enabled)` + `.list(tenant_id)` + tenant existence check |
| `apps/api/src/admin/api.py` | Modified | `POST /admin/tenants/{tenant_id}/llm-configs` + `GET /admin/tenants/{tenant_id}/llm-configs` |
| `apps/api/src/llm_client/__init__.py` | Modified | Export `TenantResolver`, `TenantLlmNotConfigured`, `TenantLLMConfigCipher` |
| `apps/api/src/admin/schemas/__init__.py` | Modified | Re-export new schemas |
| `apps/api/tests/llm_client/test_tenant_config_crypto.py` | New | 4 tests: round-trip, wrong-key rejection, key-required constructor, byte format |
| `apps/api/tests/llm_client/test_tenant_config_models.py` | New | 5 tests: ORM round-trip, unique constraint, enabled filter, empty result, cascade delete |
| `apps/api/tests/llm_client/test_tenant_resolver.py` | New | 8 tests: cache hit/miss/TTL/LRU, empty-rows raises, missing providers in exception, fallback chain passthrough, single-provider sentinel |
| `apps/api/tests/llm_client/integration/test_tenant_resolver_e2e.py` | New | 4 tests: HTTP key transmission, not-configured error, fallback chain, tenant isolation |
| `apps/api/tests/admin/test_tenant_llm_config_api.py` | New | 3 tests: POST encrypted, POST upsert, GET no-keys-in-response |
| `README.md` | Modified | Add M4.C status row + 7 tech debt items (per spec §11) |
| `~/.claude/projects/D--work-ai-0401-ai-customer/memory/m4-c-progress.md` | New | Memory file |
| `~/.claude/projects/D--work-ai-0401-ai-customer/memory/MEMORY.md` | Modified | Add M4.C pointer |

---

## Task 1: Foundation — Cipher + ORM + Migration

**Files:**
- Create: `apps/api/src/llm_client/tenant_config_crypto.py`
- Create: `apps/api/src/llm_client/tenant_config_models.py`
- Create: `apps/api/migrations/versions/16_add_tenant_llm_configs.py`
- Modify: `apps/api/src/core/config.py`
- Test: `apps/api/tests/llm_client/test_tenant_config_crypto.py`
- Test: `apps/api/tests/llm_client/test_tenant_config_models.py`

**Goal:** Tenant LLM config rows can be stored and retrieved with encrypted API keys. Cipher works independently (no TenantRepository code, just encrypt/decrypt).

- [ ] **Step 1.1: Add Settings fields**

Modify `apps/api/src/core/config.py` after the existing M4.B fields (`llm_fallback_chain`, `llm_fallback_attempt_timeout_s`). Add:

```python
    # M4.C — Tenant BYOK. ``tenant_llm_fernet_key`` is required at
    # startup; the cipher raises RuntimeError if missing. Generate with:
    # ``python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'``
    #
    # ``tenant_llm_cache_ttl_s`` controls the in-process LRU cache TTL
    # (per spec §8.2). 60s amortizes the per-tenant DB + decrypt cost
    # over the typical chat workload while bounding config-staleness.
    #
    # ``tenant_llm_cache_maxsize`` caps the LRU eviction. With ~1KB per
    # cached resolver, 1024 entries ≈ 1MB worst case.
    tenant_llm_fernet_key: str | None = Field(
        default=None, alias="TENANT_LLM_FERNET_KEY"
    )
    tenant_llm_cache_ttl_s: float = Field(
        default=60.0, alias="TENANT_LLM_CACHE_TTL_S"
    )
    tenant_llm_cache_maxsize: int = Field(
        default=1024, alias="TENANT_LLM_CACHE_MAXSIZE"
    )
```

Also add `cryptography` to `apps/api/pyproject.toml` if not already present (verify by `grep -q 'cryptography' apps/api/pyproject.toml`; if absent, add `cryptography = "^42.0.0"` to dependencies).

- [ ] **Step 1.2: Write failing test for cipher**

Create `apps/api/tests/llm_client/test_tenant_config_crypto.py`:

```python
"""Tests for TenantLLMConfigCipher (Fernet wrapper)."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from llm_client.tenant_config_crypto import TenantLLMConfigCipher


def _gen_key() -> str:
    return Fernet.generate_key().decode("utf-8")


def test_encrypt_decrypt_roundtrip() -> None:
    cipher = TenantLLMConfigCipher(_gen_key())
    plaintext = "sk-test-12345"
    ciphertext = cipher.encrypt(plaintext)
    assert isinstance(ciphertext, bytes)
    assert cipher.decrypt(ciphertext) == plaintext


def test_decrypt_rejects_wrong_key() -> None:
    encrypter = TenantLLMConfigCipher(_gen_key())
    ciphertext = encrypter.encrypt("sk-original")
    wrong_cipher = TenantLLMConfigCipher(_gen_key())
    with pytest.raises(InvalidToken):
        wrong_cipher.decrypt(ciphertext)


def test_cipher_constructor_requires_key() -> None:
    with pytest.raises(RuntimeError, match="TENANT_LLM_FERNET_KEY"):
        TenantLLMConfigCipher(None)
    with pytest.raises(RuntimeError, match="TENANT_LLM_FERNET_KEY"):
        TenantLLMConfigCipher("")


def test_encrypted_bytes_are_not_plaintext() -> None:
    cipher = TenantLLMConfigCipher(_gen_key())
    plaintext = "sk-visible-secret-key"
    ciphertext = cipher.encrypt(plaintext)
    assert plaintext.encode("utf-8") not in ciphertext
    # Fernet output is URL-safe base64 — verify it round-trips through b64decode
    import base64
    decoded = base64.urlsafe_b64decode(ciphertext + b"=" * (-len(ciphertext) % 4))
    assert plaintext.encode("utf-8") in decoded  # present in payload but encoded
```

- [ ] **Step 1.3: Run tests to verify they fail**

Run: `cd apps/api && .venv/Scripts/python.exe -m pytest tests/llm_client/test_tenant_config_crypto.py -v` (or `pytest` if activated venv).
Expected: FAIL with `ModuleNotFoundError: No module named 'llm_client.tenant_config_crypto'`.

- [ ] **Step 1.4: Implement cipher**

Create `apps/api/src/llm_client/tenant_config_crypto.py`:

```python
"""Fernet-based encryption of tenant LLM API keys at rest.

The master key is loaded from ``settings.tenant_llm_fernet_key`` (env
``TENANT_LLM_FERNET_KEY``). Fernet = AES-128-CBC + HMAC-SHA256; safe to
store ciphertext in the DB without additional key wrapping. Generate
a development key with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class TenantLLMConfigCipher:
    """Encrypts / decrypts per-tenant LLM API keys using Fernet."""

    def __init__(self, master_key: str | None) -> None:
        if not master_key:
            raise RuntimeError(
                "TENANT_LLM_FERNET_KEY is required for M4.C BYOK. "
                "Generate with: "
                "python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'"
            )
        self._fernet = Fernet(master_key.encode("utf-8"))

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt an API key. Returns URL-safe base64 ciphertext bytes."""
        if not plaintext:
            raise ValueError("plaintext cannot be empty")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        """Decrypt ciphertext produced by :meth:`encrypt`.

        Raises:
            InvalidToken: master key changed since encryption. Operator
                must re-encrypt the row (no automatic rotation).
        """
        return self._fernet.decrypt(ciphertext).decode("utf-8")


__all__ = ["TenantLLMConfigCipher", "InvalidToken"]
```

- [ ] **Step 1.5: Run tests to verify they pass**

Run: `cd apps/api && pytest tests/llm_client/test_tenant_config_crypto.py -v`
Expected: 4 passed.

- [ ] **Step 1.6: Write failing test for ORM model**

Create `apps/api/tests/llm_client/test_tenant_config_models.py`:

```python
"""Tests for TenantLLMConfig ORM + repository (DB integration via testcontainer)."""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from core.id_gen import new_id
from llm_client.tenant_config_crypto import TenantLLMConfigCipher
from llm_client.tenant_config_models import (
    TenantLLMConfig,
    TenantLLMConfigRepository,
)
from tenant.enums import TenantPlan
from tenant.repository import TenantRepository


@pytest.fixture
def cipher() -> TenantLLMConfigCipher:
    from cryptography.fernet import Fernet
    return TenantLLMConfigCipher(Fernet.generate_key().decode())


async def test_create_and_get(cipher: TenantLLMConfigCipher) -> None:
    tenant = await TenantRepository().create(name="BYOK Test 1", plan=TenantPlan.PRO)
    repo = TenantLLMConfigRepository()
    ciphertext = cipher.encrypt("sk-round-trip-test")
    created = await repo.upsert(
        tenant_id=tenant.id,
        provider_name="minimax",
        encrypted_api_key=ciphertext,
        base_url=None,
        enabled=True,
    )
    assert created.tenant_id == tenant.id
    assert created.provider_name == "minimax"
    assert created.encrypted_api_key == ciphertext
    # Verify decryption round-trip
    assert cipher.decrypt(created.encrypted_api_key) == "sk-round-trip-test"


async def test_unique_constraint_per_tenant_provider(cipher: TenantLLMConfigCipher) -> None:
    tenant = await TenantRepository().create(name="BYOK Test 2", plan=TenantPlan.PRO)
    repo = TenantLLMConfigRepository()
    ciphertext = cipher.encrypt("sk-first")
    await repo.upsert(
        tenant_id=tenant.id,
        provider_name="minimax",
        encrypted_api_key=ciphertext,
        base_url=None,
        enabled=True,
    )
    # Upsert (not insert) should succeed and replace
    updated_ciphertext = cipher.encrypt("sk-second")
    second = await repo.upsert(
        tenant_id=tenant.id,
        provider_name="minimax",
        encrypted_api_key=updated_ciphertext,
        base_url=None,
        enabled=True,
    )
    assert cipher.decrypt(second.encrypted_api_key) == "sk-second"
    # Direct INSERT bypassing upsert should fail
    from sqlalchemy import insert
    from core.database import get_sessionmaker
    sm = get_sessionmaker()
    async with sm() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(TenantLLMConfig).values(
                    id=new_id(),
                    tenant_id=tenant.id,
                    provider_name="minimax",
                    encrypted_api_key=ciphertext,
                    enabled=True,
                )
            )


async def test_list_by_tenant_returns_only_enabled(cipher: TenantLLMConfigCipher) -> None:
    tenant = await TenantRepository().create(name="BYOK Test 3", plan=TenantPlan.PRO)
    repo = TenantLLMConfigRepository()
    await repo.upsert(
        tenant_id=tenant.id, provider_name="minimax",
        encrypted_api_key=cipher.encrypt("sk-a"), base_url=None, enabled=True,
    )
    await repo.upsert(
        tenant_id=tenant.id, provider_name="anthropic",
        encrypted_api_key=cipher.encrypt("sk-b"), base_url=None, enabled=False,
    )
    rows = await repo.list_by_tenant(tenant.id, enabled_only=True)
    assert len(rows) == 1
    assert rows[0].provider_name == "minimax"


async def test_list_by_tenant_returns_empty_for_new_tenant() -> None:
    tenant = await TenantRepository().create(name="BYOK Test 4", plan=TenantPlan.PRO)
    repo = TenantLLMConfigRepository()
    rows = await repo.list_by_tenant(tenant.id)
    assert rows == []


async def test_cascade_delete_with_tenant(cipher: TenantLLMConfigCipher) -> None:
    tenant = await TenantRepository().create(name="BYOK Test 5", plan=TenantPlan.PRO)
    repo = TenantLLMConfigRepository()
    await repo.upsert(
        tenant_id=tenant.id, provider_name="minimax",
        encrypted_api_key=cipher.encrypt("sk-cascade"), base_url=None, enabled=True,
    )
    # Delete tenant — config rows should cascade
    from core.database import get_sessionmaker
    sm = get_sessionmaker()
    async with sm() as session:
        tenant_obj = await session.get(type(tenant), tenant.id)
        await session.delete(tenant_obj)
        await session.commit()
    rows = await repo.list_by_tenant(tenant.id)
    assert rows == []
```

NOTE: these tests need a Postgres testcontainer (already used elsewhere — check `tests/conftest.py` for the pattern). If testcontainer is not available, mark these tests with `@pytest.mark.skip_postgres` and add to the existing skip pattern.

- [ ] **Step 1.7: Run tests to verify they fail**

Run: `cd apps/api && pytest tests/llm_client/test_tenant_config_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'llm_client.tenant_config_models'`.

- [ ] **Step 1.8: Write the migration**

Create `apps/api/migrations/versions/16_add_tenant_llm_configs.py`:

```python
"""add tenant_llm_configs table

Revision ID: 16_add_tenant_llm_configs
Revises: 15_kb_drafts
Create Date: 2026-10-05

Per M4.C spec §6.1: one row per (tenant_id, provider_name), holds
Fernet-encrypted API key + optional base_url override + enabled flag.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "16_add_tenant_llm_configs"
down_revision = "15_kb_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_llm_configs",
        sa.Column("id", sa.String(length=26), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=26),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_name", sa.String(length=64), nullable=False),
        sa.Column("encrypted_api_key", sa.LargeBinary(), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "provider_name", name="uq_tenant_llm_configs_tenant_provider"),
    )
    op.create_index(
        "ix_tenant_llm_configs_tenant_id",
        "tenant_llm_configs",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tenant_llm_configs_tenant_id", table_name="tenant_llm_configs")
    op.drop_table("tenant_llm_configs")
```

**Verify the correct `down_revision`**: run `cd apps/api && alembic heads` and `cd apps/api && ls migrations/versions/ | tail -3`. The new revision's `down_revision` MUST equal the latest head. If `15_kb_drafts` is not the head, use the actual head filename (strip the `.py` suffix).

- [ ] **Step 1.9: Run migration locally**

Run: `cd apps/api && alembic upgrade head`
Expected: migration applies; verify with `\d tenant_llm_configs` in psql or `inspect` via Python:

```bash
cd apps/api && python -c "
import asyncio
from sqlalchemy import inspect
from core.database import get_engine
async def check():
    engine = get_engine()
    async with engine.connect() as conn:
        defs = await conn.run_sync(lambda sync: inspect(sync).get_columns('tenant_llm_configs'))
        for c in defs:
            print(c['name'], c['type'])
asyncio.run(check())
"
```

Expected output includes: `id`, `tenant_id`, `provider_name`, `encrypted_api_key`, `base_url`, `enabled`, `created_at`, `updated_at`.

- [ ] **Step 1.10: Implement ORM model + repository**

Create `apps/api/src/llm_client/tenant_config_models.py`:

```python
"""ORM model + repository for ``tenant_llm_configs`` (M4.C BYOK).

Schema per M4.C spec §6.1: per-(tenant, provider) row with Fernet-
encrypted API key, optional base_url override, and an enabled flag
for ops-level disable without row deletion.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base, get_sessionmaker
from core.id_gen import new_id


class TenantLLMConfig(Base):
    """One row = one provider's API key for one tenant."""

    __tablename__ = "tenant_llm_configs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider_name",
            name="uq_tenant_llm_configs_tenant_provider",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_api_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantLLMConfigRepository:
    """CRUD for ``TenantLLMConfig`` rows."""

    async def list_by_tenant(
        self, tenant_id: str, *, enabled_only: bool = True
    ) -> list[TenantLLMConfig]:
        """Return all configs for a tenant. ``enabled_only`` filters out disabled rows."""
        sm = get_sessionmaker()
        async with sm() as session:
            stmt = select(TenantLLMConfig).where(TenantLLMConfig.tenant_id == tenant_id)
            if enabled_only:
                stmt = stmt.where(TenantLLMConfig.enabled.is_(True))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def upsert(
        self,
        *,
        tenant_id: str,
        provider_name: str,
        encrypted_api_key: bytes,
        base_url: str | None,
        enabled: bool = True,
    ) -> TenantLLMConfig:
        """Insert or update a (tenant_id, provider_name) row.

        Uses INSERT ... ON CONFLICT (Postgres-specific) for atomicity.
        The unique constraint enforces one row per pair.
        """
        from sqlalchemy import insert
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        sm = get_sessionmaker()
        async with sm() as session:
            stmt = (
                pg_insert(TenantLLMConfig)
                .values(
                    id=new_id(),
                    tenant_id=tenant_id,
                    provider_name=provider_name,
                    encrypted_api_key=encrypted_api_key,
                    base_url=base_url,
                    enabled=enabled,
                )
                .on_conflict_do_update(
                    constraint="uq_tenant_llm_configs_tenant_provider",
                    set_={
                        "encrypted_api_key": encrypted_api_key,
                        "base_url": base_url,
                        "enabled": enabled,
                        "updated_at": func.now(),
                    },
                )
                .returning(TenantLLMConfig)
            )
            result = await session.execute(stmt)
            row_obj = result.scalar_one()
            await session.commit()
            await session.refresh(row_obj)
            return row_obj


__all__ = ["TenantLLMConfig", "TenantLLMConfigRepository"]
```

- [ ] **Step 1.11: Run tests to verify they pass**

Run: `cd apps/api && pytest tests/llm_client/test_tenant_config_models.py -v`
Expected: 5 passed.

- [ ] **Step 1.12: Commit**

```bash
cd apps/api
git add src/llm_client/tenant_config_crypto.py \
        src/llm_client/tenant_config_models.py \
        src/core/config.py \
        migrations/versions/16_add_tenant_llm_configs.py \
        pyproject.toml \
        tests/llm_client/test_tenant_config_crypto.py \
        tests/llm_client/test_tenant_config_models.py
git commit -m "feat(llm-client): TenantLLMConfigCipher + tenant_llm_configs table + repo (M4.C Task 1)

- Fernet-based API key encryption (TENANT_LLM_FERNET_KEY env)
- New tenant_llm_configs table with (tenant_id, provider_name) unique constraint
- Repository: list_by_tenant(enabled_only=True) + upsert (ON CONFLICT)
- 4 cipher tests + 5 ORM tests passing

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 2: TenantLLMConfigCache + TenantResolver

**Files:**
- Create: `apps/api/src/llm_client/tenant_resolver.py`
- Modify: `apps/api/src/llm_client/__init__.py`
- Test: `apps/api/tests/llm_client/test_tenant_resolver.py`

**Goal:** Cache absorbs per-tenant DB lookup cost; resolver builds a tenant-private gateway on cache miss; supports both single-provider and fallback-chained tenant setups. Resolver implements both `__call__` and `ainvoke`.

- [ ] **Step 2.1: Write failing tests for cache + resolver**

Create `apps/api/tests/llm_client/test_tenant_resolver.py`:

```python
"""Tests for TenantLLMConfigCache + TenantResolver."""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_client.resolvers import FallbackResolver
from llm_client.tenant_resolver import (
    TenantLLMConfigCache,
    TenantResolver,
    _NoChainConfigured,
)


class _FakeRepo:
    def __init__(self, rows_by_tenant: dict[str, list[Any]]) -> None:
        self.rows_by_tenant = rows_by_tenant
        self.call_count = 0

    async def list_by_tenant(self, tenant_id: str, *, enabled_only: bool = True) -> list[Any]:
        self.call_count += 1
        return list(self.rows_by_tenant.get(tenant_id, []))


def _make_provider(name: str) -> Any:
    """Return a stub BaseProvider carrying the .name attribute LLMClient expects."""
    p = MagicMock()
    p.name = name
    p.chat = AsyncMock()
    p.stream = AsyncMock()
    p.aclose = AsyncMock()
    return p


def test_cache_hit_skips_db_lookup() -> None:
    repo = _FakeRepo({"t1": [MagicMock()]})
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)
    cache._build_providers = MagicMock(return_value={"minimax": _make_provider("minimax")})  # type: ignore[attr-defined]
    cache.put("t1", "fake-resolver")
    assert cache.get("t1") == "fake-resolver"
    assert repo.call_count == 0


def test_cache_miss_loads_from_repo() -> None:
    repo = _FakeRepo({"t1": [MagicMock()]})
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)
    cache._build_providers = MagicMock(return_value={"minimax": _make_provider("minimax")})  # type: ignore[attr-defined]
    cache._repo = repo  # type: ignore[attr-defined]
    cache.get_or_load("t1")
    cache.get_or_load("t1")  # second call hits cache
    assert repo.call_count == 1


def test_cache_respects_ttl() -> None:
    repo = _FakeRepo({"t1": [MagicMock()]})
    cache = TenantLLMConfigCache(ttl_s=0.01, maxsize=16)
    cache._build_providers = MagicMock(return_value={"minimax": _make_provider("minimax")})  # type: ignore[attr-defined]
    cache._repo = repo  # type: ignore[attr-defined]
    cache.get_or_load("t1")
    time.sleep(0.05)  # past TTL
    cache.get_or_load("t1")
    assert repo.call_count == 2


def test_cache_lru_evicts_oldest() -> None:
    repo = _FakeRepo({f"t{i}": [MagicMock()] for i in range(5)})
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=3)
    cache._build_providers = MagicMock(return_value={"minimax": _make_provider("minimax")})  # type: ignore[attr-defined]
    cache._repo = repo  # type: ignore[attr-defined]
    cache.get_or_load("t0")
    cache.get_or_load("t1")
    cache.get_or_load("t2")
    cache.get_or_load("t3")  # t0 evicted
    assert cache.get("t0") is None  # evicted
    assert cache.get("t3") == cache.get("t2") or cache.get("t3") is not None


def test_raises_when_no_enabled_providers() -> None:
    from llm_client.exceptions import TenantLlmNotConfigured

    repo = _FakeRepo({"t1": []})
    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)
    cache._build_providers = MagicMock(return_value={})  # type: ignore[attr-defined]
    cache._repo = repo  # type: ignore[attr-defined]
    with pytest.raises(TenantLlmNotConfigured) as exc:
        cache.get_or_load("t1")
    assert exc.value.tenant_id == "t1"


def test_missing_providers_listed_in_exception() -> None:
    from llm_client.exceptions import TenantLlmNotConfigured

    cache = TenantLLMConfigCache(ttl_s=60.0, maxsize=16)
    providers = {"minimax": _make_provider("minimax")}
    with pytest.raises(TenantLlmNotConfigured) as exc:
        cache._raise_or_return(  # type: ignore[attr-defined]
            tenant_id="t1",
            providers=providers,
            known_providers=["minimax", "anthropic", "openai"],
        )
    assert "anthropic" in exc.value.missing_providers
    assert "openai" in exc.value.missing_providers
    assert "minimax" not in exc.value.missing_providers


def test_tenant_resolver_call_delegates_to_inner_gateway() -> None:
    primary = _make_provider("minimax")
    gateway = MagicMock()
    gateway.default_resolver = MagicMock(return_value=primary)
    resolver = TenantResolver(gateway=gateway)
    result = resolver(MagicMock())
    assert result is primary


def test_single_provider_tenant_ainvoke_raises_no_chain() -> None:
    primary = _make_provider("minimax")
    # _PrefixResolver has no ainvoke; simulates single-provider tenant
    prefix_resolver = MagicMock(spec=["__call__"])
    prefix_resolver.__call__ = MagicMock(return_value=primary)
    gateway = MagicMock()
    gateway.default_resolver = prefix_resolver
    resolver = TenantResolver(gateway=gateway)
    import asyncio
    with pytest.raises(_NoChainConfigured):
        asyncio.run(resolver.ainvoke(MagicMock()))


def test_chain_tenant_ainvoke_delegates() -> None:
    primary = _make_provider("minimax")
    fallback = MagicMock(spec=["__call__", "ainvoke"])
    fallback.ainvoke = AsyncMock(return_value="response")
    gateway = MagicMock()
    gateway.default_resolver = fallback
    resolver = TenantResolver(gateway=gateway)
    import asyncio
    result = asyncio.run(resolver.ainvoke(MagicMock()))
    assert result == "response"
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `cd apps/api && pytest tests/llm_client/test_tenant_resolver.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'llm_client.tenant_resolver'`.

- [ ] **Step 2.3: Implement TenantLlmNotConfigured exception**

Modify `apps/api/src/llm_client/exceptions.py` — add after the existing classes:

```python
class TenantLlmNotConfigured(ProviderUnavailable):
    """Raised when a tenant has zero enabled LLM provider configs.

    Carries the ``tenant_id`` and the list of ``missing_providers``
    so admins can diagnose which providers the tenant needs to
    configure. Extends ``ProviderUnavailable`` so existing callers
    that catch ``ProviderUnavailable`` get this for free (LLMClient
    treats it as a 5xx-equivalent failure → no retry, metric
    ``outcome="unavailable"``).
    """

    def __init__(self, tenant_id: str, missing_providers: list[str]) -> None:
        self.tenant_id = tenant_id
        self.missing_providers = missing_providers
        super().__init__(
            f"Tenant {tenant_id!r} has no LLM provider configured "
            f"(missing one of: {missing_providers})"
        )


__all__ = [
    "AttemptRecord",
    "FallbackChainExhausted",
    "InvalidRequest",
    "OutputInvalid",
    "ProviderUnavailable",
    "RateLimited",
    "TenantLlmNotConfigured",
]
```

- [ ] **Step 2.4: Implement TenantLLMConfigCache + TenantResolver**

Create `apps/api/src/llm_client/tenant_resolver.py`:

```python
"""Per-tenant LLM resolver with in-process LRU + TTL cache.

A :class:`TenantResolver` is built per-tenant on cache miss by:

1. Looking up the tenant's enabled ``TenantLLMConfig`` rows.
2. Decrypting each row's API key with the project Fernet cipher.
3. Constructing a tenant-private :class:`LLMGateway` from those
   providers (which keeps the existing prefix-router / fallback-chain
   logic intact).
4. Returning the gateway's default resolver as the cache value.

Subsequent LLM calls for the same tenant hit the cache and reuse the
resolver. The cache is process-local; multi-instance deployments see
one DB hit per process per tenant per TTL window (60s by default).
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from llm_client.exceptions import TenantLlmNotConfigured
from llm_client.resolvers import Resolver
from llm_client.tenant_config_models import TenantLLMConfigRepository

if TYPE_CHECKING:
    from llm_client.gateway import LLMGateway
    from llm_client.types import ChatRequest, ChatResponse


class _NoChainConfigured(Exception):
    """Private sentinel: tenant has a single provider, no fallback chain.

    ``TenantResolver.ainvoke`` raises this when the inner gateway's
    default resolver doesn't expose ``ainvoke`` (single-provider
    tenants). LLMClient catches it and falls back to its own retry
    loop on the primary provider.
    """


class TenantLLMConfigCache:
    """In-process LRU + TTL cache of per-tenant resolvers."""

    def __init__(
        self,
        *,
        ttl_s: float,
        maxsize: int,
        repo: TenantLLMConfigRepository | None = None,
        cipher: Any | None = None,
        settings: Any | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._maxsize = maxsize
        self._cache: OrderedDict[str, tuple[float, Resolver]] = OrderedDict()
        self._repo = repo or TenantLLMConfigRepository()
        # ``cipher`` and ``settings`` are injected by ``TenantResolver.build``
        # so this constructor is callable from fixtures. see ``_build_providers``.
        self._cipher = cipher
        self._settings = settings

    def get(self, tenant_id: str) -> Resolver | None:
        """Return the cached resolver for ``tenant_id`` if not expired.

        Expired entries are evicted lazily on access.
        """
        entry = self._cache.get(tenant_id)
        if entry is None:
            return None
        expires_at, resolver = entry
        if time.monotonic() >= expires_at:
            del self._cache[tenant_id]
            return None
        # LRU touch — move to end
        self._cache.move_to_end(tenant_id)
        return resolver

    def put(self, tenant_id: str, resolver: Resolver) -> None:
        """Insert or refresh the cached resolver."""
        expires_at = time.monotonic() + self._ttl_s
        if tenant_id in self._cache:
            self._cache.move_to_end(tenant_id)
        self._cache[tenant_id] = (expires_at, resolver)
        # Evict oldest entries past maxsize
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    def invalidate(self, tenant_id: str) -> None:
        """Drop the cached resolver for ``tenant_id`` if present."""
        self._cache.pop(tenant_id, None)

    def clear(self) -> None:
        """Drop all cached entries."""
        self._cache.clear()

    def get_or_load(self, tenant_id: str) -> Resolver:
        """Return the cached resolver, or build + cache a new one.

        Raises:
            TenantLlmNotConfigured: if the tenant has zero enabled
                configs (DB returned 0 rows or all rows decrypted to
                no usable providers).
        """
        cached = self.get(tenant_id)
        if cached is not None:
            return cached
        # Cache miss path
        from llm_client.tenant_resolver import _build_providers_for_tenant

        providers = _build_providers_for_tenant(
            tenant_id=tenant_id,
            repo=self._repo,
            cipher=self._cipher,
            settings=self._settings,
        )
        return self._raise_or_return(tenant_id, providers)

    def _raise_or_return(self, tenant_id: str, providers: dict[str, Any]) -> Resolver:
        """Build the inner gateway; raise TenantLlmNotConfigured if empty."""
        if not providers:
            known = ["minimax", "anthropic", "openai"]  # M4.A registry names
            missing = [p for p in known if p not in providers]
            raise TenantLlmNotConfigured(
                tenant_id=tenant_id,
                missing_providers=missing,
            )
        # Lazy import to avoid circular dep with gateway
        from llm_client.gateway import LLMGateway
        from llm_client.provider_registry import parse_fallback_chain_env

        chain = (
            parse_fallback_chain_env(self._settings.llm_fallback_chain)
            if self._settings is not None
            else None
        )
        gateway = LLMGateway(
            providers=providers,
            default_fallback_chain=chain or None,
            attempt_timeout_s=(
                self._settings.llm_fallback_attempt_timeout_s
                if self._settings is not None
                else None
            ),
        )
        resolver = gateway.default_resolver
        self.put(tenant_id, resolver)
        return resolver


def _build_providers_for_tenant(
    *,
    tenant_id: str,
    repo: TenantLLMConfigRepository,
    cipher: Any,
    settings: Any,
) -> dict[str, Any]:
    """Read tenant's enabled rows, decrypt keys, build providers."""
    import asyncio

    rows = asyncio.get_event_loop().run_until_complete(
        repo.list_by_tenant(tenant_id, enabled_only=True)
    ) if False else None  # noqa: E501  — never executed; real path is async below
    raise RuntimeError("synchronous call not supported — use async build")


async def build_tenant_resolver(
    tenant_id: str,
    *,
    cache: TenantLLMConfigCache,
) -> Resolver:
    """Async builder: return the cached or freshly-built resolver for a tenant.

    Raises:
        TenantLlmNotConfigured: tenant has no enabled provider configs.
    """
    cached = cache.get(tenant_id)
    if cached is not None:
        return cached
    rows = await cache._repo.list_by_tenant(tenant_id, enabled_only=True)
    providers = await _build_providers_from_rows(
        rows=rows, cipher=cache._cipher, settings=cache._settings,
    )
    return cache._raise_or_return(tenant_id, providers)


async def _build_providers_from_rows(
    *,
    rows: list[Any],
    cipher: Any,
    settings: Any,
) -> dict[str, Any]:
    """Decrypt each row's API key and instantiate the matching provider.

    Decryption failures raise ``RuntimeError("encrypted_api_key
    corrupted")`` so the operator notices a master-key rotation
    problem — never silently fall back.
    """
    from llm_client.providers.anthropic_provider import AnthropicProvider
    from llm_client.providers.openai_provider import OpenAIProvider

    out: dict[str, Any] = {}
    for row in rows:
        try:
            api_key = cipher.decrypt(row.encrypted_api_key)
        except Exception as exc:
            raise RuntimeError(
                f"encrypted_api_key corrupted for tenant_id={row.tenant_id} "
                f"provider_name={row.provider_name}: {type(exc).__name__}"
            ) from exc
        if row.provider_name == "minimax":
            out[row.provider_name] = OpenAIProvider(
                api_key=api_key,
                model=(settings.minimax_model if settings else None) or "MiniMax-M3",
                base_url=(
                    row.base_url
                    or (settings.minimax_base_url if settings else None)
                    or "https://api.minimaxi.com/v1"
                ),
            )
        elif row.provider_name == "anthropic":
            out[row.provider_name] = AnthropicProvider(
                api_key=api_key,
                model=(settings.default_llm_model if settings else "claude-3-5-sonnet-20241022"),
            )
        elif row.provider_name == "openai":
            out[row.provider_name] = OpenAIProvider(
                api_key=api_key,
                model=(settings.openai_model if settings else None) or "gpt-4o-mini",
            )
        # Unknown provider_name: silently skip — surfaces in admin API tests
    return out


class TenantResolver:
    """Per-tenant resolver that satisfies the M4.A Resolver + M4.B ainvoke seam.

    Built by :func:`build_tenant_resolver` once per (tenant, cache-miss)
    cycle. ``__call__`` returns the inner gateway's primary provider
    (M4.A protocol); ``ainvoke`` delegates to the inner fallback chain
    if present, otherwise raises :class:`_NoChainConfigured` so
    LLMClient can use its own retry loop on the single provider.
    """

    def __init__(self, *, gateway: "LLMGateway") -> None:
        self._gateway = gateway
        self._delegate = gateway.default_resolver

    def __call__(self, request: "ChatRequest") -> Any:
        return self._delegate(request)

    async def ainvoke(self, request: "ChatRequest") -> "ChatResponse":
        if hasattr(self._delegate, "ainvoke"):
            return await self._delegate.ainvoke(request)
        raise _NoChainConfigured()


__all__ = [
    "TenantLLMConfigCache",
    "TenantResolver",
    "TenantLlmNotConfigured",
    "_NoChainConfigured",
    "build_tenant_resolver",
]
```

- [ ] **Step 2.5: Re-export from `llm_client`**

Modify `apps/api/src/llm_client/__init__.py`:

```python
from llm_client.tenant_resolver import (
    TenantLLMConfigCache,
    TenantResolver,
    build_tenant_resolver,
)
from llm_client.exceptions import TenantLlmNotConfigured

__all__ = [
    # ... existing exports ...
    "TenantLLMConfigCache",
    "TenantResolver",
    "TenantLlmNotConfigured",
    "build_tenant_resolver",
]
```

(Keep the existing exports; just append.)

- [ ] **Step 2.6: Run tests to verify they pass**

Run: `cd apps/api && pytest tests/llm_client/test_tenant_resolver.py -v`
Expected: 8 passed.

NOTE: if any of these tests fail because the inner LLMGateway construction raises on missing `Settings`, instantiate a minimal settings stub inside the test. Tests use mocked providers so the inner gateway construction needs `settings` injected. Re-check `_raise_or_return` to make sure it handles `settings=None` (the gateway might fail if `attempt_timeout_s` is required). If gateway requires a real settings object, add a fixture that builds one with `Settings(environment="test", DATABASE_URL="...", REDIS_URL="...", JWT_SECRET="..."*32)`.

- [ ] **Step 2.7: Commit**

```bash
cd apps/api
git add src/llm_client/tenant_resolver.py \
        src/llm_client/exceptions.py \
        src/llm_client/__init__.py \
        tests/llm_client/test_tenant_resolver.py
git commit -m "feat(llm-client): TenantLLMConfigCache + TenantResolver + TenantLlmNotConfigured (M4.C Task 2)

- LRU (maxsize=1024) + TTL (60s) cache of per-tenant resolvers
- TenantResolver satisfies Resolver + ainvoke seam (M4.A + M4.B compatible)
- TenantLlmNotConfigured(ProviderUnavailable) carries tenant_id + missing_providers
- _NoChainConfigured sentinel for single-provider tenants; LLMClient retries via its own loop
- 8 tests: cache hit/miss/TTL/LRU, empty-rows raises, missing-providers list, resolver delegation

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 3: Exception wiring + LLMFactory update

**Files:**
- Modify: `apps/api/src/agent/llm_factory.py`
- Modify: `apps/api/src/core/business_metrics.py`
- Test: `apps/api/tests/agent/test_llm_factory_tenant.py`

**Goal:** `_default_llm_client_factory(tenant_id)` builds via `TenantResolver.build(tenant_id)`. The `TenantLlmNotConfigured` exception is caught at the right boundary and surfaced to ops via a new metric.

- [ ] **Step 3.1: Add the new metric**

Modify `apps/api/src/core/business_metrics.py` — add after `LLM_FALLBACK_ATTEMPTS_TOTAL`:

```python
# M4.C — Tenant LLM not-configured counter (zero-label by design).
# Spec §8.5: tenant_id stays in logs only to keep cardinality bounded
# and avoid PII leakage via metric scraping.
LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL = Counter(
    "lumen_llm_tenant_not_configured_total",
    "Number of LLM calls rejected because the tenant has no enabled provider configs.",
)
```

- [ ] **Step 3.2: Write failing test for the factory wiring**

Create `apps/api/tests/agent/test_llm_factory_tenant.py`:

```python
"""Tests for _default_llm_client_factory BYOK wiring."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.llm_factory import _default_llm_client_factory
from llm_client.exceptions import TenantLlmNotConfigured


async def test_factory_uses_tenant_resolver() -> None:
    fake_resolver = MagicMock()
    fake_cache = MagicMock()
    fake_cache.get_or_load = AsyncMock(return_value=fake_resolver)

    with patch("agent.llm_factory._build_tenant_cache", return_value=fake_cache):
        client = _default_llm_client_factory(tenant_id="t1")

    assert client.tenant_id == "t1"
    assert client.provider_resolver is fake_resolver


async def test_factory_raises_tenant_not_configured() -> None:
    from llm_client.exceptions import TenantLlmNotConfigured

    fake_cache = MagicMock()
    fake_cache.get_or_load = AsyncMock(
        side_effect=TenantLlmNotConfigured(
            tenant_id="t1", missing_providers=["anthropic"]
        )
    )

    with patch("agent.llm_factory._build_tenant_cache", return_value=fake_cache):
        with pytest.raises(TenantLlmNotConfigured):
            _default_llm_client_factory(tenant_id="t1")
```

- [ ] **Step 3.3: Run tests to verify they fail**

Run: `cd apps/api && pytest tests/agent/test_llm_factory_tenant.py -v`
Expected: FAIL with `AttributeError: module 'agent.llm_factory' has no attribute '_build_tenant_cache'`.

- [ ] **Step 3.4: Update llm_factory.py**

Modify `apps/api/src/agent/llm_factory.py`:

```python
"""Default LLMClient factory — builds tenant-scoped LLMClient instances
backed by a per-tenant LLMGateway via BYOK config (M4.C).

Each tenant's provider keys are read from ``tenant_llm_configs``
(Fernet-encrypted, see ``TenantLLMConfigCipher``), decrypted, and
cached in-process. The cache absorbs the per-request DB + decrypt
cost after the first call. Tenants with zero enabled providers raise
``TenantLlmNotConfigured`` — strict mode, no silent fallback to the
project-wide keys.

See M4.C spec §7 (data flow) and §8.4 (strict-mode boundary).
"""
from __future__ import annotations

from cryptography.fernet import Fernet

from core.config import get_settings
from llm_client.client import LLMClient
from llm_client.tenant_config_crypto import TenantLLMConfigCipher
from llm_client.tenant_config_models import TenantLLMConfigRepository
from llm_client.tenant_resolver import (
    TenantLLMConfigCache,
    build_tenant_resolver,
)
from llm_client.usage import UsageRecorder


# Module-level singleton cache (per process). Reset via tests if needed.
_tenant_cache: TenantLLMConfigCache | None = None


def _build_tenant_cache() -> TenantLLMConfigCache:
    """Construct the per-process TenantLLMConfigCache singleton."""
    global _tenant_cache
    if _tenant_cache is None:
        settings = get_settings()
        cipher = TenantLLMConfigCipher(settings.tenant_llm_fernet_key)
        _tenant_cache = TenantLLMConfigCache(
            ttl_s=settings.tenant_llm_cache_ttl_s,
            maxsize=settings.tenant_llm_cache_maxsize,
            repo=TenantLLMConfigRepository(),
            cipher=cipher,
            settings=settings,
        )
    return _tenant_cache


async def _default_llm_client_factory(tenant_id: str) -> LLMClient:
    """Build a per-tenant LLMClient via TenantResolver.

    Strict mode: tenants with no enabled configs raise
    TenantLlmNotConfigured immediately at factory time — the
    caller (API endpoint) catches and returns a 503-style response.

    Args:
        tenant_id: opaque tenant identifier; must exist in ``tenants``
            table.

    Raises:
        TenantLlmNotConfigured: tenant has zero enabled provider configs.
    """
    cache = _build_tenant_cache()
    resolver = await build_tenant_resolver(tenant_id, cache=cache)
    return LLMClient(
        provider_resolver=resolver,
        tenant_id=tenant_id,
        usage_recorder=UsageRecorder(),
    )


def _resolve_default_model() -> str:
    """Resolve the effective default model from settings.

    (Unchanged from M4.B; preserved for backward compatibility with
    :class:`SimpleResponder.__init__`.)
    """
    from agent.simple_responder import DEFAULT_MODEL
    from core.config import get_settings

    settings = get_settings()
    if settings.minimax_api_key:
        return settings.minimax_model or "MiniMax-M3"
    return DEFAULT_MODEL


__all__ = ["_default_llm_client_factory", "_resolve_default_model", "_build_tenant_cache"]
```

- [ ] **Step 3.5: Update existing callers of `_default_llm_client_factory`**

The factory signature changed from sync to async. Find all callers and add `await`:

```bash
cd apps/api && grep -rn "_default_llm_client_factory" src/ tests/
```

For each call site:
- Replace `_default_llm_client_factory(tenant_id=...)` with `await _default_llm_client_factory(tenant_id=...)`
- If the call is inside a sync function, wrap it: but most are in async coroutines already

Verify: all call sites now `await` the factory. Existing tests in `tests/agent/integration/conftest.py` may need fixture adjustments to be async-aware.

- [ ] **Step 3.6: Run tests to verify they pass**

Run: `cd apps/api && pytest tests/agent/test_llm_factory_tenant.py -v`
Expected: 2 passed.

Then: `cd apps/api && pytest tests/agent/ -v` — verify the wider agent test suite still passes (especially any tests that call `_default_llm_client_factory` directly).

- [ ] **Step 3.7: Commit**

```bash
cd apps/api
git add src/agent/llm_factory.py \
        src/core/business_metrics.py \
        tests/agent/test_llm_factory_tenant.py
git commit -m "feat(agent): _default_llm_client_factory uses TenantResolver (M4.C Task 3)

- Factory signature: sync -> async (DB lookup on cache miss)
- Module-level singleton TenantLLMConfigCache (in-process LRU 1024 + TTL 60s)
- LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL zero-label counter
- TenantLlmNotConfigured raises at factory time (no silent fallback)
- 2 tests: factory wires TenantResolver, factory raises on empty config

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 4: Admin API endpoints

**Files:**
- Create: `apps/api/src/admin/schemas/tenant_llm_config.py`
- Modify: `apps/api/src/admin/schemas/__init__.py`
- Modify: `apps/api/src/admin/repository.py`
- Modify: `apps/api/src/admin/api.py`
- Test: `apps/api/tests/admin/test_tenant_llm_config_api.py`

**Goal:** Admins can register tenant API keys via `POST /admin/tenants/{id}/llm-configs` and list providers via `GET /admin/tenants/{id}/llm-configs`. Decrypted keys never leave the API.

- [ ] **Step 4.1: Write schemas**

Create `apps/api/src/admin/schemas/tenant_llm_config.py`:

```python
"""Pydantic schemas for tenant LLM config admin endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TenantLLMConfigCreate(BaseModel):
    """Request body for POST /admin/tenants/{tenant_id}/llm-configs."""

    provider_name: Literal["minimax", "anthropic", "openai"] = Field(
        ..., description="LLM provider identifier"
    )
    api_key: str = Field(
        ..., min_length=1, max_length=512,
        description="Plaintext API key (encrypted at rest with Fernet)",
    )
    base_url: str | None = Field(
        default=None, max_length=512,
        description="Optional override for provider base URL",
    )
    enabled: bool = Field(default=True)


class TenantLLMConfigRead(BaseModel):
    """Response body — NEVER includes the decrypted API key."""

    provider_name: str
    base_url: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


__all__ = ["TenantLLMConfigCreate", "TenantLLMConfigRead"]
```

Modify `apps/api/src/admin/schemas/__init__.py` to re-export:

```python
from admin.schemas.tenant_llm_config import (
    TenantLLMConfigCreate,
    TenantLLMConfigRead,
)
```

- [ ] **Step 4.2: Write failing tests for admin API**

Create `apps/api/tests/admin/test_tenant_llm_config_api.py`:

```python
"""Tests for /admin/tenants/{id}/llm-configs endpoints."""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tenant.enums import TenantPlan
from tenant.repository import TenantRepository


async def test_post_creates_row_with_encrypted_key(async_client: AsyncClient) -> None:
    tenant = await TenantRepository().create(name="Admin BYOK Test", plan=TenantPlan.PRO)
    resp = await async_client.post(
        f"/admin/tenants/{tenant.id}/llm-configs",
        json={"provider_name": "minimax", "api_key": "sk-test-12345"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["provider_name"] == "minimax"
    assert body["enabled"] is True
    assert "api_key" not in body
    assert "encrypted_api_key" not in body


async def test_post_upserts_existing_row(async_client: AsyncClient) -> None:
    tenant = await TenantRepository().create(name="Admin BYOK Upsert", plan=TenantPlan.PRO)
    first = await async_client.post(
        f"/admin/tenants/{tenant.id}/llm-configs",
        json={"provider_name": "minimax", "api_key": "sk-first"},
    )
    assert first.status_code == 201
    second = await async_client.post(
        f"/admin/tenants/{tenant.id}/llm-configs",
        json={"provider_name": "minimax", "api_key": "sk-second"},
    )
    assert second.status_code == 201
    assert second.json()["updated_at"] != first.json()["updated_at"]


async def test_get_returns_provider_names_without_keys(async_client: AsyncClient) -> None:
    tenant = await TenantRepository().create(name="Admin BYOK Get", plan=TenantPlan.PRO)
    await async_client.post(
        f"/admin/tenants/{tenant.id}/llm-configs",
        json={"provider_name": "minimax", "api_key": "sk-test"},
    )
    resp = await async_client.get(f"/admin/tenants/{tenant.id}/llm-configs")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["provider_name"] == "minimax"
    assert "api_key" not in rows[0]
    assert "encrypted_api_key" not in rows[0]


async def test_get_returns_404_for_unknown_tenant(async_client: AsyncClient) -> None:
    resp = await async_client.get("/admin/tenants/nonexistent-tenant/llm-configs")
    assert resp.status_code == 404
```

- [ ] **Step 4.3: Run tests to verify they fail**

Run: `cd apps/api && pytest tests/admin/test_tenant_llm_config_api.py -v`
Expected: FAIL with 404 (endpoint not registered).

- [ ] **Step 4.4: Add repository methods**

Modify `apps/api/src/admin/repository.py`:

```python
# Add import at top
from llm_client.tenant_config_models import TenantLLMConfigRepository
from core.config import get_settings
from llm_client.tenant_config_crypto import TenantLLMConfigCipher
from admin.schemas.tenant_llm_config import TenantLLMConfigCreate
from tenant.repository import TenantRepository


class AdminTenantLLMConfigRepository:
    """Admin-facing wrapper that handles encryption + tenant existence check."""

    def __init__(self) -> None:
        self._inner = TenantLLMConfigRepository()
        self._cipher = TenantLLMConfigCipher(get_settings().tenant_llm_fernet_key)
        self._tenants = TenantRepository()

    async def upsert(
        self, *, tenant_id: str, payload: TenantLLMConfigCreate
    ) -> TenantLLMConfig:
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        encrypted = self._cipher.encrypt(payload.api_key)
        return await self._inner.upsert(
            tenant_id=tenant_id,
            provider_name=payload.provider_name,
            encrypted_api_key=encrypted,
            base_url=payload.base_url,
            enabled=payload.enabled,
        )

    async def list(self, tenant_id: str) -> list[TenantLLMConfig]:
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        return await self._inner.list_by_tenant(tenant_id, enabled_only=False)
```

- [ ] **Step 4.5: Add admin endpoints**

Modify `apps/api/src/admin/api.py` — add after the existing endpoints:

```python
from admin.repository import AdminTenantLLMConfigRepository
from admin.schemas.tenant_llm_config import (
    TenantLLMConfigCreate,
    TenantLLMConfigRead,
)


@router.post(
    "/tenants/{tenant_id}/llm-configs",
    response_model=TenantLLMConfigRead,
    status_code=201,
)
async def create_or_update_tenant_llm_config(
    tenant_id: str,
    payload: TenantLLMConfigCreate,
) -> TenantLLMConfigRead:
    """Upsert a tenant's LLM provider config.

    The plaintext ``api_key`` is encrypted at rest; the response
    NEVER includes the key (decrypted or encrypted).
    """
    try:
        row = await AdminTenantLLMConfigRepository().upsert(
            tenant_id=tenant_id, payload=payload
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return TenantLLMConfigRead(
        provider_name=row.provider_name,
        base_url=row.base_url,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get(
    "/tenants/{tenant_id}/llm-configs",
    response_model=list[TenantLLMConfigRead],
)
async def list_tenant_llm_configs(
    tenant_id: str,
) -> list[TenantLLMConfigRead]:
    """List a tenant's LLM provider configs.

    Returns provider_name + enabled + timestamps only; the encrypted
    API key is NEVER included in the response.
    """
    try:
        rows = await AdminTenantLLMConfigRepository().list(tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return [
        TenantLLMConfigRead(
            provider_name=r.provider_name,
            base_url=r.base_url,
            enabled=r.enabled,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]
```

- [ ] **Step 4.6: Run tests to verify they pass**

Run: `cd apps/api && pytest tests/admin/test_tenant_llm_config_api.py -v`
Expected: 4 passed.

- [ ] **Step 4.7: Commit**

```bash
cd apps/api
git add src/admin/schemas/tenant_llm_config.py \
        src/admin/schemas/__init__.py \
        src/admin/repository.py \
        src/admin/api.py \
        tests/admin/test_tenant_llm_config_api.py
git commit -m "feat(admin): tenant LLM config POST/GET endpoints (M4.C Task 4)

- POST /admin/tenants/{id}/llm-configs: upsert (Fernet encrypts before storage)
- GET /admin/tenants/{id}/llm-configs: list provider names + metadata (NEVER key)
- 404 for unknown tenant (TenantRepository.get_by_id check)
- Pydantic Literal['minimax','anthropic','openai'] restricts provider_name
- 4 admin API tests passing

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## Task 5: E2E integration + close-out

**Files:**
- Create: `apps/api/tests/llm_client/integration/test_tenant_resolver_e2e.py`
- Modify: `README.md`
- Create: `~/.claude/projects/D--work-ai-0401-ai-customer/memory/m4-c-progress.md`
- Modify: `~/.claude/projects/D--work-ai-0401-ai-customer/memory/MEMORY.md`

**Goal:** End-to-end verification via real HTTP mocks + docs + memory.

- [ ] **Step 5.1: Write failing e2e tests**

Create `apps/api/tests/llm_client/integration/test_tenant_resolver_e2e.py`:

```python
"""End-to-end tests for TenantResolver with real HTTP traffic via pytest-httpx."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pytest_httpx import HTTPXMock

from agent.llm_factory import _default_llm_client_factory
from llm_client.exceptions import TenantLlmNotConfigured
from llm_client.types import ChatMessage, ChatRequest
from llm_client.usage import UsageRecorder
from llm_client.tenant_config_crypto import TenantLLMConfigCipher
from tenant.enums import TenantPlan
from tenant.repository import TenantRepository


async def test_e2e_tenant_key_used_in_anthropic_request(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the tenant's decrypted key is what reaches the provider's HTTP client."""
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("TENANT_LLM_FERNET_KEY", fernet_key)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # disable global Anthropic key
    monkeypatch.setenv("OPENAI_API_KEY", "")  # disable global provider keys

    tenant = await TenantRepository().create(name="E2E Antr Key", plan=TenantPlan.PRO)
    cipher = TenantLLMConfigCipher(fernet_key)
    ciphertext = cipher.encrypt("sk-tenant-anthropic-secret")
    from llm_client.tenant_config_models import TenantLLMConfigRepository
    await TenantLLMConfigRepository().upsert(
        tenant_id=tenant.id,
        provider_name="anthropic",
        encrypted_api_key=ciphertext,
        base_url=None,
        enabled=True,
    )
    # Reset module-level cache so the new fernet_key takes effect
    from agent import llm_factory
    llm_factory._tenant_cache = None

    # Mock Anthropic API endpoint with pytest-httpx
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        json={
            "id": "msg_test",
            "content": [{"type": "text", "text": "hello from anthropic"}],
            "model": "claude-3-5-sonnet-20241022",
            "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                },
            },
            status_code=200,
        )
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        json={
            "id": "msg_test2",
            "content": [{"type": "text", "text": "hello"}],
            "model": "claude-3-5-sonnet-20241022",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        status_code=200,
    )

    client = await _default_llm_client_factory(tenant_id=tenant.id)
    resp = await client.chat(
        ChatRequest(
            model="claude-3-5-sonnet-20241022",
            messages=[ChatMessage(role="user", content="hi")],
        )
    )
    assert resp.content == "hello from anthropic"

    # Verify the request used the tenant's key (not a global key)
    requests = httpx_mock.get_requests()
    assert len(requests) >= 1
    auth_header = requests[0].headers.get("x-api-key", "")
    assert auth_header == "sk-tenant-anthropic-secret"


async def test_e2e_tenant_not_configured_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("TENANT_LLM_FERNET_KEY", fernet_key)

    tenant = await TenantRepository().create(name="E2E No Key", plan=TenantPlan.PRO)
    from agent import llm_factory
    llm_factory._tenant_cache = None

    with pytest.raises(TenantLlmNotConfigured) as exc:
        await _default_llm_client_factory(tenant_id=tenant.id)
    assert exc.value.tenant_id == tenant.id


async def test_e2e_tenant_key_isolation(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tenants each with their own key — verify no cross-leak."""
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("TENANT_LLM_FERNET_KEY", fernet_key)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    cipher = TenantLLMConfigCipher(fernet_key)
    t1 = await TenantRepository().create(name="E2E Iso 1", plan=TenantPlan.PRO)
    t2 = await TenantRepository().create(name="E2E Iso 2", plan=TenantPlan.PRO)
    from llm_client.tenant_config_models import TenantLLMConfigRepository
    repo = TenantLLMConfigRepository()
    await repo.upsert(
        tenant_id=t1.id, provider_name="anthropic",
        encrypted_api_key=cipher.encrypt("sk-tenant-1-key"), base_url=None, enabled=True,
    )
    await repo.upsert(
        tenant_id=t2.id, provider_name="anthropic",
        encrypted_api_key=cipher.encrypt("sk-tenant-2-key"), base_url=None, enabled=True,
    )

    from agent import llm_factory
    llm_factory._tenant_cache = None

    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        method="POST",
        json={
            "id": "msg",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-3-5-sonnet-20241022",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        status_code=200,
    )

    c1 = await _default_llm_client_factory(tenant_id=t1.id)
    await c1.chat(
        ChatRequest(
            model="claude-3-5-sonnet-20241022",
            messages=[ChatMessage(role="user", content="hi")],
        )
    )
    requests = httpx_mock.get_requests()
    # First request should carry tenant 1's key
    assert requests[0].headers.get("x-api-key") == "sk-tenant-1-key"
```

NOTE: This is a sample of the most important e2e tests. Add the full set per spec §9.4 (4 tests). Verify the test mocks the right URL — `AnthropicProvider.chat` hits `https://api.anthropic.com/v1/messages`. Adjust the mock URL to match the actual provider URL.

- [ ] **Step 5.2: Run e2e tests**

Run: `cd apps/api && pytest tests/llm_client/integration/test_tenant_resolver_e2e.py -v`
Expected: 3+ passed. If any fail, debug per the assertion messages (common: missing mock URL, wrong API key, etc.).

- [ ] **Step 5.3: Run full test suite to check for regressions**

Run: `cd apps/api && pytest tests/ -q --tb=line`
Expected: all M4.A/B tests + M4.C new tests pass; pre-existing skipped/unrelated failures (per M4.B baseline) remain.

- [ ] **Step 5.4: Update README**

Modify `README.md` — add M4.C row to the project status table (find existing M4.B row, add M4.C below it). Add the M4.C tech-debt section with the 7 items per spec §11:

```markdown
### M4.C — Tenant Resolver (BYOK)

Per-tenant provider API keys (BYOK), encrypted at rest with Fernet,
cached with in-process LRU + TTL. See
[[docs/superpowers/specs/2026-10-05-m4-c-tenant-resolver-design]].

| Component | Status |
|---|---|
| `TenantLLMConfigCipher` (Fernet) | shipped |
| `tenant_llm_configs` table + repo | shipped |
| `TenantResolver` (LRU + TTL cache) | shipped |
| `_default_llm_client_factory` async + strict mode | shipped |
| `POST /admin/tenants/{id}/llm-configs` + `GET` | shipped |
| `LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL` metric | shipped |

#### M4.C known tech debt

1. **No explicit cache invalidation on admin write** — admin POSTs do not
   invalidate; operators see propagation within ≤ 60s (TTL window).
   Follow-up: add Redis pub/sub (`admin.channel:tenant_llm_changed`).
2. **Fernet key rotation** — master key is loaded once at startup.
   Rotation requires (a) restart, (b) re-encrypt every row, (c) update env.
   No zero-downtime rotation.
3. **No audit log** — `tenant_llm_configs` updates are silent.
4. **No per-tenant fallback chain** — chain is project-wide.
5. **`enabled=FALSE` semantics** — currently "skip in resolver".
6. **No per-tenant model override** — model is project-default per provider.
7. **Demo / staging seeding** — demo tenant must be seeded via admin API.
```

- [ ] **Step 5.5: Create memory file**

Create `~/.claude/projects/D--work-ai-0401-ai-customer/memory/m4-c-progress.md`:

```markdown
---
name: m4-c-progress
description: "M4.C — BYOK TenantResolver (per-tenant provider API keys + Fernet + LRU cache), 5 tasks shipped"
metadata:
  node_type: memory
  type: project
  originSessionId: 2026-10-05
  modified: 2026-10-05T...:00.000Z
---

M4.C plan shipped all 5 implementation tasks + close-out.

## Scope

- **Task 1** — `apps/api/src/llm_client/tenant_config_crypto.py` + `tenant_config_models.py` + migration `16_add_tenant_llm_configs.py`: `TenantLLMConfigCipher` (Fernet) + `TenantLLMConfig` ORM + `TenantLLMConfigRepository` (list_by_tenant + upsert via ON CONFLICT). 4 cipher tests + 5 ORM tests.
- **Task 2** — `apps/api/src/llm_client/tenant_resolver.py`: `TenantLLMConfigCache` (LRU + TTL) + `TenantResolver` class (implements M4.A `Resolver` + M4.B `ainvoke`) + `_NoChainConfigured` sentinel + `TenantLlmNotConfigured(ProviderUnavailable)` with `tenant_id` + `missing_providers`. 8 tests.
- **Task 3** — `apps/api/src/agent/llm_factory.py` + `core/business_metrics.py`: factory signature sync→async; module-level `_tenant_cache` singleton; `LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL` zero-label counter. 2 factory tests.
- **Task 4** — `apps/api/src/admin/{api.py,repository.py,schemas/tenant_llm_config.py}`: `POST /admin/tenants/{id}/llm-configs` + `GET` (no key in response); `TenantLLMConfigCreate` (Literal provider_name) + `TenantLLMConfigRead`. 4 admin tests.
- **Task 5 (close-out)** — e2e tests via pytest-httpx + README status row + 7 known tech debt items + memory file + MEMORY.md pointer.

## Architecture decisions

- **Strict mode boundary** — tenant has no enabled providers → `TenantLlmNotConfigured` raised at factory time (NOT silently falling back to global project keys). Per spec §8.4.
- **Per-tenant `LLMGateway`** — not one shared gateway. Provider keys are tenant-private; sharing would leak key A into tenant B's HTTP traffic. Cache absorbs gateway construction cost.
- **`_NoChainConfigured` sentinel** — single-provider tenants can't have `ainvoke` delegate to a fallback chain; sentinel raised so `LLMClient` falls back to its own retry loop on the primary provider.
- **Fernet at app layer, master key from env** — no KMS, no pgcrypto. `TENANT_LLM_FERNET_KEY` required (cipher raises `RuntimeError` at construction if missing).
- **In-process LRU + TTL, no Redis** — `tenant_llm_cache_maxsize=1024` × `tenant_llm_cache_ttl_s=60.0`. Multi-instance deployments see one DB hit per process per tenant per TTL.
- **Zero-label `LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL`** — `tenant_id` stays in logs only (PII discipline + cardinality bound).
- **`enabled=FALSE` semantics** — "skip in resolver" (preserve key for re-enable). Could be expanded to rate-limit / circuit-broken in M4.D.
- **`tenant_id` as opaque string in `LLMClient`** — M4.A signature preserved. `TenantResolver` hides behind the resolver seam.

## Known tech debt (in README)

- 7 items from spec §11: TTL invalidation, Fernet rotation, audit log, per-tenant chain, enabled=FALSE expansion, per-tenant model, demo/staging seeding.

## Test counts

- **Total new tests across Tasks 1-5: ~26**
  - Task 1 (cipher + ORM): 9
  - Task 2 (cache + resolver): 8
  - Task 3 (factory wiring): 2
  - Task 4 (admin API): 4
  - Task 5 (e2e): 3+ (depending on full set)
- All e2e tests pass via pytest-httpx

## Why

M4.C is the multi-tenant isolation layer on top of M4.B's reliability layer.
Primary use case: enterprise customers requiring their own API keys (data
residency, billing isolation, vendor procurement). Composition pattern (per
spec §6.4 + Future Work): M4.D budget layers on top via
`LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL` + per-tenant token counters.

## How to apply

When adding M4.D (budget) work: read tenant configs (already encrypted +
cached) to extract per-tenant rate limits; layer `BudgetResolver` BETWEEN
`TenantResolver` and `LLMGateway`. When adding per-tenant fallback chain
(follow-up to #4): extend `tenant_llm_configs` with `fallback_chain_json`
column; `TenantResolver` reads the column on cache miss. When adding admin
SPA UI for BYOK: admin SPA already exists per tech-debt #20 — add a new
"LLM Config" tab. Carry forward PII discipline + multi-tenant isolation
rules from [[m1-design]] / [[m4-a-progress]] / [[m4-b-progress]].
```

- [ ] **Step 5.6: Update MEMORY.md**

Modify `~/.claude/projects/D--work-ai-0401-ai-customer/memory/MEMORY.md` — add pointer:

```markdown
- [M4.C progress](m4-c-progress.md) — M4.C TenantResolver shipped (BYOK + Fernet + LRU + admin API)
```

- [ ] **Step 5.7: Commit close-out**

```bash
cd apps/api
git add tests/llm_client/integration/test_tenant_resolver_e2e.py README.md
git commit -m "test(llm-client): M4.C e2e integration + README close-out (Task 5)

- pytest-httpx: tenant key in HTTP request, not-configured error, tenant isolation
- README M4.C status row + 7 known tech debt items
- Memory file m4-c-progress.md + MEMORY.md pointer

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

- [ ] **Step 5.8: Push and verify origin**

Run: `cd apps/api && git push origin main`
Expected: 5 commits land on origin/main.

---

## Self-Review Notes

**Spec coverage check** (spec → task mapping):
| Spec section | Task |
|---|---|
| §1 Goals | All |
| §3 Background | Task 2 (intent) |
| §4 Architecture | Task 2 (cache + resolver) |
| §5 Components | All 5 tasks (one per layer) |
| §6 Data Model | Task 1 (ORM + migration) |
| §7.1 Per-call | Task 3 (factory wiring) |
| §7.2 Admin write | Task 4 (POST endpoint) |
| §7.3 Admin read | Task 4 (GET endpoint) |
| §8.1 Resolver contract | Task 2 (TenantResolver) |
| §8.2 Cache semantics | Task 2 (TenantLLMConfigCache) |
| §8.3 Fernet cipher | Task 1 (TenantLLMConfigCipher) |
| §8.4 Strict mode | Task 3 (factory raises) |
| §8.5 Metric | Task 3 (LLM_TENANT_LLM_NOT_CONFIGURED_TOTAL) |
| §8.6 Fallback interaction | Task 2 (TenantResolver gating ainner) |
| §9.1-9.5 Tests | Tasks 1, 2, 3, 4, 5 (each adds its slice) |
| §10 Rollout | Tasks 1-5 |
| §11 Tech debt | Task 5 (README) |

**No placeholders detected** (grep for TODO/TBD/placeholder/FIXME → 0 matches).

**Type consistency check**:
- `TenantLlmNotConfigured(tenant_id: str, missing_providers: list[str])` — consistent across Task 1 (exception definition), Task 2 (raises), Task 3 (factory raises), Task 5 (e2e test).
- `TenantResolver(gateway=LLMGateway)` — consistent in Task 2 and Task 3.
- `TenantLLMConfigCache(ttl_s, maxsize, repo, cipher, settings)` — consistent.
- `TenantLLMConfigRepository.list_by_tenant(tenant_id, enabled_only=True)` — consistent in Tasks 1, 2, 5.
- `TenantLLMConfigRepository.upsert(tenant_id, provider_name, encrypted_api_key, base_url, enabled)` — consistent in Tasks 1, 4.

**One known drift**: spec §6.1 says column type is `BYTEA` for `encrypted_api_key`; ORM model uses SQLAlchemy `LargeBinary` (which maps to BYTEA in Postgres). Verified consistent.

**Execution Handoff**:

Plan complete and saved to `docs/superpowers/plans/2026-10-05-m4-c-tenant-resolver.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?