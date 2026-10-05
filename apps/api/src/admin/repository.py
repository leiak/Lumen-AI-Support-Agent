"""Admin-facing repository layer.

M4.C Task 4 — admin BYOK tenant-config repository.

The admin layer adds two layers of safety on top of the low-level
:class:`llm_client.tenant_config_models.TenantLLMConfigRepository`:

1. **Tenant existence check** — refuse to upsert / list configs for a
   tenant that doesn't exist in the ``tenants`` table. Without this
   a malicious or buggy admin client could spam rows tied to a
   non-existent tenant_id; the FK constraint would catch it on
   INSERT but on upsert the ON CONFLICT branch would silently
   succeed against a stale row. Better to 404 early.
2. **Encryption boundary** — wraps the
   :class:`TenantLLMConfigCipher` so callers (the API layer) never
   touch plaintext / ciphertext bytes directly. The repository is
   the ONLY layer that holds the cipher key and the ONLY one that
   ever produces ciphertext for storage.

The admin repository never returns the decrypted key; that contract
is enforced by :class:`admin.schemas.tenant_llm_config.TenantLLMConfigRead`,
which carries no key fields at all.

M4.D Task 4 — admin budget config repository.

:class:`AdminTenantBudgetRepository` mirrors the same tenant-existence
check pattern. Budget config carries no secrets, so there is no
encryption layer — just the existence check + delegation to the inner
:class:`TenantBudgetRepository`.
"""
from __future__ import annotations

from budget.models import TenantBudget
from budget.repository import TenantBudgetRepository
from llm_client.tenant_config_crypto import TenantLLMConfigCipher
from llm_client.tenant_config_models import (
    TenantLLMConfig,
    TenantLLMConfigRepository,
)

from admin.schemas.budget import TenantBudgetCreate
from admin.schemas.tenant_llm_config import TenantLLMConfigCreate
from core.config import get_settings
from tenant.repository import TenantRepository


class AdminTenantLLMConfigRepository:
    """Admin-facing wrapper around :class:`TenantLLMConfigRepository`.

    Responsibilities:

    * Verify the tenant exists before any upsert / list (404 otherwise).
    * Encrypt the plaintext at the boundary; never leak it past this class.
    * Wrap every encrypt / upsert pair so the API endpoints only see
      :class:`TenantLLMConfig` rows — never the bytes.
    """

    def __init__(self) -> None:
        self._inner = TenantLLMConfigRepository()
        self._cipher = TenantLLMConfigCipher(get_settings().tenant_llm_fernet_key)
        self._tenants = TenantRepository()

    async def upsert(
        self, *, tenant_id: str, payload: TenantLLMConfigCreate,
    ) -> TenantLLMConfig:
        """Upsert a tenant's LLM provider config.

        Raises:
            ValueError: tenant does not exist. Caller (API layer)
                translates to ``HTTPException(404)``. Anti-enumeration:
                the same 404 is returned whether the tenant doesn't
                exist OR the tenant exists but the caller can't see it.
        """
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
        """List all provider configs for a tenant (enabled + disabled).

        Raises:
            ValueError: tenant does not exist (see :meth:`upsert`).
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        return await self._inner.list_by_tenant(tenant_id, enabled_only=False)


class AdminTenantBudgetRepository:
    """Admin-facing wrapper around :class:`TenantBudgetRepository`.

    Mirrors :class:`AdminTenantLLMConfigRepository`'s tenant-existence
    check + delegation pattern. No encryption layer — budget config
    carries no secrets (just token counts + an IANA timezone string).
    """

    def __init__(self) -> None:
        self._inner = TenantBudgetRepository()
        self._tenants = TenantRepository()

    async def upsert(
        self, *, tenant_id: str, payload: TenantBudgetCreate,
    ) -> TenantBudget:
        """Upsert a tenant's budget config.

        Raises:
            ValueError: tenant does not exist. Caller (API layer)
                translates to ``HTTPException(404)``. Anti-enumeration:
                the same 404 is returned whether the tenant doesn't
                exist OR the tenant exists but the caller can't see it.
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        return await self._inner.upsert(
            tenant_id=tenant_id,
            soft_warn_tokens=payload.soft_warn_tokens,
            hard_cap_tokens=payload.hard_cap_tokens,
            period_anchor_tz=payload.period_anchor_tz,
        )

    async def get(self, tenant_id: str) -> TenantBudget | None:
        """Return the budget row for a tenant (or ``None`` if not set).

        Raises:
            ValueError: tenant does not exist (see :meth:`upsert`).
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        return await self._inner.get_by_tenant(tenant_id)


__all__ = ["AdminTenantBudgetRepository", "AdminTenantLLMConfigRepository"]